#!/usr/bin/env python3
"""Tests for navidrome_favsync.

Three layers:

  1. Container maths, checked against an independent bitwise CRC reference and
     against real Ogg files shipped with the system (freedesktop sound theme).
  2. Opus rewriting, using a synthetic stream laid out exactly the way ffmpeg's
     ``-f opus`` muxer writes one.
  3. End to end against a mock Navidrome server that can be told to misbehave:
     429, lying Content-Length, mid-transfer disconnects, HTTP 200 carrying a
     Subsonic error document, and streaming the original file instead of a
     transcode.

Run with:  python3 -m unittest -v test_navidrome_favsync
"""

from __future__ import annotations

import contextlib
import glob
import hashlib
import http.server
import io
import json
import os
import struct
import tempfile
import threading
import time
import unittest
import urllib.parse
from pathlib import Path
from unittest import mock

import navidrome_favsync as navi


# ---------------------------------------------------------------------------
# Independent helpers. These deliberately do not use the code under test to
# build fixtures, so a bug in the module cannot cancel itself out.
# ---------------------------------------------------------------------------

OGG_CRC_POLY = 0x04C11DB7


def reference_ogg_crc(data: bytes) -> int:
    """Bitwise Ogg CRC exactly as the specification defines it (no lookup table)."""
    crc = 0
    for byte in data:
        crc ^= byte << 24
        crc &= 0xFFFFFFFF
        for _ in range(8):
            if crc & 0x80000000:
                crc = ((crc << 1) ^ OGG_CRC_POLY) & 0xFFFFFFFF
            else:
                crc = (crc << 1) & 0xFFFFFFFF
    return crc & 0xFFFFFFFF


def build_ogg_page(serial: int, sequence: int, granule: int, header_type: int, lacing, payload: bytes) -> bytes:
    table = bytes(lacing)
    page = bytearray(b"OggS")
    page += bytes((0, header_type))
    page += struct.pack("<q", granule)
    page += struct.pack("<III", serial, sequence, 0)
    page += bytes((len(table),))
    page += table
    page += payload
    struct.pack_into("<I", page, 22, reference_ogg_crc(bytes(page)))
    return bytes(page)


def lace(packet: bytes):
    lacing = []
    remaining = len(packet)
    while remaining >= 255:
        lacing.append(255)
        remaining -= 255
    lacing.append(remaining)
    return lacing


def build_opus_head(channels: int = 2, preskip: int = 312, rate: int = 48000) -> bytes:
    return (
        b"OpusHead"
        + bytes((1, channels))
        + struct.pack("<H", preskip)
        + struct.pack("<I", rate)
        + struct.pack("<h", 0)
        + bytes((0,))
    )


def build_opus_tags(vendor: str, comments) -> bytes:
    out = bytearray(b"OpusTags")
    encoded = vendor.encode("utf-8")
    out += struct.pack("<I", len(encoded)) + encoded
    out += struct.pack("<I", len(comments))
    for key, value in comments:
        item = f"{key}={value}".encode("utf-8")
        out += struct.pack("<I", len(item)) + item
    return bytes(out)


def paginate(packets, serial: int, start_sequence: int, bos: bool = True):
    """Split packets across Ogg pages of at most 255 segments, as a muxer would.

    Written independently of the module under test; the segment-table layout
    itself is dictated by the Ogg spec.
    """
    runs = []  # (segment_size, chunk, terminates_packet)
    for packet in packets:
        segments = lace(packet)
        offset = 0
        for index, size in enumerate(segments):
            runs.append((size, packet[offset : offset + size], index == len(segments) - 1))
            offset += size
    pages = []
    index = 0
    while index < len(runs):
        chunk = runs[index : index + 255]
        index += 255
        if not pages:
            header_type = 0x02 if bos else 0x00
        elif not chunk[0][2]:
            header_type = 0x01
        else:
            header_type = 0x00
        pages.append(
            build_ogg_page(
                serial,
                start_sequence + len(pages),
                0,
                header_type,
                [item[0] for item in chunk],
                b"".join(item[1] for item in chunk),
            )
        )
    return pages


def build_opus_stream(
    *,
    serial: int = 0x1234ABCD,
    audio_packets=None,
    tags_comments=(("TITLE", "Fixture Track"), ("ARTIST", "Fixture Artist")),
    vendor: str = "navifavsync-tests",
    tags_padding: int = 0,
) -> bytes:
    """Assemble an Ogg/Opus stream shaped like ffmpeg's ``-f opus`` output.

    ``tags_padding`` inflates the comment packet so that it must span several
    Ogg pages, which is what happens once cover art is embedded.
    """
    if audio_packets is None:
        audio_packets = (
            b"\xfc" + bytes(index % 256 for index in range(200)),
            b"\xfc" + bytes((index + 100) % 256 for index in range(200)),
        )
    head = build_opus_head()
    comments = list(tags_comments)
    if tags_padding:
        comments.append(("PADDING", "x" * tags_padding))
    tags = build_opus_tags(vendor, comments)

    out = bytearray()
    for page in paginate([head], serial, 0):
        out += page
    for page in paginate([tags], serial, len(list(navi.iter_ogg_pages(bytes(out))))):
        out += page

    sequence = len(list(navi.iter_ogg_pages(bytes(out))))
    granule = 0
    for packet in audio_packets:
        granule += 960
        out += build_ogg_page(serial, sequence, granule, 0x00, lace(packet), packet)
        sequence += 1
    # No end-of-stream flag: real ffmpeg output does not set header_type 0x04.
    out += build_ogg_page(serial, sequence, granule, 0x00, [5], b"\x00" * 5)
    return bytes(out)


def make_jpeg(width: int = 600, height: int = 600, payload_size: int = 900) -> bytes:
    """A structurally valid JPEG header plus filler scan data."""
    out = bytearray(b"\xff\xd8")
    out += b"\xff\xe0" + struct.pack(">H", 16) + b"JFIF\x00" + b"\x01\x01\x00" + b"\x00\x01\x00\x01" + b"\x00\x00"
    sof = b"\xff\xc0" + struct.pack(">H", 17) + bytes((8,)) + struct.pack(">HH", height, width) + bytes((3, 1, 0x11, 0, 2, 0x11, 1, 3, 0x11, 1))
    out += sof
    out += b"\xff\xda" + struct.pack(">H", 12) + bytes((3, 1, 0x00, 2, 0x11, 3, 0x11, 0, 63, 0))
    body = bytes((index * 37 + 11) % 256 for index in range(payload_size))
    out += body
    out += b"\xff\xd9"
    return bytes(out)


def make_png(width: int = 320, height: int = 240, bit_depth: int = 8, color_type: int = 2) -> bytes:
    ihdr = struct.pack(">IIBBBBB", width, height, bit_depth, color_type, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + struct.pack(">I", len(ihdr)) + b"IHDR" + ihdr + struct.pack(">I", 0) + b"IEND" + b"\x00\x00\x00\x00"


def make_webp_lossy(width: int = 640, height: int = 480) -> bytes:
    body = b"VP8 " + struct.pack("<I", 8) + bytes(3) + b"\x9d\x01\x2a" + struct.pack("<HH", width, height)
    return b"RIFF" + struct.pack("<I", 4 + len(body)) + b"WEBP" + body


def make_webp_lossless(width: int = 300, height: int = 200) -> bytes:
    bits = (width - 1) | ((height - 1) << 14)
    body = b"VP8L" + struct.pack("<I", 5) + bytes((0x2F,)) + struct.pack("<I", bits)
    return b"RIFF" + struct.pack("<I", 4 + len(body)) + b"WEBP" + body


def make_webp_extended(width: int = 1024, height: int = 768) -> bytes:
    body = b"VP8X" + struct.pack("<I", 10) + bytes(4) + (width - 1).to_bytes(3, "little") + (height - 1).to_bytes(3, "little")
    return b"RIFF" + struct.pack("<I", 4 + len(body)) + b"WEBP" + body


def audio_payload_bytes(data: bytes) -> bytes:
    """Concatenate the audio page payloads of an Opus stream, headers excluded."""
    pages = navi.iter_ogg_pages(data)
    _packets, _consumed, _serial = navi._consume_header_packets(pages)
    return b"".join(page.payload for page in pages)


# ---------------------------------------------------------------------------
# 1. Container maths
# ---------------------------------------------------------------------------


class TestOggCrc(unittest.TestCase):
    def test_matches_bitwise_reference(self):
        import random as _random

        rng = _random.Random(20240926)
        for length in (0, 1, 2, 3, 27, 64, 1000, 65025):
            data = bytes(rng.randrange(256) for _ in range(length))
            self.assertEqual(navi.ogg_crc32(data), reference_ogg_crc(data), f"mismatch at length {length}")

    def test_known_vectors(self):
        # Deterministic, human-checkable cases.
        self.assertEqual(navi.ogg_crc32(b""), 0)
        self.assertEqual(navi.ogg_crc32(b"\x00" * 4), reference_ogg_crc(b"\x00" * 4))
        self.assertEqual(navi.ogg_crc32(b"OggS"), reference_ogg_crc(b"OggS"))


@unittest.skipUnless(glob.glob("/usr/share/sounds/**/*.oga", recursive=True), "no system Ogg files")
class TestRealOggFiles(unittest.TestCase):
    """The parser must accept files produced by a real encoder."""

    def files(self):
        return sorted(glob.glob("/usr/share/sounds/**/*.oga", recursive=True))

    def test_all_system_files_parse_and_verify_crc(self):
        for path in self.files():
            with self.subTest(file=os.path.basename(path)):
                data = Path(path).read_bytes()
                pages = list(navi.iter_ogg_pages(data))  # raises on any bad CRC
                self.assertGreater(len(pages), 1)
                self.assertTrue(pages[0].header_type & 0x02, "first page must carry BOS")
                serial = pages[0].serial
                for index, page in enumerate(pages):
                    self.assertEqual(page.sequence, index)
                    self.assertEqual(page.serial, serial)
                # A continued page must actually continue something.
                for page in pages[1:]:
                    if page.header_type & 0x01:
                        self.assertTrue(page.packets or page.carry_after)

    def test_detects_a_single_bit_flip(self):
        path = self.files()[0]
        data = bytearray(Path(path).read_bytes())
        data[len(data) // 2] ^= 0x01
        with self.assertRaises(navi.OggError):
            list(navi.iter_ogg_pages(bytes(data)))

    def test_known_vorbis_files(self):
        data = Path(self.files()[0]).read_bytes()
        self.assertEqual(navi.sniff_audio_format(data[:64]), "vorbis")

    def test_refuses_to_rewrite_vorbis_header_layout(self):
        """Vorbis packs comment+setup in one packet that spans pages, so the
        injector must decline rather than guess."""
        data = Path(self.files()[0]).read_bytes()
        with self.assertRaises(navi.OggError):
            navi.embed_cover_art(data, make_jpeg())


# ---------------------------------------------------------------------------
# 2. Opus rewriting
# ---------------------------------------------------------------------------


class TestOpusTags(unittest.TestCase):
    def test_round_trip(self):
        comments = [("TITLE", "Hello"), ("ARTIST", "Ünicode Ãrtist"), ("X", "")]
        packet = navi.build_opus_tags("some-vendor", comments)
        self.assertTrue(packet.startswith(b"OpusTags"))
        vendor, parsed = navi.parse_opus_tags(packet)
        self.assertEqual(vendor, "some-vendor")
        self.assertEqual(parsed, comments)

    def test_rejects_non_tags_packet(self):
        with self.assertRaises(navi.OggError):
            navi.parse_opus_tags(b"NotTags" + bytes(40))


class TestCompletenessCheck(unittest.TestCase):
    """The Ogg container, not Content-Length, decides whether a body is whole."""

    def test_accepts_a_complete_stream(self):
        data = build_opus_stream()
        stats = navi.assert_complete_opus(data)
        self.assertGreaterEqual(stats["pages"], 3)
        self.assertGreaterEqual(stats["granule"], 0)

    def test_accepts_a_stream_without_an_eos_flag(self):
        """Real ffmpeg output does not set header_type 0x04 on the last page."""
        data = build_opus_stream()
        pages = list(navi.iter_ogg_pages(data))
        self.assertEqual(pages[-1].header_type & 0x04, 0, "fixture must have no EOS flag")
        navi.assert_complete_opus(data)

    def test_rejects_a_truncated_payload(self):
        data = build_opus_stream()
        with self.assertRaises(navi.OggError):
            navi.assert_complete_opus(data[: len(data) - 100])

    def test_rejects_an_unfinished_final_packet(self):
        data = bytearray(build_opus_stream())
        # Append a page whose lacing promises a 260-byte packet, then cut it short.
        sequence = len(list(navi.iter_ogg_pages(bytes(data))))
        data += build_ogg_page(0x1234ABCD, sequence, 960, 0x00, [255, 5], bytes(260))[: 27 + 2 + 10]
        with self.assertRaises(navi.OggError):
            navi.assert_complete_opus(bytes(data))

    def test_rejects_a_corrupt_page(self):
        data = bytearray(build_opus_stream())
        data[40] ^= 0xFF
        with self.assertRaises(navi.OggError):
            navi.assert_complete_opus(bytes(data))

    def test_rejects_a_header_only_stream(self):
        head = build_opus_head()
        page = build_ogg_page(1, 0, 0, 0x02, lace(head), head)
        with self.assertRaises(navi.OggError):
            navi.assert_complete_opus(page)

    def test_rejects_a_stream_ending_with_an_unfinished_page(self):
        serial = 0x1234ABCD
        out = bytearray()
        head = build_opus_head()
        tags = build_opus_tags("v", [])
        for page in paginate([head], serial, 0):
            out += page
        for page in paginate([tags], serial, 1, bos=False):
            out += page
        # A page that completes no packet is marked with granule -1.
        out += build_ogg_page(serial, 3, -1, 0x00, [255, 5], bytes(260))
        with self.assertRaises(navi.OggError):
            navi.assert_complete_opus(bytes(out))


class TestEmbedCoverArt(unittest.TestCase):
    def test_parses_synthetic_opus(self):
        stream = build_opus_stream()
        pages = list(navi.iter_ogg_pages(stream))
        (head, tags), consumed, serial = navi._consume_header_packets(iter(pages))
        self.assertTrue(head.startswith(b"OpusHead"))
        self.assertTrue(tags.startswith(b"OpusTags"))
        self.assertEqual(consumed, 2)
        self.assertEqual(serial, 0x1234ABCD)
        self.assertEqual(navi.sniff_audio_format(stream[:64]), "opus")

    def test_inject_and_verify(self):
        stream = build_opus_stream()
        cover = make_jpeg(1000, 1000)
        before_audio = audio_payload_bytes(stream)

        rewritten, info = navi.embed_cover_art(stream, cover)
        report = navi.verify_opus_with_cover(rewritten, cover)

        self.assertEqual(info["width"], 1000)
        self.assertEqual(info["height"], 1000)
        self.assertEqual(info["mime"], "image/jpeg")
        self.assertEqual(info["picture_type"], 3)
        self.assertEqual(report["picture"]["image"] if "image" in report["picture"] else cover, cover)
        self.assertEqual(audio_payload_bytes(rewritten), before_audio, "audio payload must be untouched")
        self.assertGreater(len(rewritten), len(stream))

    def test_preserves_existing_comments(self):
        stream = build_opus_stream(tags_comments=(("TITLE", "Keep Me"), ("ALBUM", "Also Me")))
        cover = make_png(64, 64)
        rewritten, _ = navi.embed_cover_art(stream, cover)
        (_head, tags), _consumed, _serial = navi._consume_header_packets(iter(navi.iter_ogg_pages(rewritten)))
        vendor, comments = navi.parse_opus_tags(tags)
        self.assertEqual(vendor, "navifavsync-tests")
        keys = [key for key, _ in comments]
        self.assertIn("TITLE", keys)
        self.assertIn("ALBUM", keys)
        self.assertIn("METADATA_BLOCK_PICTURE", keys)
        self.assertEqual(dict(comments)["TITLE"], "Keep Me")

    def test_replaces_an_existing_picture(self):
        stream = build_opus_stream()
        first, _ = navi.embed_cover_art(stream, make_jpeg(100, 100))
        second, info = navi.embed_cover_art(first, make_png(200, 150))
        (_head, tags), _consumed, _serial = navi._consume_header_packets(iter(navi.iter_ogg_pages(second)))
        _vendor, comments = navi.parse_opus_tags(tags)
        pictures = [value for key, value in comments if key == "METADATA_BLOCK_PICTURE"]
        self.assertEqual(len(pictures), 1, "the previous picture must be replaced, not appended")
        self.assertEqual(info["mime"], "image/png")
        navi.verify_opus_with_cover(second, make_png(200, 150))

    def test_large_cover_forces_multi_page_header(self):
        """A ~400 KiB cover makes OpusTags span four pages; the audio pages
        after it must be renumbered contiguously and keep valid CRCs."""
        stream = build_opus_stream()
        cover = make_jpeg(1000, 1000, payload_size=400_000)
        # The original header is OpusHead on one page plus OpusTags on one.
        before_pages = len(list(navi.iter_ogg_pages(stream)))
        before_audio_pages = before_pages - 2
        before_audio = audio_payload_bytes(stream)

        rewritten, info = navi.embed_cover_art(stream, cover)
        after_pages = list(navi.iter_ogg_pages(rewritten))
        self.assertEqual(audio_payload_bytes(rewritten), before_audio)

        (_head, tags), consumed, serial = navi._consume_header_packets(iter(after_pages))
        self.assertGreater(consumed, 2, "fixture should have produced a multi-page comment header")
        self.assertEqual(serial, 0x1234ABCD)
        for index, page in enumerate(after_pages):
            self.assertEqual(page.sequence, index)
            self.assertEqual(page.serial, serial)
        # Only the header may have grown; the audio page count is fixed.
        self.assertEqual(len(after_pages) - consumed, before_audio_pages)
        self.assertEqual(len(after_pages), before_pages - 2 + consumed)
        navi.verify_opus_with_cover(rewritten, cover)
        self.assertEqual(info["width"], 1000)

    def test_header_spanning_pages_round_trip(self):
        stream = build_opus_stream(tags_padding=200_000)
        self.assertGreater(len(list(navi.iter_ogg_pages(stream))), 4, "fixture must span several pages")
        cover = make_jpeg(500, 500, payload_size=200_000)
        before_audio = audio_payload_bytes(stream)
        rewritten, _ = navi.embed_cover_art(stream, cover)
        self.assertEqual(audio_payload_bytes(rewritten), before_audio)
        navi.verify_opus_with_cover(rewritten, cover)

    def test_every_image_format_embeds(self):
        for label, cover in (
            ("jpeg", make_jpeg(800, 450)),
            ("png", make_png(300, 300, bit_depth=8, color_type=6)),
            ("webp-lossy", make_webp_lossy(640, 480)),
            ("webp-lossless", make_webp_lossless(300, 200)),
            ("webp-extended", make_webp_extended(1024, 768)),
        ):
            with self.subTest(format=label):
                stream = build_opus_stream()
                rewritten, info = navi.embed_cover_art(stream, cover)
                navi.verify_opus_with_cover(rewritten, cover)
                self.assertIn(info["mime"], ("image/jpeg", "image/png", "image/webp"))

    def test_rejects_unknown_image(self):
        stream = build_opus_stream()
        with self.assertRaises(navi.OggError):
            navi.embed_cover_art(stream, b"not an image at all")

    def test_rejects_corrupt_stream(self):
        data = bytearray(build_opus_stream())
        data[30] ^= 0xFF
        with self.assertRaises(navi.OggError):
            navi.embed_cover_art(bytes(data), make_jpeg())

    def test_verify_detects_tampered_picture(self):
        stream = build_opus_stream()
        rewritten, _ = navi.embed_cover_art(stream, make_jpeg(100, 100))
        with self.assertRaises(navi.OggError):
            navi.verify_opus_with_cover(rewritten, make_jpeg(101, 101))

    def test_verify_detects_swapped_picture_bytes(self):
        stream = build_opus_stream()
        rewritten, _ = navi.embed_cover_art(stream, make_jpeg(100, 100))
        tampered = bytearray(rewritten)
        # Flip a byte deep inside the base64 payload; the page CRC must catch it.
        tampered[80] ^= 0x01
        with self.assertRaises(navi.OggError):
            navi.verify_opus_with_cover(bytes(tampered), make_jpeg(100, 100))

    def test_paginate_matches_reference_crc(self):
        pages = navi.paginate_packets([b"x" * 70000, b"y" * 10], 0xABCD)
        self.assertEqual(len(pages), 2)
        for index, page in enumerate(pages):
            raw = page
            stored = struct.unpack_from("<I", raw, 22)[0]
            zeroed = raw[:22] + b"\x00\x00\x00\x00" + raw[26:]
            self.assertEqual(stored, reference_ogg_crc(zeroed))
            self.assertEqual(struct.unpack_from("<I", raw, 18)[0], index)
        self.assertTrue(pages[0][5] & 0x02)
        self.assertTrue(pages[1][5] & 0x01, "second page continues the oversized packet")


# ---------------------------------------------------------------------------
# 3. Sniffing
# ---------------------------------------------------------------------------


class TestSniffImage(unittest.TestCase):
    def test_jpeg(self):
        mime, width, height, depth = navi.sniff_image(make_jpeg(1024, 768))
        self.assertEqual((mime, width, height), ("image/jpeg", 1024, 768))
        self.assertEqual(depth, 24)

    def test_png_rgb(self):
        mime, width, height, depth = navi.sniff_image(make_png(64, 32, bit_depth=8, color_type=2))
        self.assertEqual((mime, width, height), ("image/png", 64, 32))
        self.assertEqual(depth, 24)

    def test_png_rgba(self):
        _mime, _w, _h, depth = navi.sniff_image(make_png(64, 32, bit_depth=16, color_type=6))
        self.assertEqual(depth, 64)

    def test_webp_variants(self):
        self.assertEqual(navi.sniff_image(make_webp_lossy(640, 480))[:3], ("image/webp", 640, 480))
        self.assertEqual(navi.sniff_image(make_webp_lossless(300, 200))[:3], ("image/webp", 300, 200))
        self.assertEqual(navi.sniff_image(make_webp_extended(1024, 768))[:3], ("image/webp", 1024, 768))

    def test_unknown(self):
        self.assertIsNone(navi.sniff_image(b"\x00\x01\x02\x03"))
        self.assertIsNone(navi.sniff_image(b""))


class TestSniffAudio(unittest.TestCase):
    def test_opus(self):
        self.assertEqual(navi.sniff_audio_format(build_opus_stream()[:64]), "opus")

    def test_containers(self):
        cases = {
            b"fLaC\x00\x00\x00\x22": "flac",
            b"ID3\x04\x00\x00\x00\x00\x00\x00": "mp3",
            b"\xff\xfb\x90\x00" + bytes(8): "mp3",
            b"\xff\xf1\x50\x80" + bytes(8): "aac",
            b"\xff\xe0" + bytes(8): "aac",
            b"RIFF\x00\x00\x00\x00WAVEfmt ": "wav",
            b"\x1a\x45\xdf\xa3" + bytes(8): "matroska",
            b"\x00\x00\x00\x18ftypM4A ": "mp4",
            b"FORM\x00\x00\x00\x00AIFF": "aiff",
            b"OggS" + bytes(60): "ogg",
            b"garbage!": "unknown",
            b"": "unknown",
        }
        for prefix, expected in cases.items():
            with self.subTest(prefix=prefix[:8]):
                self.assertEqual(navi.sniff_audio_format(prefix), expected)

    def test_vorbis_detected(self):
        # Layout: "OggS", 22 bytes of fixed header fields, nseg, the segment
        # table, then the payload, whose first byte is the Vorbis packet type.
        head = b"OggS" + bytes(22) + bytes((1, 30)) + b"\x01vorbis\x00\x00"
        self.assertEqual(navi.sniff_audio_format(head), "vorbis")
        head = b"OggS" + bytes(22) + bytes((1, 19)) + build_opus_head()
        self.assertEqual(navi.sniff_audio_format(head), "opus")

    def test_opus_detected_with_a_full_length_segment_table(self):
        """Regression: embedding cover art moves the whole comment packet onto
        the first page, so the codec tag sits past a 64-byte prefix."""
        stream = build_opus_stream()
        rewritten, _ = navi.embed_cover_art(stream, make_jpeg(1000, 1000, payload_size=200_000))
        head = rewritten[: navi.SNIFF_PREFIX_BYTES]
        self.assertEqual(head[26], 255, "fixture should fill the first segment table")
        self.assertEqual(navi.sniff_audio_format(head), "opus")
        # A short prefix legitimately cannot decide.
        self.assertEqual(navi.sniff_audio_format(rewritten[:64]), "ogg")

    def test_opus_detected_with_a_short_prefix(self):
        self.assertEqual(navi.sniff_audio_format(build_opus_stream()[:64]), "opus")


class TestSniffersAreNotConfused(unittest.TestCase):
    def test_mp3_with_id3_and_sync(self):
        # An ID3 tag followed by a real MPEG frame.
        data = b"ID3\x04\x00\x00\x00\x00\x02\x00" + b"\xff\xfb\x90\x64" + bytes(64)
        self.assertEqual(navi.sniff_audio_format(data), "mp3")

    def test_flac_is_not_mistaken_for_opus(self):
        self.assertNotEqual(navi.sniff_audio_format(b"fLaC" + bytes(60)), "opus")


# ---------------------------------------------------------------------------
# 4. Filenames
# ---------------------------------------------------------------------------


class TestFilenames(unittest.TestCase):
    def test_sanitize_illegal_characters(self):
        self.assertEqual(navi.sanitize_component('a/b\\c:d*e?f"g<h>i|j'), "a_b_c_d_e_f_g_h_i_j")

    def test_sanitize_control_characters(self):
        self.assertEqual(navi.sanitize_component("bad\x00\x1fname"), "bad_name")

    def test_sanitize_trailing_dots_and_spaces(self):
        self.assertEqual(navi.sanitize_component("name.  "), "name")

    def test_sanitize_reserved_windows_names(self):
        self.assertEqual(navi.sanitize_component("CON"), "_CON")
        self.assertEqual(navi.sanitize_component("com1.txt"), "_com1.txt")
        self.assertEqual(navi.sanitize_component("LPT9"), "_LPT9")

    def test_sanitize_empty_and_hidden(self):
        self.assertEqual(navi.sanitize_component(""), "Unknown")
        self.assertEqual(navi.sanitize_component("   "), "Unknown")
        self.assertEqual(navi.sanitize_component(".hidden"), "_.hidden")

    def test_sanitize_length_cap(self):
        result = navi.sanitize_component("x" * 500)
        self.assertLessEqual(len(result), navi.MAX_NAME_LEN)

    def test_sanitize_collapses_whitespace(self):
        self.assertEqual(navi.sanitize_component("a   b\t\tc"), "a b c")

    def test_album_dir_name(self):
        song = {"artist": "Miles Davis", "album": "Kind of Blue"}
        self.assertEqual(navi.build_album_dir_name(song), "Miles Davis - Kind of Blue")

    def test_album_dir_name_with_slashes(self):
        song = {"artist": "AC/DC", "album": "Back in Black"}
        self.assertEqual(navi.build_album_dir_name(song), "AC_DC - Back in Black")

    def test_album_dir_name_missing_fields(self):
        self.assertEqual(navi.build_album_dir_name({}), "Unknown Artist - Unknown Album")

    def test_track_filename(self):
        song = {"title": "So What", "track": 1, "discNumber": 1}
        self.assertEqual(navi.build_track_filename(song, ".opus"), "01 - So What.opus")

    def test_track_filename_multidisc(self):
        song = {"title": "Movement", "track": 3, "discNumber": 2}
        self.assertEqual(navi.build_track_filename(song, ".opus"), "02-03 - Movement.opus")

    def test_track_filename_without_track_number(self):
        self.assertEqual(navi.build_track_filename({"title": "Untitled"}, ".opus"), "Untitled.opus")

    def test_track_filename_string_numbers(self):
        song = {"title": "Song", "track": "7", "discNumber": "1"}
        self.assertEqual(navi.build_track_filename(song, ".opus"), "07 - Song.opus")

    def test_unique_path_avoids_collisions(self):
        """Manifest.reserve hands out a free name when one is already taken."""
        with tempfile.TemporaryDirectory() as tmp:
            manifest = navi.Manifest(Path(tmp) / "manifest.jsonl")
            base = Path(tmp) / "01 - Song.opus"
            self.assertEqual(manifest.reserve(base, "song-a"), base)
            self.assertEqual(manifest.reserve(base, "song-b").name, "01 - Song (2).opus")
            self.assertEqual(manifest.reserve(base, "song-c").name, "01 - Song (3).opus")
            # The same song re-asking keeps its original name.
            self.assertEqual(manifest.reserve(base, "song-a"), base)
            self.assertEqual(manifest.owner_of(base), "song-a")

    def test_reserve_reloads_from_existing_manifest(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "manifest.jsonl"
            first = navi.Manifest(path)
            target = Path(tmp) / "01 - Song.opus"
            first.reserve(target, "song-a")
            first.write({"status": "downloaded", "path": str(target), "id": "song-a"})

            second = navi.Manifest(path)
            self.assertEqual(second.owner_of(target), "song-a")
            self.assertEqual(second.reserve(target, "song-b").name, "01 - Song (2).opus")

    def test_reserve_skips_names_present_on_disk(self):
        with tempfile.TemporaryDirectory() as tmp:
            manifest = navi.Manifest(Path(tmp) / "manifest.jsonl")
            base = Path(tmp) / "01 - Song.opus"
            # The canonical name is spoken for by another track, and both it and
            # the first suffix already exist on disk.
            manifest.reserve(base, "song-z")
            base.write_bytes(b"x")
            (base.parent / "01 - Song (2).opus").write_bytes(b"x")
            self.assertEqual(manifest.reserve(base, "song-a").name, "01 - Song (3).opus")

    def test_reserve_overwrites_own_file(self):
        """--force must reuse this song's own path rather than adding a suffix."""
        with tempfile.TemporaryDirectory() as tmp:
            manifest = navi.Manifest(Path(tmp) / "manifest.jsonl")
            base = Path(tmp) / "01 - Song.opus"
            base.write_bytes(b"x")
            manifest.reserve(base, "song-a")
            self.assertEqual(manifest.reserve(base, "song-a"), base)


class TestRedactUrl(unittest.TestCase):
    def test_masks_credentials(self):
        url = "https://host/rest/stream.view?u=alice&v=1.16.1&t=deadbeef&s=abcd&format=opus"
        masked = navi.redact_url(url)
        self.assertNotIn("deadbeef", masked)
        self.assertIn("t=REDACTED", masked)
        self.assertIn("u=alice", masked)
        self.assertIn("format=opus", masked)

    def test_masks_plaintext_password_param(self):
        masked = navi.redact_url("https://host/rest/ping.view?u=a&p=hunter2")
        self.assertNotIn("hunter2", masked)

    def test_leaves_clean_urls_alone(self):
        url = "https://host/rest/ping.view?u=alice"
        self.assertEqual(navi.redact_url(url), url)


# ---------------------------------------------------------------------------
# 5. Mock Navidrome server
# ---------------------------------------------------------------------------

PASSWORD = "correct horse battery staple"
USERNAME = "alice"
COVER = make_jpeg(1000, 1000, payload_size=2000)


def song_entry(
    song_id: str,
    title: str,
    track: int,
    album="Fixture Album",
    artist="Fixture Artist",
    album_id="al-1",
    cover_art="cover-1",
) -> dict:
    return {
        "id": song_id,
        "parent": album_id,
        "isDir": False,
        "title": title,
        "album": album,
        "artist": artist,
        "track": track,
        "discNumber": 1,
        "year": 2024,
        "genre": "Test",
        "size": 4_000_000,
        "suffix": "flac",
        "contentType": "audio/flac",
        "duration": 245,
        "bitRate": 900,
        "path": f"/music/{album}/" + title + ".flac",
        "albumId": album_id,
        "artistId": "ar-1",
        "coverArt": cover_art,
        "type": "music",
        "isVideo": False,
    }


def album_fixture(album_id, name, artist, tracks) -> dict:
    """One entry for the mock's album list, as getAlbumList2 would report it."""
    return {
        "id": album_id,
        "isDir": True,
        "name": name,
        "album": name,
        "artist": artist,
        "albumArtist": artist,
        "songCount": len(tracks),
        "duration": sum(t.get("duration", 0) for t in tracks),
        "year": 2024,
        "genre": "Test",
        "coverArt": "cover-1",
        "created": "2024-01-01T00:00:00Z",
        "songs": tracks,
    }


class MockNavidromeHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "Navidrome/0.99.0"
    sys_version = ""

    # -- plumbing --

    def log_message(self, *args):  # keep the test output readable
        pass

    @property
    def state(self):
        return self.server.state

    def _send_json(self, payload, status=200):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_bytes(self, body, content_type, status=200, extra_headers=()):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for key, value in extra_headers:
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def _subsonic_ok(self, body=None):
        payload = {
            "subsonic-response": {
                "status": "ok",
                "version": "1.16.1",
                "type": "Navidrome",
                "serverVersion": "0.99.0",
            }
        }
        if body:
            payload["subsonic-response"].update(body)
        self._send_json(payload)

    def _subsonic_failed(self, code, message, status=200):
        self._send_json(
            {"subsonic-response": {"status": "failed", "version": "1.16.1", "error": {"code": code, "message": message}}},
            status=status,
        )

    def _authenticated(self, params) -> bool:
        username = params.get("u")
        salt = params.get("s", "")
        token = params.get("t", "")
        if username != USERNAME or not salt or not token:
            return False
        expected = hashlib.md5((PASSWORD + salt).encode("utf-8")).hexdigest()
        return token == expected

    # -- routing --

    def do_GET(self):
        parts = urllib.parse.urlsplit(self.path)
        endpoint = parts.path.rsplit("/", 1)[-1]
        if endpoint.endswith(".view"):
            endpoint = endpoint[: -len(".view")]
        params = dict(urllib.parse.parse_qsl(parts.query, keep_blank_values=True))
        self.state["requests"].append((endpoint, params))

        if params.get("v") != "1.16.1" or params.get("c") != "navifavsync" or params.get("f") != "json":
            self._subsonic_failed(10, "Required parameter is missing")
            return
        if not self._authenticated(params):
            self.state["auth_failures"] += 1
            self._subsonic_failed(40, "Wrong username or password", status=401)
            return

        handler = {
            "ping": self._handle_ping,
            "getStarred2": self._handle_starred,
            "getAlbumList2": self._handle_album_list2,
            "getAlbum": self._handle_album,
            "getSong": self._handle_song,
            "search3": self._handle_search3,
            "getCoverArt": self._handle_cover,
            "stream": self._handle_stream,
        }.get(endpoint)
        if handler is None:
            self._subsonic_failed(0, f"unknown endpoint {endpoint}", status=404)
            return
        handler(params)

    def _handle_ping(self, params):
        self._subsonic_ok()

    def _handle_starred(self, params):
        songs = [song_entry(song_id, title, track) for song_id, title, track in self.state["library"]]
        # Navidrome also returns starred albums and artists; they must be filtered out.
        self._subsonic_ok(
            {
                "starred2": {
                    "album": [{"id": "al-1", "isDir": True, "name": "Fixture Album"}],
                    "song": songs,
                }
            }
        )

    def _handle_album_list2(self, params):
        if self.state["behaviours"].get("getAlbumList2") == "server-error":
            self._subsonic_failed(0, "Internal Server Error: boom", status=500)
            return
        if params.get("type") != "alphabeticalByName":
            self._subsonic_failed(0, f"unexpected album list type {params.get('type')}")
            return
        try:
            size = int(params.get("size", 10))
            offset = int(params.get("offset", 0))
        except ValueError:
            self._subsonic_failed(10, "size/offset must be integers")
            return
        window = self.state["albums"][offset : offset + size]
        # A real server reports album metadata only; tracks come from getAlbum.
        listed = [{key: value for key, value in album.items() if key != "songs"} for album in window]
        self._subsonic_ok({"albumList2": {"album": listed}})

    def _handle_album(self, params):
        album_id = params.get("id", "")
        behaviour = self.state["behaviours"].get(album_id)
        if behaviour == "server-error":
            self._subsonic_failed(0, "Internal Server Error: boom", status=500)
            return
        if behaviour == "gone":
            self._subsonic_failed(70, "Album not found", status=404)
            return
        for album in self.state["albums"]:
            if album["id"] != album_id:
                continue
            payload = {key: value for key, value in album.items() if key != "songs"}
            payload["song"] = album["songs"]
            self._subsonic_ok({"album": payload})
            return
        self._subsonic_failed(70, "Album not found", status=404)

    def all_songs(self):
        """Every track the mock knows about, from the library and every album."""
        songs = [song_entry(song_id, title, track) for song_id, title, track in self.state["library"]]
        for album in self.state["albums"]:
            songs.extend(album["songs"])
        seen = set()
        unique = []
        for song in songs:
            if song["id"] in seen:
                continue
            seen.add(song["id"])
            unique.append(song)
        return unique

    def _handle_song(self, params):
        song_id = params.get("id", "")
        behaviour = self.state["behaviours"].get(f"song:{song_id}")
        if behaviour == "server-error":
            self._subsonic_failed(0, "Internal Server Error: boom", status=500)
            return
        for song in self.all_songs():
            if song["id"] == song_id:
                # getSong answers with a bare object, not a list.
                self._subsonic_ok({"song": song})
                return
        self._subsonic_failed(70, "Song not found", status=404)

    def _handle_search3(self, params):
        query = (params.get("query") or "").casefold()
        try:
            count = int(params.get("songCount", 20))
            offset = int(params.get("songOffset", 0))
        except ValueError:
            self._subsonic_failed(10, "songCount/songOffset must be integers")
            return
        matches = []
        for song in self.all_songs():
            haystack = " ".join(
                str(song.get(key) or "") for key in ("title", "album", "artist")
            ).casefold()
            if query in haystack:
                matches.append(song)
        window = matches[offset : offset + count]
        self._subsonic_ok(
            {
                "searchResult3": {
                    "song": window,
                    # Real search3 also returns matching artists and albums, which
                    # are not streams and must be ignored.
                    "artist": [{"id": "ar-1", "name": "Fixture Artist"}],
                    "album": [{"id": "al-1", "name": "Fixture Album"}],
                }
            }
        )

    def _handle_cover(self, params):
        if params.get("size") != str(self.state["cover_size"]):
            self._subsonic_failed(0, f"unexpected size {params.get('size')}")
            return
        self._send_bytes(COVER, "image/jpeg")

    def _handle_stream(self, params):
        song_id = params.get("id", "")
        fmt = params.get("format")
        bitrate = params.get("maxBitRate")
        self.state["stream_params"].append((song_id, fmt, bitrate))
        attempts = self.state["stream_attempts"].get(song_id, 0) + 1
        self.state["stream_attempts"][song_id] = attempts

        if fmt != "opus" or bitrate != "192":
            self._subsonic_failed(0, f"unexpected transcode request format={fmt} maxBitRate={bitrate}")
            return

        behaviour = self.state["behaviours"].get(song_id, "ok")
        opus = self.state["opus"]

        if behaviour == "ratelimit" and attempts <= self.state["ratelimit_count"]:
            self.send_response(429)
            self.send_header("Content-Type", "application/json")
            self.send_header("Retry-After", "0")
            body = json.dumps(
                {"subsonic-response": {"status": "failed", "error": {"code": 0, "message": "too many concurrent transcodes"}}}
            ).encode("utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if behaviour == "transcoder-fault":
            self._subsonic_failed(0, "Internal Server Error: Error starting transcoder: ffmpeg not found")
            return
        if behaviour == "gone":
            self._subsonic_failed(70, "data not found", status=404)
            return
        if behaviour == "server-error":
            self._subsonic_failed(0, "Internal Server Error: boom", status=500)
            return
        if behaviour == "unauthorized":
            self._subsonic_failed(40, "Wrong username or password", status=401)
            return
        if behaviour == "flac":
            # The server has no opus transcoding, so it streams the original.
            self._send_bytes(b"fLaC" + bytes(600), "audio/flac")
            return
        if behaviour == "error-200":
            # Subsonic failures are delivered with HTTP 200 and a JSON body.
            self._subsonic_failed(0, "too many concurrent transcodes, please retry shortly")
            return
        if behaviour == "truncate" and attempts == 1:
            # Promise more bytes than are delivered, then hang up.
            self.send_response(200)
            self.send_header("Content-Type", "audio/opus")
            self.send_header("Content-Length", str(len(opus) + 4096))
            self.end_headers()
            self.wfile.write(opus[: len(opus) // 2])
            self.wfile.flush()
            self.close_connection = True
            return
        if behaviour == "truncate-always":
            self.send_response(200)
            self.send_header("Content-Type", "audio/opus")
            self.send_header("Content-Length", str(len(opus) + 4096))
            self.end_headers()
            self.wfile.write(opus[: len(opus) // 2])
            self.wfile.flush()
            self.close_connection = True
            return
        if behaviour == "drop" and attempts == 1:
            self.send_response(200)
            self.send_header("Content-Type", "audio/opus")
            self.send_header("Content-Length", str(len(opus)))
            self.end_headers()
            self.wfile.write(opus[:16])
            self.wfile.flush()
            self.close_connection = True
            return
        if behaviour == "empty":
            self._send_bytes(b"", "audio/opus")
            return
        if behaviour == "vbr-estimate":
            # Mirrors real Navidrome: Content-Length is derived from the nominal
            # bitrate and overstates VBR libopus output by about 1%.
            self.send_response(200)
            self.send_header("Content-Type", "audio/ogg")
            self.send_header("Content-Length", str(int(len(opus) * 1.008)))
            self.end_headers()
            self.wfile.write(opus)
            return
        if behaviour == "silent-truncate":
            # Content-Length matches the body, so only the container structure
            # reveals that the stream was cut.
            body = opus[: len(opus) // 2]
            self.send_response(200)
            self.send_header("Content-Type", "audio/opus")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if behaviour == "hang":
            time.sleep(30)
            return

        self._send_bytes(opus, "audio/opus", extra_headers=(("X-Content-Duration", "245.0"),))


class MockServer:
    def __init__(self, library, behaviours=None, cover_size=1000, ratelimit_count=1, albums=None):
        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), MockNavidromeHandler)
        self.httpd.daemon_threads = True
        self.default_behaviours = dict(behaviours or {})
        self.default_ratelimit_count = ratelimit_count
        self.state = {
            "library": list(library),
            # By default the library is a single album holding the starred tracks,
            # so both download modes see the same fixture.
            "albums": list(albums)
            if albums is not None
            else [
                album_fixture(
                    "al-1",
                    "Fixture Album",
                    "Fixture Artist",
                    [song_entry(song_id, title, track) for song_id, title, track in library],
                )
            ],
            "behaviours": dict(self.default_behaviours),
            "opus": build_opus_stream(),
            "requests": [],
            "stream_attempts": {},
            "stream_params": [],
            "auth_failures": 0,
            "cover_size": cover_size,
            "ratelimit_count": ratelimit_count,
        }
        self.httpd.state = self.state
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    @property
    def url(self):
        host, port = self.httpd.server_address[:2]
        return f"http://{host}:{port}"

    def reset(self, overrides=None):
        """Restore per-test state so class-scoped servers stay independent."""
        self.state["behaviours"] = dict(self.default_behaviours)
        if overrides:
            self.state["behaviours"].update(overrides)
        self.state["stream_attempts"].clear()
        self.state["stream_params"].clear()
        self.state["requests"].clear()
        self.state["ratelimit_count"] = self.default_ratelimit_count

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=5)


# ---------------------------------------------------------------------------
# 6. End to end
# ---------------------------------------------------------------------------


class EndToEndTestCase(unittest.TestCase):
    behaviours: dict = {}
    library = (("ok-1", "First Track", 1),)
    albums: list = None
    extra_args: list = []
    ratelimit_count = 1

    @classmethod
    def setUpClass(cls):
        cls.server = MockServer(
            cls.library, cls.behaviours, ratelimit_count=cls.ratelimit_count, albums=cls.albums
        )

    @classmethod
    def tearDownClass(cls):
        cls.server.close()

    def stub(self, **behaviours):
        """Queue a per-test stream behaviour override, e.g. stub(flac_1='flac')."""
        self.pending_behaviours.update(behaviours)

    def setUp(self):
        self.pending_behaviours = {}
        # A run with a terminal asks which tracks to download, so every test here
        # must look like an unattended one -- including when the suite itself is
        # started from a terminal, where stdin really is a tty.
        tty = mock.patch.object(navi, "_stdin_is_tty", return_value=False)
        tty.start()
        self.addCleanup(tty.stop)

    def run_script(self, out_dir, *extra):
        self.server.reset(self.pending_behaviours)
        args = [
            "--url", self.server.url,
            "--user", USERNAME,
            "--password", PASSWORD,
            "--out", str(out_dir),
            "--log-level", "CRITICAL",
            "--retries", "4",
        ]
        args.extend(extra)
        args.extend(self.extra_args)
        return navi.main(args)

    def temp_dir(self):
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        return Path(holder.name)


class TestHappyPath(EndToEndTestCase):
    library = (("ok-1", "First Track", 1), ("ok-2", "Second Track", 2))

    def test_downloads_and_embeds(self):
        out = self.temp_dir()
        code = self.run_script(out)
        self.assertEqual(code, 0)

        album = out / "Fixture Artist - Fixture Album"
        self.assertTrue((album / "01 - First Track.opus").is_file())
        self.assertTrue((album / "02 - Second Track.opus").is_file())
        self.assertTrue((album / "cover.jpg").is_file())
        self.assertEqual((album / "cover.jpg").read_bytes(), COVER)

        track = (album / "01 - First Track.opus").read_bytes()
        report = navi.verify_opus_with_cover(track, COVER)
        self.assertEqual(report["picture"]["mime"], "image/jpeg")
        self.assertEqual(report["picture"]["width"], 1000)
        self.assertEqual(navi.sniff_audio_format(track[:64]), "opus")
        self.assertEqual(audio_payload_bytes(track), audio_payload_bytes(self.server.state["opus"]))

        self.assertEqual(self.server.state["stream_params"], [("ok-1", "opus", "192"), ("ok-2", "opus", "192")])

    def test_manifest_records_everything(self):
        out = self.temp_dir()
        self.assertEqual(self.run_script(out), 0)
        lines = (out / "manifest.jsonl").read_text().strip().splitlines()
        self.assertEqual(len(lines), 2)
        records = [json.loads(line) for line in lines]
        self.assertEqual({record["status"] for record in records}, {"downloaded"})
        for record in records:
            self.assertEqual(record["audio_format"], "opus")
            self.assertTrue(record["cover_embedded"])
            self.assertTrue(record["cover_file"])
            self.assertGreater(record["bytes"], 0)
            self.assertEqual(record["attempts"], 1)
            self.assertIsNone(record["error"])
            self.assertEqual(record["artist"], "Fixture Artist")

    def test_second_run_skips_everything(self):
        out = self.temp_dir()
        self.assertEqual(self.run_script(out), 0)
        self.server.state["stream_attempts"].clear()
        self.assertEqual(self.run_script(out), 0)
        self.assertEqual(self.server.state["stream_attempts"], {}, "no stream request should be made")
        lines = (out / "manifest.jsonl").read_text().strip().splitlines()
        self.assertEqual([json.loads(line)["status"] for line in lines], ["downloaded", "downloaded", "skipped", "skipped"])

    def test_force_redownloads(self):
        out = self.temp_dir()
        self.assertEqual(self.run_script(out), 0)
        self.assertEqual(self.run_script(out, "--force"), 0)
        self.assertEqual(len(self.server.state["stream_attempts"]), 2)

    def test_dry_run_writes_nothing(self):
        out = self.temp_dir()
        code = self.run_script(out, "--dry-run")
        self.assertEqual(code, 0)
        self.assertEqual(list(out.rglob("*")), [])
        self.assertEqual(self.server.state["stream_attempts"], {})

    def test_no_embed_leaves_file_alone(self):
        out = self.temp_dir()
        self.assertEqual(self.run_script(out, "--no-embed"), 0)
        track = out / "Fixture Artist - Fixture Album" / "01 - First Track.opus"
        self.assertTrue(track.is_file())
        self.assertFalse(navi._has_embedded_cover(track))
        self.assertTrue((out / "Fixture Artist - Fixture Album" / "cover.jpg").is_file())

    def test_no_cover_skips_fetch(self):
        out = self.temp_dir()
        self.assertEqual(self.run_script(out, "--no-cover"), 0)
        self.assertFalse((out / "Fixture Artist - Fixture Album" / "cover.jpg").exists())
        self.assertNotIn("getCoverArt", [endpoint for endpoint, _ in self.server.state["requests"]])

    def test_parallel_workers(self):
        out = self.temp_dir()
        self.assertEqual(self.run_script(out, "--workers", "3"), 0)
        album = out / "Fixture Artist - Fixture Album"
        self.assertEqual(len(list(album.glob("*.opus"))), 2)
        statuses = sorted(json.loads(line)["status"] for line in (out / "manifest.jsonl").read_text().strip().splitlines())
        self.assertEqual(statuses, ["downloaded", "downloaded"])

    def test_limit(self):
        out = self.temp_dir()
        self.assertEqual(self.run_script(out, "--limit", "1"), 0)
        self.assertEqual(len(list(out.rglob("*.opus"))), 1)


def multi_album_fixture():
    """Two albums by different artists, of the kind only the album walk returns."""
    return [
        album_fixture(
            "al-1",
            "First Album",
            "Artist One",
            [
                song_entry("ok-1", "One One", 1, "First Album", "Artist One", "al-1"),
                song_entry("ok-2", "One Two", 2, "First Album", "Artist One", "al-1"),
            ],
        ),
        album_fixture(
            "al-2",
            "Second Album",
            "Artist Two",
            [song_entry("ok-3", "Two One", 1, "Second Album", "Artist Two", "al-2", "cover-2")],
        ),
    ]


class TestFullLibrary(EndToEndTestCase):
    library = (("star-1", "Starred Only", 1),)
    albums = multi_album_fixture()

    def endpoints(self):
        return [endpoint for endpoint, _params in self.server.state["requests"]]

    def test_whole_library_is_downloaded_by_default(self):
        out = self.temp_dir()
        self.assertEqual(self.run_script(out), 0)

        self.assertTrue((out / "Artist One - First Album" / "01 - One One.opus").is_file())
        self.assertTrue((out / "Artist One - First Album" / "02 - One Two.opus").is_file())
        self.assertTrue((out / "Artist Two - Second Album" / "01 - Two One.opus").is_file())
        self.assertFalse((out / "Fixture Artist - Fixture Album").exists())
        self.assertEqual(len(list(out.rglob("*.opus"))), 3)

        endpoints = self.endpoints()
        self.assertIn("getAlbumList2", endpoints)
        self.assertNotIn("getStarred2", endpoints, "the whole library is the default, favorites are not consulted")
        self.assertEqual(
            [song_id for song_id, _fmt, _bitrate in self.server.state["stream_params"]],
            ["ok-1", "ok-2", "ok-3"],
        )

    def test_manifest_covers_every_album(self):
        out = self.temp_dir()
        self.assertEqual(self.run_script(out), 0)
        records = [json.loads(line) for line in (out / "manifest.jsonl").read_text().strip().splitlines()]
        self.assertEqual(len(records), 3)
        self.assertEqual(
            sorted(record["album"] for record in records), ["First Album", "First Album", "Second Album"]
        )
        self.assertTrue(all(record["status"] == "downloaded" for record in records))

    def test_favorites_only_ignores_the_rest_of_the_library(self):
        out = self.temp_dir()
        self.assertEqual(self.run_script(out, "--favorites-only"), 0)

        self.assertTrue((out / "Fixture Artist - Fixture Album" / "01 - Starred Only.opus").is_file())
        self.assertFalse((out / "Artist One - First Album").exists())
        self.assertEqual(len(list(out.rglob("*.opus"))), 1)

        endpoints = self.endpoints()
        self.assertIn("getStarred2", endpoints)
        self.assertNotIn("getAlbumList2", endpoints, "--favorites-only must not scan the library")
        self.assertNotIn("getAlbum", endpoints, "--favorites-only must not scan the library")

    def test_paging_covers_every_album(self):
        original = navi.ALBUM_PAGE_SIZE
        navi.ALBUM_PAGE_SIZE = 2
        self.addCleanup(setattr, navi, "ALBUM_PAGE_SIZE", original)

        out = self.temp_dir()
        self.assertEqual(self.run_script(out), 0)
        self.assertEqual(len(list(out.rglob("*.opus"))), 3)

        album_pages = [params for endpoint, params in self.server.state["requests"] if endpoint == "getAlbumList2"]
        self.assertEqual([(page["size"], page["offset"]) for page in album_pages], [("2", "0"), ("2", "2")])
        album_reads = [params["id"] for endpoint, params in self.server.state["requests"] if endpoint == "getAlbum"]
        self.assertEqual(album_reads, ["al-1", "al-2"])

    def test_albums_with_no_playable_tracks_are_skipped(self):
        self.server.state["albums"].append(
            album_fixture("al-3", "Booklet", "Various", [{"id": "note-1", "isDir": False, "title": "Notes"}])
        )
        self.addCleanup(self.server.state["albums"].pop)

        out = self.temp_dir()
        self.assertEqual(self.run_script(out), 0)
        self.assertEqual(len(list(out.rglob("*.opus"))), 3)
        self.assertFalse((out / "Various - Booklet").exists())

    def test_duplicate_tracks_are_only_downloaded_once(self):
        self.server.state["albums"].append(
            album_fixture(
                "al-4",
                "First Album (Compilation)",
                "Artist One",
                [song_entry("ok-1", "One One", 1, "First Album (Compilation)", "Artist One", "al-4")],
            )
        )
        self.addCleanup(self.server.state["albums"].pop)

        out = self.temp_dir()
        self.assertEqual(self.run_script(out), 0)
        self.assertEqual(len(list(out.rglob("*.opus"))), 3)
        self.assertEqual(
            sorted(song_id for song_id, _fmt, _bitrate in self.server.state["stream_params"]),
            ["ok-1", "ok-2", "ok-3"],
        )

    def test_limit_stops_the_scan_early(self):
        out = self.temp_dir()
        self.assertEqual(self.run_script(out, "--limit", "1"), 0)
        self.assertEqual(len(list(out.rglob("*.opus"))), 1)
        self.assertEqual(
            [params["id"] for endpoint, params in self.server.state["requests"] if endpoint == "getAlbum"],
            ["al-1"],
            "--limit must not keep reading albums once it has enough tracks",
        )

    def test_unreadable_album_is_skipped_and_reported(self):
        self.stub(**{"al-2": "server-error"})
        out = self.temp_dir()
        code = self.run_script(out)
        self.assertEqual(code, 1, "an incomplete mirror has to be visible in the exit code")

        self.assertTrue((out / "Artist One - First Album" / "01 - One One.opus").is_file())
        self.assertEqual(len(list(out.rglob("*.opus"))), 2)

    def test_vanished_album_is_skipped(self):
        self.stub(**{"al-2": "gone"})
        out = self.temp_dir()
        self.assertEqual(self.run_script(out), 1)
        self.assertEqual(len(list(out.rglob("*.opus"))), 2)

    def test_album_list_failure_stops_before_downloading(self):
        self.stub(**{"getAlbumList2": "server-error"})
        out = self.temp_dir()
        self.assertEqual(self.run_script(out), 2)
        self.assertEqual(list(out.rglob("*")), [])


class TestEmptyLibrary(EndToEndTestCase):
    library = ()
    albums = []

    def test_no_tracks_exits_cleanly(self):
        out = self.temp_dir()
        self.assertEqual(self.run_script(out), 0)
        self.assertFalse((out / "manifest.jsonl").exists())

    def test_no_favorites_exits_cleanly(self):
        out = self.temp_dir()
        self.assertEqual(self.run_script(out, "--favorites-only"), 0)
        self.assertFalse((out / "manifest.jsonl").exists())


class TestFavoritesOnlyHappyPath(EndToEndTestCase):
    library = (("ok-1", "First Track", 1), ("ok-2", "Second Track", 2))
    albums = multi_album_fixture()
    extra_args = ["--favorites-only"]

    def test_only_starred_tracks_are_downloaded(self):
        out = self.temp_dir()
        self.assertEqual(self.run_script(out), 0)
        album = out / "Fixture Artist - Fixture Album"
        self.assertTrue((album / "01 - First Track.opus").is_file())
        self.assertTrue((album / "02 - Second Track.opus").is_file())
        self.assertEqual(len(list(out.rglob("*.opus"))), 2)
        self.assertNotIn("getAlbumList2", [endpoint for endpoint, _ in self.server.state["requests"]])

    def test_limit_applies_to_favorites(self):
        out = self.temp_dir()
        self.assertEqual(self.run_script(out, "--limit", "1"), 0)
        self.assertEqual(len(list(out.rglob("*.opus"))), 1)


# ---------------------------------------------------------------------------
# 7. Choosing tracks
# ---------------------------------------------------------------------------


class SelectionTestCase(EndToEndTestCase):
    """Base for the scope tests: two albums, three tracks, one starred track."""

    library = (("star-1", "Starred Only", 1),)
    albums = multi_album_fixture()

    def endpoints(self):
        return [endpoint for endpoint, _params in self.server.state["requests"]]

    def streamed(self):
        return [song_id for song_id, _fmt, _bitrate in self.server.state["stream_params"]]


class TestSongIdSelection(SelectionTestCase):
    def test_one_id_downloads_only_that_track(self):
        out = self.temp_dir()
        self.assertEqual(self.run_script(out, "--song-id", "ok-2"), 0)

        self.assertTrue((out / "Artist One - First Album" / "02 - One Two.opus").is_file())
        self.assertEqual(len(list(out.rglob("*.opus"))), 1)
        self.assertEqual(self.streamed(), ["ok-2"])

    def test_no_library_scan_is_needed(self):
        out = self.temp_dir()
        self.assertEqual(self.run_script(out, "--song-id", "ok-3"), 0)
        # The point of --song-id: instant, even on a library too big to walk.
        for endpoint in ("getAlbumList2", "getAlbum", "getStarred2"):
            self.assertNotIn(endpoint, self.endpoints(), f"{endpoint} must not be called")
        self.assertEqual(self.endpoints().count("getSong"), 1)

    def test_repeated_and_comma_separated_ids(self):
        out = self.temp_dir()
        code = self.run_script(out, "--song-id", "ok-1,ok-2", "--song-id", "ok-3")
        self.assertEqual(code, 0)
        self.assertEqual(self.streamed(), ["ok-1", "ok-2", "ok-3"])
        self.assertEqual(len(list(out.rglob("*.opus"))), 3)

    def test_duplicate_ids_are_downloaded_once(self):
        out = self.temp_dir()
        self.assertEqual(self.run_script(out, "--song-id", "ok-1", "--song-id", "ok-1"), 0)
        self.assertEqual(self.streamed(), ["ok-1"])
        self.assertEqual(len(list(out.rglob("*.opus"))), 1)

    def test_blank_entries_in_a_comma_list_are_ignored(self):
        out = self.temp_dir()
        self.assertEqual(self.run_script(out, "--song-id", " , ok-1, "), 0)
        self.assertEqual(self.streamed(), ["ok-1"])

    def test_unknown_id_is_reported_and_the_rest_still_download(self):
        out = self.temp_dir()
        code = self.run_script(out, "--song-id", "ok-1", "--song-id", "nope")

        self.assertEqual(code, 1, "a missing requested track has to show in the exit code")
        self.assertTrue((out / "Artist One - First Album" / "01 - One One.opus").is_file())
        self.assertEqual(len(list(out.rglob("*.opus"))), 1)

    def test_every_id_unknown_exits_2(self):
        out = self.temp_dir()
        self.assertEqual(self.run_script(out, "--song-id", "nope", "--song-id", "also-nope"), 2)
        self.assertEqual(list(out.rglob("*")), [], "nothing may be written when nothing was resolved")

    def test_getsong_failure_with_nothing_resolved_exits_2(self):
        self.stub(**{"song:ok-1": "server-error"})
        out = self.temp_dir()
        self.assertEqual(self.run_script(out, "--song-id", "ok-1"), 2)
        self.assertEqual(list(out.rglob("*.opus")), [])

    def test_id_that_is_not_a_track_is_not_downloadable(self):
        # A directory entry the server knows about, but which is not a stream.
        self.server.state["albums"].append(
            album_fixture("al-9", "Booklet", "Various", [{"id": "note-1", "isDir": True, "title": "Notes"}])
        )
        self.addCleanup(self.server.state["albums"].pop)

        out = self.temp_dir()
        self.assertEqual(self.run_script(out, "--song-id", "note-1"), 2)
        self.assertEqual(list(out.rglob("*.opus")), [])

    def test_limit_cannot_drop_a_requested_id(self):
        out = self.temp_dir()
        self.assertEqual(self.run_script(out, "--song-id", "ok-3", "--limit", "1"), 0)
        self.assertEqual(self.streamed(), ["ok-3"])

    def test_manifest_records_the_requested_tracks_only(self):
        out = self.temp_dir()
        self.assertEqual(self.run_script(out, "--song-id", "ok-3"), 0)
        records = [json.loads(line) for line in (out / "manifest.jsonl").read_text().strip().splitlines()]
        self.assertEqual([record["id"] for record in records], ["ok-3"])
        self.assertEqual(records[0]["album"], "Second Album")


class TestSearchSelection(SelectionTestCase):
    def test_matches_are_downloaded(self):
        out = self.temp_dir()
        self.assertEqual(self.run_script(out, "--search", "second"), 0)

        self.assertTrue((out / "Artist Two - Second Album" / "01 - Two One.opus").is_file())
        self.assertEqual(len(list(out.rglob("*.opus"))), 1)
        self.assertEqual(self.streamed(), ["ok-3"])

    def test_no_library_scan_is_needed(self):
        out = self.temp_dir()
        self.assertEqual(self.run_script(out, "--search", "one"), 0)
        for endpoint in ("getAlbumList2", "getAlbum", "getStarred2"):
            self.assertNotIn(endpoint, self.endpoints(), f"{endpoint} must not be called")
        self.assertIn("search3", self.endpoints())

    def test_matching_artists_and_albums_are_not_streams(self):
        # search3 also returns artist and album hits; only songs can be streamed.
        out = self.temp_dir()
        self.assertEqual(self.run_script(out, "--search", "fixture"), 0)
        streamed = set(self.streamed())
        self.assertTrue(streamed, "the fixture query should match the fixture songs")
        self.assertNotIn("ar-1", streamed, "an artist hit is not a track")
        self.assertNotIn("al-1", streamed, "an album hit is not a track")
        self.assertTrue(streamed <= {"star-1", "ok-1", "ok-2", "ok-3"}, streamed)

    def test_results_are_paged(self):
        original = navi.SEARCH_PAGE_SIZE
        navi.SEARCH_PAGE_SIZE = 1
        self.addCleanup(setattr, navi, "SEARCH_PAGE_SIZE", original)

        out = self.temp_dir()
        # "one" matches all three titles, so paging has to walk the whole set.
        self.assertEqual(self.run_script(out, "--search", "one"), 0)
        pages = [
            (params["songCount"], params["songOffset"])
            for endpoint, params in self.server.state["requests"]
            if endpoint == "search3"
        ]
        self.assertEqual(pages, [("1", "0"), ("1", "1"), ("1", "2"), ("1", "3")])
        self.assertEqual(self.streamed(), ["ok-1", "ok-2", "ok-3"])

    def test_no_match_is_a_clean_no_op(self):
        out = self.temp_dir()
        self.assertEqual(self.run_script(out, "--search", "nothingmatchesthis"), 0)
        self.assertEqual(list(out.rglob("*")), [])
        self.assertFalse((out / "manifest.jsonl").exists())

    def test_search_is_case_insensitive(self):
        out = self.temp_dir()
        self.assertEqual(self.run_script(out, "--search", "SECOND"), 0)
        self.assertEqual(self.streamed(), ["ok-3"])


class TestLocalFilterSelection(SelectionTestCase):
    def test_artist_filter(self):
        out = self.temp_dir()
        self.assertEqual(self.run_script(out, "--artist", "artist two"), 0)
        self.assertTrue((out / "Artist Two - Second Album" / "01 - Two One.opus").is_file())
        self.assertEqual(self.streamed(), ["ok-3"])
        self.assertFalse((out / "Artist One - First Album").exists())

    def test_album_filter_is_a_substring_match(self):
        out = self.temp_dir()
        self.assertEqual(self.run_script(out, "--album", "cond al"), 0)
        self.assertEqual(self.streamed(), ["ok-3"])

    def test_filters_still_walk_the_library(self):
        out = self.temp_dir()
        self.assertEqual(self.run_script(out, "--artist", "Artist One"), 0)
        self.assertIn("getAlbumList2", self.endpoints())
        self.assertEqual(sorted(self.streamed()), ["ok-1", "ok-2"])

    def test_no_match_is_a_clean_no_op(self):
        out = self.temp_dir()
        self.assertEqual(self.run_script(out, "--artist", "nobody at all"), 0)
        self.assertEqual(list(out.rglob("*")), [])

    def test_limit_applies_to_the_matches(self):
        out = self.temp_dir()
        # The scan cannot stop early here, because a filter that never saw the
        # second album would answer a different question; --limit then takes the
        # first of the matches.
        self.assertEqual(self.run_script(out, "--artist", "artist one", "--limit", "1"), 0)
        self.assertEqual(len(list(out.rglob("*.opus"))), 1)
        self.assertEqual([params["id"] for endpoint, params in self.server.state["requests"]
                          if endpoint == "getAlbum"], ["al-1", "al-2"])


class TestInteractiveSelection(EndToEndTestCase):
    library = (("star-1", "Starred Only", 1),)
    albums = multi_album_fixture()

    def interactive(self, *answers):
        """Pretend stdin is a terminal, script the prompt, and capture the table.

        An answer may be an exception instance, which is raised instead -- that
        is how Ctrl-D at the prompt is simulated.
        """
        tty = mock.patch.object(navi, "_stdin_is_tty", return_value=True)
        tty.start()
        self.addCleanup(tty.stop)

        remaining = list(answers)

        def answer(_prompt=""):
            if not remaining:
                # A question the test did not expect must fail loudly, not hang.
                raise EOFError("no more scripted answers")
            scripted = remaining.pop(0)
            if isinstance(scripted, BaseException):
                raise scripted
            return scripted

        patcher = mock.patch("builtins.input", side_effect=answer)
        prompt = patcher.start()
        self.addCleanup(patcher.stop)

        # The candidate table is the prompt's own output, so it is captured and
        # asserted on rather than sprayed over the test report.
        self.prompt_output = io.StringIO()
        capture = contextlib.redirect_stdout(self.prompt_output)
        capture.__enter__()
        self.addCleanup(capture.__exit__, None, None, None)
        return prompt

    def streamed(self):
        return [song_id for song_id, _fmt, _bitrate in self.server.state["stream_params"]]

    def test_a_terminal_is_asked_and_only_chosen_tracks_download(self):
        prompt = self.interactive("2,3")
        out = self.temp_dir()
        self.assertEqual(self.run_script(out), 0)

        self.assertTrue(prompt.called, "a terminal run must ask before downloading")
        self.assertEqual(self.streamed(), ["ok-2", "ok-3"])
        self.assertFalse((out / "Artist One - First Album" / "01 - One One.opus").exists())
        self.assertEqual(len(list(out.rglob("*.opus"))), 2)

    def test_the_candidate_list_is_printed(self):
        self.interactive("1")
        out = self.temp_dir()
        self.assertEqual(self.run_script(out), 0)
        printed = self.prompt_output.getvalue()
        self.assertIn("One One", printed)
        self.assertIn("Artist One", printed)
        self.assertIn("ok-1", printed, "the id is what makes --song-id usable afterwards")
        self.assertIn("3 candidate(s)", printed)

    def test_all_answers_take_every_candidate(self):
        self.interactive("all")
        out = self.temp_dir()
        self.assertEqual(self.run_script(out), 0)
        self.assertEqual(len(list(out.rglob("*.opus"))), 3)

    def test_quitting_downloads_nothing(self):
        self.interactive("q")
        out = self.temp_dir()
        self.assertEqual(self.run_script(out), 0)
        self.assertEqual(list(out.rglob("*")), [])

    def test_an_unanswered_prompt_is_not_a_crash(self):
        self.interactive(EOFError())
        out = self.temp_dir()
        self.assertEqual(self.run_script(out), 0)
        self.assertEqual(list(out.rglob("*")), [])

    def test_a_bad_answer_is_rejected_and_asked_again(self):
        prompt = self.interactive("9", "nope", "1")
        out = self.temp_dir()
        self.assertEqual(self.run_script(out), 0)
        self.assertEqual(prompt.call_count, 3)
        self.assertEqual(self.streamed(), ["ok-1"])
        self.assertIn("Try again", self.prompt_output.getvalue())

    def test_all_skips_the_prompt_in_a_terminal(self):
        prompt = self.interactive()
        out = self.temp_dir()
        self.assertEqual(self.run_script(out, "--all"), 0)
        self.assertEqual(prompt.call_count, 0, "--all must not ask anything")
        self.assertEqual(len(list(out.rglob("*.opus"))), 3)

    def test_pick_is_the_same_prompt_explicitly(self):
        self.interactive("1")
        out = self.temp_dir()
        self.assertEqual(self.run_script(out, "--pick"), 0)
        self.assertEqual(self.streamed(), ["ok-1"])

    def test_pick_without_a_terminal_fails_loudly(self):
        out = self.temp_dir()
        self.assertEqual(self.run_script(out, "--pick"), 2)
        self.assertEqual(
            list(out.rglob("*")), [], "--pick must not quietly download the whole library instead"
        )

    def test_pick_lists_the_favorites_too(self):
        self.interactive("1")
        out = self.temp_dir()
        self.assertEqual(self.run_script(out, "--favorites-only"), 0)
        self.assertTrue((out / "Fixture Artist - Fixture Album" / "01 - Starred Only.opus").is_file())
        self.assertEqual(self.streamed(), ["star-1"])

    def test_the_prompt_ignores_limit_so_the_list_is_complete(self):
        self.interactive("all")
        out = self.temp_dir()
        # --limit still applies, but the list offered must be the whole library.
        self.assertEqual(self.run_script(out, "--limit", "2"), 0)
        self.assertEqual(len(list(out.rglob("*.opus"))), 2)

    def test_search_then_pick_is_one_command(self):
        self.interactive("1")
        out = self.temp_dir()
        self.assertEqual(self.run_script(out, "--search", "one", "--pick"), 0)

        # Only the three "one" hits were ever offered, and one was chosen.
        self.assertIn("One One", self.prompt_output.getvalue())
        self.assertNotIn("Starred Only", self.prompt_output.getvalue())
        self.assertEqual(self.streamed(), ["ok-1"])

    def test_filter_then_pick_offers_only_the_matches(self):
        self.interactive("1")
        out = self.temp_dir()
        self.assertEqual(self.run_script(out, "--artist", "artist two", "--pick"), 0)
        self.assertIn("Two One", self.prompt_output.getvalue())
        self.assertNotIn("One One", self.prompt_output.getvalue())
        self.assertEqual(self.streamed(), ["ok-3"])

    def test_a_terminal_asks_even_for_a_scope(self):
        self.interactive("all")
        out = self.temp_dir()
        self.assertEqual(self.run_script(out, "--search", "second"), 0)
        self.assertIn("1 candidate(s)", self.prompt_output.getvalue())
        self.assertEqual(self.streamed(), ["ok-3"])

    def test_an_empty_scope_is_not_asked_about(self):
        # Nothing to choose from: the reason matters more than a prompt.
        self.interactive()
        out = self.temp_dir()
        self.assertEqual(self.run_script(out, "--search", "nothingmatchesthis"), 0)
        self.assertEqual(list(out.rglob("*")), [])

    def test_song_id_is_never_asked_about(self):
        prompt = self.interactive()
        out = self.temp_dir()
        self.assertEqual(self.run_script(out, "--song-id", "ok-1"), 0)
        self.assertEqual(prompt.call_count, 0, "an explicit list of ids is the whole answer")
        self.assertEqual(self.streamed(), ["ok-1"])


class TestSelectionParsing(unittest.TestCase):
    def test_single_number(self):
        self.assertEqual(navi.parse_selection("2", 5), [1])

    def test_lists_and_ranges(self):
        self.assertEqual(navi.parse_selection("2,5", 8), [1, 4])
        self.assertEqual(navi.parse_selection("2 5", 8), [1, 4])
        self.assertEqual(navi.parse_selection("2-4", 8), [1, 2, 3])
        self.assertEqual(navi.parse_selection(" 2 , 4 - 5 , 8 ", 8), [1, 3, 4, 7])

    def test_reversed_range_is_accepted(self):
        self.assertEqual(navi.parse_selection("5-2", 8), [1, 2, 3, 4])

    def test_sentinel_answers(self):
        self.assertEqual(navi.parse_selection("all", 3), [0, 1, 2])
        self.assertEqual(navi.parse_selection("ALL", 3), [0, 1, 2])
        self.assertEqual(navi.parse_selection("none", 3), [])
        self.assertEqual(navi.parse_selection("q", 3), [])
        self.assertEqual(navi.parse_selection("quit", 3), [])

    def test_repeats_are_collapsed(self):
        self.assertEqual(navi.parse_selection("2,2,2-3", 5), [1, 2])

    def test_zero_does_not_wrap_to_the_last_track(self):
        with self.assertRaises(navi.SelectionError):
            navi.parse_selection("0", 5)
        with self.assertRaises(navi.SelectionError):
            navi.parse_selection("0-2", 5)

    def test_out_of_range_is_rejected(self):
        for answer in ("6", "2-6", "0"):
            with self.assertRaises(navi.SelectionError):
                navi.parse_selection(answer, 5)

    def test_nonsense_is_rejected(self):
        for answer in ("", "   ", "x", "1-2-3", "1-", "-1", "all please", "1,,x"):
            with self.assertRaises(navi.SelectionError, msg=answer):
                navi.parse_selection(answer, 5)

    def test_error_messages_are_shown_as_is(self):
        with self.assertRaises(navi.SelectionError) as caught:
            navi.parse_selection("99", 5)
        self.assertIn("5 candidate(s)", str(caught.exception))


class TestSelectionTable(unittest.TestCase):
    def songs(self, count, start=1):
        return [song_entry(f"id-{index}", f"Title {index}", index) for index in range(start, start + count)]

    def render(self, songs, page_size=None):
        buffer = io.StringIO()
        # "q" is a real answer, unlike "": an empty answer is rejected and asked
        # again, which would spin here forever.
        navi.prompt_for_tracks(songs, reader=lambda: "q", writer=buffer, page_size=page_size or navi.PICKER_PAGE_SIZE)
        return buffer.getvalue()

    def test_every_candidate_is_listed_once(self):
        printed = self.render(self.songs(5))
        for index in range(1, 6):
            self.assertIn(f"Title {index}", printed)
        self.assertIn("id-1", printed)
        self.assertEqual(printed.count("Title 1"), 1)
        self.assertIn("5 candidate(s)", printed)

    def test_long_lists_are_paged_not_truncated(self):
        printed = self.render(self.songs(450), page_size=200)
        # Three screens: 200 + 200 + 50, with the reader answering each pause.
        self.assertEqual(printed.count("press Enter for the next page"), 2)
        self.assertIn("-- 250 more candidate(s)", printed)
        self.assertIn("-- 50 more candidate(s)", printed)
        self.assertIn("Title 450", printed)

    def test_track_and_length_columns(self):
        printed = self.render([song_entry("id-1", "Song", 5)])
        self.assertIn("05", printed)
        self.assertIn("4:05", printed)

    def test_multidisc_and_unknown_positions(self):
        song = song_entry("id-1", "Song", 5)
        song["discNumber"] = 2
        buffer = io.StringIO()
        navi.prompt_for_tracks([song], reader=lambda: "q", writer=buffer)
        self.assertIn("02-05", buffer.getvalue())

        bare = {"id": "id-2", "title": "Bare"}
        buffer = io.StringIO()
        navi.prompt_for_tracks([bare], reader=lambda: "q", writer=buffer)
        printed = buffer.getvalue()
        self.assertIn("--", printed)
        self.assertIn("--:--", printed, "a track with no duration must not claim to be zero long")

    def test_nothing_to_choose_from_is_an_error(self):
        with self.assertRaises(navi.SelectionError):
            navi.prompt_for_tracks([], reader=lambda: "q", writer=io.StringIO())

    def test_wide_characters_do_not_break_the_columns(self):
        # CJK is one character but two terminal columns, so len() would misalign
        # any table mixing it with ASCII.
        songs = [
            song_entry("id-1", "白夜", 1, "白夜", "Reol"),
            song_entry("id-2", "Blue", 1, "Blue", "Reol"),
            song_entry("id-3", "Combine", 2, "Mixed", "Reol"),
        ]
        printed = self.render(songs)
        self.assertIn("白夜", printed)
        rows = [
            line
            for line in printed.splitlines()
            if line.strip() and "Which to download?" not in line
        ]
        self.assertEqual(len(rows), 3, printed)
        widths = {navi._display_width(row) for row in rows}
        self.assertEqual(len(widths), 1, f"columns are not aligned:\n{printed}")

    def test_display_width_and_fit(self):
        self.assertEqual(navi._display_width("abc"), 3)
        self.assertEqual(navi._display_width("白夜"), 4)
        self.assertEqual(navi._display_width("é"), 1)
        self.assertEqual(navi._fit("ab", 5), "ab   ")
        self.assertEqual(navi._display_width(navi._fit("白夜白夜白夜", 6)), 6)
        self.assertEqual(navi._display_width(navi._fit("abcdef", 4)), 4)


class TestSelectionHelpers(unittest.TestCase):
    def test_split_ids(self):
        self.assertEqual(navi._split_ids(["a, b", "c", "a", "", " ,d"]), ["a", "b", "c", "d"])
        self.assertEqual(navi._split_ids(None), [])
        self.assertEqual(navi._split_ids([]), [])

    def test_filter_songs(self):
        songs = [
            song_entry("a", "One", 1, "Blue", "Miles Davis"),
            song_entry("b", "Two", 1, "Kind of Blue", "Miles Davis"),
            song_entry("c", "Three", 1, "Radio", "Radiohead"),
        ]
        self.assertEqual([s["id"] for s in navi.filter_songs(songs, "miles")], ["a", "b"])
        self.assertEqual([s["id"] for s in navi.filter_songs(songs, "MILES")], ["a", "b"])
        self.assertEqual([s["id"] for s in navi.filter_songs(songs, album="blue")], ["a", "b"])
        self.assertEqual([s["id"] for s in navi.filter_songs(songs, "miles", "kind")], ["b"])
        self.assertEqual(navi.filter_songs(songs), songs)
        self.assertEqual(navi.filter_songs(songs, "nobody"), [])

    def test_filter_survives_missing_metadata(self):
        # A track with no artist or album must not crash the filter, and must
        # not match a filter that names either of them.
        songs = [{"id": "a", "title": "Bare"}, {"id": "b", "title": "Named", "artist": None}]
        self.assertEqual(navi.filter_songs(songs, "anything"), [])
        self.assertEqual(navi.filter_songs(songs, album="blue"), [])
        self.assertEqual(navi.filter_songs([songs[1]], ""), [songs[1]])

    def test_format_duration(self):
        self.assertEqual(navi.format_duration(0), "--:--")
        self.assertEqual(navi.format_duration(None), "--:--")
        self.assertEqual(navi.format_duration("245"), "4:05")
        self.assertEqual(navi.format_duration(59), "0:59")
        self.assertEqual(navi.format_duration(3725), "1:02:05")
        self.assertEqual(navi.format_duration("junk"), "--:--")

    def test_nested_album_and_artist_are_flattened(self):
        song = {"id": "a", "title": "T", "album": {"id": "al-1", "name": "Blue"}, "artist": {"name": "Miles"}}
        flat = navi._normalise_song(song)
        self.assertEqual(flat["album"], "Blue")
        self.assertEqual(flat["artist"], "Miles")
        # The original must not be modified.
        self.assertIsInstance(song["album"], dict)

    def test_nested_metadata_still_fills_in_paths(self):
        song = navi._normalise_song(
            {
                "id": "a",
                "title": "So What",
                "track": 1,
                "suffix": "flac",
                "album": {"name": "Kind of Blue"},
                "artist": {"name": "Miles Davis"},
            }
        )
        self.assertEqual(navi.build_album_dir_name(song), "Miles Davis - Kind of Blue")
        self.assertEqual(navi.build_track_filename(song, ".opus"), "01 - So What.opus")

    def test_stdout_writer_is_used_by_default(self):
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            navi.prompt_for_tracks([song_entry("a", "Song", 1)], reader=lambda: "1")
        self.assertIn("Song", buffer.getvalue())


class TestRetryBehaviour(EndToEndTestCase):
    library = (("ratelimit-1", "Rate Limited", 1),)
    behaviours = {"ratelimit-1": "ratelimit"}
    ratelimit_count = 2

    def test_retries_429_and_succeeds(self):
        out = self.temp_dir()
        code = self.run_script(out)
        self.assertEqual(code, 0)
        self.assertTrue((out / "Fixture Artist - Fixture Album" / "01 - Rate Limited.opus").is_file())
        self.assertEqual(self.server.state["stream_attempts"]["ratelimit-1"], 3)
        record = json.loads((out / "manifest.jsonl").read_text().strip())
        self.assertEqual(record["status"], "downloaded")
        self.assertEqual(record["attempts"], 3)

    def test_200_with_error_body_is_not_saved(self):
        self.stub(**{"ratelimit-1": "error-200"})
        out = self.temp_dir()
        code = self.run_script(out, "--retries", "2")
        self.assertEqual(code, 1)
        self.assertEqual(list(out.rglob("*.opus")), [], "an error document must never be written as audio")
        self.assertEqual(list(out.rglob("*.part")), [], "no partial files may be left behind")
        record = json.loads((out / "manifest.jsonl").read_text().strip())
        self.assertEqual(record["status"], "failed")
        self.assertIn("too many concurrent transcodes", record["error"])


class TestFailureModes(EndToEndTestCase):
    library = (
        ("ok-1", "First Track", 1),
        ("flac-1", "Not Transcoded", 2),
        ("gone-1", "Missing", 3),
        ("error-1", "Always Errors", 4),
    )
    behaviours = {"flac-1": "flac", "gone-1": "gone", "error-1": "error-200"}

    def test_good_tracks_survive_bad_ones(self):
        out = self.temp_dir()
        code = self.run_script(out)
        self.assertEqual(code, 1, "failures must be reported through the exit code")

        album = out / "Fixture Artist - Fixture Album"
        self.assertTrue((album / "01 - First Track.opus").is_file())
        self.assertEqual(list(out.rglob("*.part")), [])

        records = {json.loads(line)["title"]: json.loads(line) for line in (out / "manifest.jsonl").read_text().strip().splitlines()}
        self.assertEqual(records["First Track"]["status"], "downloaded")
        self.assertEqual(records["Not Transcoded"]["status"], "failed")
        self.assertIn("instead of the requested opus transcode", records["Not Transcoded"]["error"])
        self.assertIn("audio/flac", records["Not Transcoded"]["error"])
        self.assertEqual(records["Not Transcoded"]["attempts"], 1, "a misconfiguration must not be retried")
        self.assertEqual(records["Missing"]["status"], "failed")
        self.assertEqual(records["Missing"]["attempts"], 1, "a 404 must not be retried")

    def test_transcoder_fault_is_reported_with_guidance(self):
        out = self.temp_dir()
        self.stub(**{"flac-1": "flac"})
        code = self.run_script(out, "--limit", "2", "--retries", "1")
        self.assertEqual(code, 1)
        record = json.loads((out / "manifest.jsonl").read_text().strip().splitlines()[-1])
        self.assertEqual(record["status"], "failed")

    def test_retryable_error_eventually_gives_up_with_retries(self):
        out = self.temp_dir()
        self.stub(**{"error-1": "server-error"})
        code = self.run_script(out, "--retries", "2")
        self.assertEqual(code, 1)
        record = [json.loads(line) for line in (out / "manifest.jsonl").read_text().strip().splitlines()]
        self.assertEqual(record[0]["status"], "downloaded")
        self.assertEqual(record[-1]["status"], "failed")
        self.assertEqual(record[-1]["attempts"], 2)
        self.assertEqual(self.server.state["stream_attempts"]["error-1"], 2)


class TestTransportFailures(EndToEndTestCase):
    library = (("drop-1", "Dropped Mid Transfer", 1),)
    behaviours = {"drop-1": "drop"}

    def test_incomplete_body_is_retried(self):
        out = self.temp_dir()
        code = self.run_script(out, "--retries", "5")
        self.assertEqual(code, 0, "a truncated transfer should be retried and then succeed")
        self.assertEqual(self.server.state["stream_attempts"]["drop-1"], 2)
        track = out / "Fixture Artist - Fixture Album" / "01 - Dropped Mid Transfer.opus"
        self.assertTrue(track.is_file())
        self.assertTrue(navi._has_embedded_cover(track))

    def test_lying_content_length_is_caught(self):
        out = self.temp_dir()
        self.stub(**{"drop-1": "truncate"})
        code = self.run_script(out, "--retries", "2")
        self.assertEqual(code, 0, "the server is only told to truncate once")
        record = json.loads((out / "manifest.jsonl").read_text().strip())
        self.assertEqual(record["status"], "downloaded")

    def test_permanently_failing_transport_gives_up(self):
        out = self.temp_dir()
        self.stub(**{"drop-1": "truncate-always"})
        code = self.run_script(out, "--retries", "2")
        self.assertEqual(code, 1)
        self.assertEqual(list(out.rglob("*.part")), [])
        record = json.loads((out / "manifest.jsonl").read_text().strip())
        self.assertEqual(record["status"], "failed")
        self.assertEqual(record["attempts"], 2)
        self.assertIn("truncated download", record["error"])


class TestVbrLengthEstimate(EndToEndTestCase):
    """Regression test for real Navidrome behaviour found in production."""

    library = (("vbr-1", "Vbr Estimated", 1),)
    behaviours = {"vbr-1": "vbr-estimate"}

    def test_slightly_high_content_length_is_accepted(self):
        out = self.temp_dir()
        code = self.run_script(out, "--retries", "1")
        self.assertEqual(code, 0, "a nominal-bitrate estimate must not fail the download")
        track = out / "Fixture Artist - Fixture Album" / "01 - Vbr Estimated.opus"
        self.assertTrue(track.is_file())
        navi.verify_opus_with_cover(track.read_bytes(), COVER)
        record = json.loads((out / "manifest.jsonl").read_text().strip())
        self.assertEqual(record["status"], "downloaded")
        self.assertEqual(record["attempts"], 1)

    def test_silently_truncated_stream_is_caught_by_the_container_check(self):
        out = self.temp_dir()
        self.stub(**{"vbr-1": "silent-truncate"})
        code = self.run_script(out, "--retries", "2")
        self.assertEqual(code, 1)
        self.assertEqual(list(out.rglob("*.opus")), [], "a broken container must never be saved")
        self.assertEqual(list(out.rglob("*.part")), [])
        record = json.loads((out / "manifest.jsonl").read_text().strip())
        self.assertEqual(record["status"], "failed")
        self.assertIn("incomplete audio stream", record["error"])

    def test_grossly_short_body_is_still_rejected(self):
        out = self.temp_dir()
        self.stub(**{"vbr-1": "truncate-always"})
        code = self.run_script(out, "--retries", "2")
        self.assertEqual(code, 1)
        self.assertEqual(list(out.rglob("*.opus")), [])
        record = json.loads((out / "manifest.jsonl").read_text().strip())
        self.assertEqual(record["status"], "failed")
        self.assertEqual(record["attempts"], 2)


class TestEmptyResponse(EndToEndTestCase):
    library = (("empty-1", "Empty", 1),)
    behaviours = {"empty-1": "empty"}

    def test_empty_stream_is_a_failure(self):
        out = self.temp_dir()
        code = self.run_script(out, "--retries", "1")
        self.assertEqual(code, 1)
        self.assertEqual(list(out.rglob("*.opus")), [])
        record = json.loads((out / "manifest.jsonl").read_text().strip())
        self.assertIn("empty audio stream", record["error"])


class TestAuth(EndToEndTestCase):
    library = (("ok-1", "First Track", 1),)

    def test_wrong_password_fails_fast(self):
        out = self.temp_dir()
        code = navi.main(
            ["--url", self.server.url, "--user", USERNAME, "--password", "wrong",
             "--out", str(out), "--log-level", "CRITICAL"]
        )
        self.assertEqual(code, 2)
        self.assertGreaterEqual(self.server.state["auth_failures"], 1)

    def test_wrong_username_fails_fast(self):
        out = self.temp_dir()
        code = navi.main(
            ["--url", self.server.url, "--user", "mallory", "--password", PASSWORD,
             "--out", str(out), "--log-level", "CRITICAL"]
        )
        self.assertEqual(code, 2)

    def test_unreachable_server_fails_fast(self):
        out = self.temp_dir()
        code = navi.main(
            ["--url", "http://127.0.0.1:1", "--user", USERNAME, "--password", PASSWORD,
             "--out", str(out), "--log-level", "CRITICAL", "--timeout", "2"]
        )
        self.assertEqual(code, 2)

    def test_token_auth_is_used(self):
        """The password must never appear in the query string."""
        out = self.temp_dir()
        self.assertEqual(self.run_script(out), 0)
        for _endpoint, params in self.server.state["requests"]:
            self.assertIn("t", params)
            self.assertIn("s", params)
            self.assertNotIn("p", params)
            self.assertNotIn(PASSWORD, params.values())

    def test_env_vars_are_honoured(self):
        out = self.temp_dir()
        os.environ["ND_URL"] = self.server.url
        os.environ["ND_USERNAME"] = USERNAME
        os.environ["ND_PASSWORD"] = PASSWORD
        try:
            code = navi.main(["--out", str(out), "--log-level", "CRITICAL"])
        finally:
            for key in ("ND_URL", "ND_USERNAME", "ND_PASSWORD"):
                os.environ.pop(key, None)
        self.assertEqual(code, 0)


class TestArgumentValidation(unittest.TestCase):
    def test_missing_required(self):
        self.assertEqual(navi.main(["--log-level", "CRITICAL"]), 2)
        self.assertEqual(navi.main(["--url", "http://x", "--log-level", "CRITICAL"]), 2)
        self.assertEqual(navi.main(["--url", "http://x", "--user", "a", "--log-level", "CRITICAL"]), 2)

    def test_bitrate_bounds(self):
        for bad in ("0", "300", "-5"):
            code = navi.main(
                ["--url", "http://x", "--user", "a", "--password", "b",
                 "--bitrate", bad, "--log-level", "CRITICAL", "--dry-run"]
            )
            self.assertEqual(code, 2, f"--bitrate {bad} should be rejected")

    def test_numeric_bounds(self):
        for flag, bad in (("--retries", "0"), ("--workers", "0"), ("--timeout", "0"), ("--cover-size", "0")):
            code = navi.main(
                ["--url", "http://x", "--user", "a", "--password", "b",
                 flag, bad, "--log-level", "CRITICAL", "--dry-run"]
            )
            self.assertEqual(code, 2, f"{flag} {bad} should be rejected")

    def test_url_scheme_is_inferred(self):
        args = navi.build_parser().parse_args(["--url", "music.example.com", "--user", "a", "--password", "b"])
        navi.validate_args(args)
        self.assertFalse("://" in args.url)

    def test_defaults(self):
        args = navi.build_parser().parse_args(["--url", "http://x", "--user", "a", "--password", "b"])
        self.assertEqual(navi.validate_args(args), [])
        self.assertEqual(args.out, "music")
        self.assertFalse(args.favorites_only, "the whole library is the default scope")

    def test_favorites_only_flag(self):
        args = navi.build_parser().parse_args(
            ["--url", "http://x", "--user", "a", "--password", "b", "--favorites-only"]
        )
        self.assertTrue(args.favorites_only)

    def test_selection_defaults(self):
        args = navi.build_parser().parse_args(["--url", "http://x", "--user", "a", "--password", "b"])
        self.assertIsNone(args.song_id)
        self.assertIsNone(args.search)
        self.assertIsNone(args.artist)
        self.assertIsNone(args.album)
        self.assertFalse(args.pick)
        self.assertFalse(args.all, "a terminal run asks rather than assuming")

    def test_song_id_is_repeatable(self):
        args = navi.build_parser().parse_args(
            ["--url", "http://x", "--user", "a", "--password", "b", "--song-id", "a", "--song-id", "b,c"]
        )
        self.assertEqual(args.song_id, ["a", "b,c"])
        self.assertEqual(navi._split_ids(args.song_id), ["a", "b", "c"])

    def test_only_one_scope_at_a_time(self):
        scopes = (
            ["--favorites-only"],
            ["--song-id", "a"],
            ["--search", "q"],
            ["--artist", "x"],
            ["--album", "y"],
        )
        for first in scopes:
            for second in scopes:
                if first is second:
                    continue
                args = navi.build_parser().parse_args(
                    ["--url", "http://x", "--user", "a", "--password", "b"] + first + second
                )
                problems = navi.validate_args(args)
                self.assertTrue(
                    any("choose one scope" in problem for problem in problems),
                    f"{first} with {second} should be rejected, got {problems}",
                )

    def test_asking_composes_with_a_scope(self):
        # --pick and --all answer "should I ask?", not "what do I want?", so
        # they are allowed with a scope -- that is what makes search-then-pick a
        # single command.
        for modifier in (["--pick"], ["--all"], []):
            for scope in (["--search", "q"], ["--artist", "x"], ["--favorites-only"], []):
                args = navi.build_parser().parse_args(
                    ["--url", "http://x", "--user", "a", "--password", "b"] + modifier + scope
                )
                self.assertEqual(navi.validate_args(args), [], f"{modifier} with {scope} should be valid")

    def test_every_single_scope_is_accepted(self):
        for scope in (["--favorites-only"], ["--song-id", "a"], ["--search", "q"],
                      ["--artist", "x"], ["--album", "y"], ["--all"], []):
            args = navi.build_parser().parse_args(
                ["--url", "http://x", "--user", "a", "--password", "b"] + scope
            )
            self.assertEqual(navi.validate_args(args), [], f"{scope} should be valid on its own")

    def test_empty_scope_values_are_rejected(self):
        for flag in ("--search", "--artist", "--album"):
            code = navi.main(
                ["--url", "http://x", "--user", "a", "--password", "b",
                 flag, "   ", "--log-level", "CRITICAL", "--dry-run"]
            )
            self.assertEqual(code, 2, f"{flag} with a blank value should be rejected")

    def test_pick_cannot_be_combined_with_all(self):
        code = navi.main(
            ["--url", "http://x", "--user", "a", "--password", "b",
             "--pick", "--all", "--log-level", "CRITICAL", "--dry-run"]
        )
        self.assertEqual(code, 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
