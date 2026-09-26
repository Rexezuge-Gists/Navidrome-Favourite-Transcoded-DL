#!/usr/bin/env python3
"""Download chosen tracks from Navidrome as transcoded Opus files.

Run from a terminal, the script shows what it found and asks which tracks to
download, so the common case is "grab these three songs". Pass ``--all`` (or
pipe the output, or run it from cron, where there is no terminal) to take
everything without being asked. Tracks can also be chosen up front instead:

  * ``--song-id``     - exact Subsonic song ids (``getSong``)
  * ``--search``      - a server-side search over title/artist/album (``search3``)
  * ``--artist`` / ``--album`` - substring filters over the enumerated library
  * ``--favorites-only`` - the account's starred tracks

Only one of those may be given per run, so what a command means is never
ambiguous. ``--pick`` and ``--all`` are not scopes: they answer "should I ask?",
and either may be combined with a scope -- so ``--search reol --pick`` searches
and then lets you choose from the hits.

Navidrome only exposes transcoded audio through the Subsonic API (``/rest``), so
this script speaks that protocol end to end:

  * ``ping``          - validate the URL and credentials before doing any work
  * ``getAlbumList2`` - page through every album (``alphabeticalByName``)
  * ``getAlbum``      - the tracks of one album
  * ``getStarred2``   - the account's favorite tracks (``--favorites-only``)
  * ``getSong``       - the metadata of one named track (``--song-id``)
  * ``search3``       - search by title/artist/album (``--search``)
  * ``getCoverArt``   - one album cover per album, reused across its tracks
  * ``stream``        - the audio, transcoded server side to Opus at a bitrate

Enumerating the library costs one ``getAlbum`` request per album, which is
cheap next to streaming a full transcode of every track, and it is what makes
the list complete: ``getAlbum`` returns an album's tracks without a page limit,
so nothing can fall through a paging gap. ``--song-id`` and ``--search`` skip
that walk entirely, because the server already knows which tracks match.

Authentication uses the Subsonic token scheme (``t=md5(password+salt)``) so the
plaintext password never appears in a URL, and therefore never lands in a
server-side access log.

Dependencies: Python 3.9+ standard library only.

Caveat: transcoding happens on the *server*. If Navidrome has no Opus target
format configured, or ffmpeg is missing from the server/container, the server
silently streams the original file instead. This script detects that (by
sniffing the response) and fails the track loudly rather than saving a FLAC
with an ``.opus`` extension.
"""

from __future__ import annotations

import argparse
import base64
import dataclasses
import hashlib
import http.client
import json
import logging
import os
import random
import re
import secrets
import signal
import socket
import ssl
import struct
import sys
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Iterator, Optional, Sequence

__version__ = "1.1.0"

LOG = logging.getLogger("navifavsync")

SUBSONIC_API_VERSION = "1.16.1"
CLIENT_NAME = "navifavsync"
USER_AGENT = f"{CLIENT_NAME}/{__version__} (python)"

# libopus tops out at 256 kbps for standard Opus.
MIN_OPUS_BITRATE = 6
MAX_OPUS_BITRATE = 256

MAX_NAME_LEN = 120
PARTIAL_SUFFIX = ".part"
DEFAULT_COVER_SIZE = 1000
COVER_FILENAME = "cover.jpg"

# getAlbumList2 rejects more than 500 per page, so that is the largest window
# available for walking the library.
ALBUM_PAGE_SIZE = 500
# 1000 pages is 500k albums; purely a guard against a server that never returns
# a short page and would otherwise loop forever.
MAX_ALBUM_PAGES = 1000
# Scanning a large library is slow, so say something while it happens.
ALBUM_PROGRESS_EVERY = 25

# search3 is paged too, and 100 per page is within every server's comfort zone.
SEARCH_PAGE_SIZE = 100
# 10 pages is 1000 matches; a guard against a server that never returns a short
# page, and a sane ceiling for "download everything this query matched".
MAX_SEARCH_PAGES = 10

# Rows of the candidate list shown between "press enter for more" prompts.
PICKER_PAGE_SIZE = 200
# Sentinel answers accepted at the selection prompt.
PICKER_ALL = "all"
PICKER_NONE = "none"
PICKER_QUIT = "q"

# Transient conditions worth another attempt.
RETRYABLE_HTTP_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504})
RETRYABLE_SUBSONIC_CODES = frozenset({0})  # generic, e.g. "too many concurrent transcodes"

# Subsonic error codes (server/subsonic/responses/errors.go).
SUBSONIC_AUTH_FAIL = 40
SUBSONIC_AUTHZ_FAIL = 50
SUBSONIC_NOT_FOUND = 70

# Server-side transcoding misconfiguration; retrying cannot help and the same
# problem will hit every remaining track.
TRANSCODER_FAULT_MARKERS = (
    "error starting transcoder",
    "executable file not found",
    "no transcoding command",
    "ffmpeg",
)

DEFAULT_BACKOFF_BASE = 1.0
DEFAULT_BACKOFF_CAP = 30.0

# Enough bytes for the first Ogg page's fixed header plus a maximal 255-entry
# segment table and an eight-byte codec tag (27 + 255 + 8).
SNIFF_PREFIX_BYTES = 512

# Navidrome's Content-Length is a nominal-bitrate estimate and runs slightly
# high for VBR Opus, so it is only used to catch grossly short transfers.
LENGTH_TOLERANCE = 0.9


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class NavifavError(Exception):
    """Base class for expected, reportable failures."""


class SubsonicError(NavifavError):
    """A Subsonic API call reported a failure, or the request never completed.

    ``retryable`` overrides the status/code based guesswork below; leave it as
    ``None`` when the classification should be inferred.
    """

    def __init__(
        self,
        message: str,
        *,
        code: Optional[int] = None,
        http_status: Optional[int] = None,
        retry_after: Optional[float] = None,
        retryable: Optional[bool] = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.http_status = http_status
        self.retry_after = retry_after
        self.retryable = retryable


class NotTranscodedError(NavifavError):
    """The server streamed the original file instead of an Opus transcode."""


class OggError(NavifavError):
    """The Ogg/Opus container could not be parsed or rewritten."""


class SelectionError(NavifavError):
    """A selection could not be made: conflicting flags, song ids the server does
    not know, or a ``--pick`` with no terminal to ask the question on."""


# ---------------------------------------------------------------------------
# Ogg container plumbing
# ---------------------------------------------------------------------------
#
# Embedding cover art in an Opus file means rewriting the ``OpusTags`` comment
# packet, because Opus is read-only for conventional ID3-style tags. The
# pipeline below is:
#
#   1. split the bitstream into Ogg pages (validating every page CRC),
#   2. reassemble the first two packets, which must be ``OpusHead`` and
#      ``OpusTags``,
#   3. rebuild the comment packet with an added METADATA_BLOCK_PICTURE entry,
#   4. re-paginate the header and re-stamp the CRC of every page,
#   5. copy the audio pages through untouched apart from their sequence number.
#
# Ogg uses CRC-32 with polynomial 0x04c11db7, no reflection and no final XOR --
# that is *not* ``zlib.crc32``, so the table is built by hand below.

_CAPTURE_PATTERN = b"OggS"
_CRC_OFFSET = 22
_CRC_LENGTH = 4
_MAX_SEGMENTS_PER_PAGE = 255
_CONTINUED_PACKET = 0x01
_BEGINNING_OF_STREAM = 0x02

METADATA_BLOCK_PICTURE = "METADATA_BLOCK_PICTURE"
PICTURE_TYPE_FRONT_COVER = 3


def _build_ogg_crc_table() -> tuple:
    table = []
    for index in range(256):
        remainder = index << 24
        for _ in range(8):
            if remainder & 0x80000000:
                remainder = ((remainder << 1) ^ 0x04C11DB7) & 0xFFFFFFFF
            else:
                remainder = (remainder << 1) & 0xFFFFFFFF
        table.append(remainder)
    return tuple(table)


_OGG_CRC_TABLE = _build_ogg_crc_table()


def ogg_crc32(data: bytes) -> int:
    """CRC-32 as Ogg defines it (poly 0x04c11db7, no bit reversal)."""
    crc = 0
    table = _OGG_CRC_TABLE
    for byte in data:
        crc = ((crc << 8) & 0xFFFFFFFF) ^ table[((crc >> 24) & 0xFF) ^ byte]
    return crc & 0xFFFFFFFF


@dataclasses.dataclass(frozen=True)
class OggPage:
    raw: bytes
    serial: int
    sequence: int
    granule: int
    header_type: int
    payload: bytes
    packets: tuple
    carry_after: bytes


def _assemble_packets(segments: bytes, payload: bytes, carry: bytes) -> tuple:
    packets = []
    current = bytearray(carry)
    offset = 0
    for size in segments:
        current += payload[offset : offset + size]
        offset += size
        if size != 255:
            packets.append(bytes(current))
            current = bytearray()
    return packets, bytes(current)


def iter_ogg_pages(data: bytes) -> Iterator[OggPage]:
    """Yield each Ogg page in *data*, verifying its CRC and segment table."""
    position = 0
    total = len(data)
    carry = b""
    while position < total:
        if data[position : position + 4] != _CAPTURE_PATTERN:
            raise OggError(f"lost Ogg page sync at byte offset {position}")
        if position + 27 > total:
            raise OggError(f"truncated Ogg page header at byte offset {position}")
        if data[position + 4] != 0:
            raise OggError(f"unsupported Ogg page version {data[position + 4]}")
        header_type = data[position + 5]
        granule = struct.unpack_from("<q", data, position + 6)[0]
        serial, sequence, stored_crc = struct.unpack_from("<III", data, position + 14)
        segment_count = data[position + 26]
        segment_start = position + 27
        if segment_start + segment_count > total:
            raise OggError(f"truncated Ogg segment table in page {sequence}")
        segments = data[segment_start : segment_start + segment_count]
        payload_start = segment_start + segment_count
        payload_length = sum(segments)
        page_end = payload_start + payload_length
        if page_end > total:
            raise OggError(f"truncated Ogg page payload in page {sequence}")

        raw = data[position:page_end]
        zeroed = raw[:_CRC_OFFSET] + b"\x00" * _CRC_LENGTH + raw[_CRC_OFFSET + _CRC_LENGTH :]
        computed = ogg_crc32(zeroed)
        if computed != stored_crc:
            raise OggError(
                f"Ogg CRC mismatch in page {sequence} "
                f"(stored {stored_crc:#010x}, computed {computed:#010x})"
            )

        payload = raw[payload_start - position :]
        packets, carry = _assemble_packets(segments, payload, carry)
        yield OggPage(
            raw=raw,
            serial=serial,
            sequence=sequence,
            granule=granule,
            header_type=header_type,
            payload=payload,
            packets=tuple(packets),
            carry_after=carry,
        )
        position = page_end
    if carry:
        raise OggError("stream ends with an incomplete Ogg packet")


def _build_page(serial: int, sequence: int, granule: int, header_type: int, segments: list, payload: bytes) -> bytes:
    if len(segments) > _MAX_SEGMENTS_PER_PAGE:
        raise OggError("segment table overflow")
    table = bytes(segments)
    page = bytearray()
    page += _CAPTURE_PATTERN
    page += bytes((0, header_type))
    page += struct.pack("<q", granule)
    page += struct.pack("<III", serial, sequence, 0)
    page += bytes((len(table),))
    page += table
    page += payload
    crc = ogg_crc32(bytes(page))
    struct.pack_into("<I", page, _CRC_OFFSET, crc)
    return bytes(page)


def paginate_packets(packets: Sequence[bytes], serial: int) -> list:
    """Split *packets* across as many Ogg pages as they require."""
    runs = []
    for packet in packets:
        offset = 0
        while offset < len(packet):
            take = min(255, len(packet) - offset)
            runs.append((take, packet[offset : offset + take], len(packet) - offset - take < 255))
            offset += take
    if not runs:
        return []

    pages = []
    index = 0
    while index < len(runs):
        chunk = runs[index : index + _MAX_SEGMENTS_PER_PAGE]
        index += _MAX_SEGMENTS_PER_PAGE
        header_type = 0
        if not pages:
            header_type = _BEGINNING_OF_STREAM
        elif not chunk[0][2]:
            # The first segment is not a terminator, so this page continues a
            # packet begun on the previous page.
            header_type = _CONTINUED_PACKET
        pages.append(
            _build_page(
                serial,
                len(pages),
                0,
                header_type,
                [run[0] for run in chunk],
                b"".join(run[1] for run in chunk),
            )
        )
    return pages


def restamp_page(page: bytes, *, serial: int, sequence: int) -> bytes:
    """Return *page* with a new serial/sequence and a recomputed CRC."""
    buffer = bytearray(page)
    struct.pack_into("<I", buffer, 14, serial)
    struct.pack_into("<I", buffer, 18, sequence)
    struct.pack_into("<I", buffer, _CRC_OFFSET, 0)
    struct.pack_into("<I", buffer, _CRC_OFFSET, ogg_crc32(bytes(buffer)))
    return bytes(buffer)


# ---------------------------------------------------------------------------
# Opus comment packets
# ---------------------------------------------------------------------------


def parse_opus_tags(packet: bytes) -> tuple:
    """Return ``(vendor, [(key, value), ...])`` from an OpusTags packet."""
    if not packet.startswith(b"OpusTags"):
        raise OggError("packet is not an OpusTags header")
    cursor = 8
    (vendor_length,) = struct.unpack_from("<I", packet, cursor)
    cursor += 4
    vendor = packet[cursor : cursor + vendor_length].decode("utf-8", "replace")
    cursor += vendor_length
    (count,) = struct.unpack_from("<I", packet, cursor)
    cursor += 4
    comments = []
    for _ in range(count):
        (length,) = struct.unpack_from("<I", packet, cursor)
        cursor += 4
        raw = packet[cursor : cursor + length]
        cursor += length
        key, _, value = raw.partition(b"=")
        comments.append((key.decode("utf-8", "replace"), value.decode("utf-8", "replace")))
    return vendor, comments


def build_opus_tags(vendor: str, comments: Sequence) -> bytes:
    out = bytearray(b"OpusTags")
    encoded_vendor = vendor.encode("utf-8")
    out += struct.pack("<I", len(encoded_vendor)) + encoded_vendor
    out += struct.pack("<I", len(comments))
    for key, value in comments:
        encoded = f"{key}={value}".encode("utf-8")
        out += struct.pack("<I", len(encoded)) + encoded
    return bytes(out)


# ---------------------------------------------------------------------------
# FLAC METADATA_BLOCK_PICTURE
# ---------------------------------------------------------------------------


def flac_picture_block(image: bytes, mime: str, width: int, height: int, color_depth: int) -> bytes:
    """Build a FLAC picture metadata block (the payload of the base64 comment)."""
    mime_bytes = mime.encode("ascii")
    description = b""
    parts = [
        struct.pack(">I", PICTURE_TYPE_FRONT_COVER),
        struct.pack(">I", len(mime_bytes)),
        mime_bytes,
        struct.pack(">I", len(description)),
        description,
        struct.pack(">I", max(0, int(width))),
        struct.pack(">I", max(0, int(height))),
        struct.pack(">I", max(0, int(color_depth))),
        struct.pack(">I", 0),  # palette size: 0 means not colour-indexed
        struct.pack(">I", len(image)),
        image,
    ]
    return b"".join(parts)


def parse_flac_picture_block(block: bytes) -> dict:
    """Read back a picture block; used to verify what was written."""
    cursor = 0
    (picture_type,) = struct.unpack_from(">I", block, cursor)
    cursor += 4
    (mime_length,) = struct.unpack_from(">I", block, cursor)
    cursor += 4
    mime = block[cursor : cursor + mime_length].decode("ascii", "replace")
    cursor += mime_length
    (description_length,) = struct.unpack_from(">I", block, cursor)
    cursor += 4
    description = block[cursor : cursor + description_length]
    cursor += description_length
    width, height, depth, colors, data_length = struct.unpack_from(">IIIII", block, cursor)
    cursor += 20
    data = block[cursor : cursor + data_length]
    return {
        "picture_type": picture_type,
        "mime": mime,
        "description": description.decode("utf-8", "replace"),
        "width": width,
        "height": height,
        "color_depth": depth,
        "colors": colors,
        "declared_length": data_length,
        "length": len(data),
        "image": data,
    }


# ---------------------------------------------------------------------------
# Image sniffing (no Pillow in the standard library)
# ---------------------------------------------------------------------------

_JPEG_SOF_MARKERS = frozenset(
    list(range(0xC0, 0xC4)) + list(range(0xC5, 0xC8)) + list(range(0xC9, 0xCC)) + list(range(0xCD, 0xD0))
)


def _sniff_jpeg(data: bytes) -> Optional[tuple]:
    if not data.startswith(b"\xff\xd8"):
        return None
    cursor = 2
    while cursor + 4 <= len(data):
        if data[cursor] != 0xFF:
            cursor += 1
            continue
        marker = data[cursor + 1]
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
            cursor += 2
            continue
        if marker == 0xD9:
            break
        if cursor + 4 > len(data):
            break
        (length,) = struct.unpack_from(">H", data, cursor + 2)
        if marker in _JPEG_SOF_MARKERS:
            if cursor + 9 > len(data):
                break
            height, width = struct.unpack_from(">HH", data, cursor + 5)
            components = data[cursor + 9] if cursor + 9 < len(data) else 3
            return "image/jpeg", int(width), int(height), 8 * max(1, components)
        cursor += 2 + max(2, length)
    return None


def _sniff_png(data: bytes) -> Optional[tuple]:
    if not data.startswith(b"\x89PNG\r\n\x1a\n"):
        return None
    if len(data) < 26 or data[12:16] != b"IHDR":
        return None
    width, height = struct.unpack_from(">II", data, 16)
    bit_depth = data[24]
    color_type = data[25]
    channels = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}.get(color_type, 3)
    return "image/png", int(width), int(height), max(1, bit_depth * channels)


def _sniff_webp(data: bytes) -> Optional[tuple]:
    if len(data) < 16 or data[:4] != b"RIFF" or data[8:12] != b"WEBP":
        return None
    chunk = data[12:16]
    if chunk == b"VP8X" and len(data) >= 30:
        width = int.from_bytes(data[24:27], "little") + 1
        height = int.from_bytes(data[27:30], "little") + 1
        return "image/webp", width, height, 32
    if chunk == b"VP8 " and len(data) >= 30:
        if data[23:26] != b"\x9d\x01\x2a":
            return None
        width = int.from_bytes(data[26:28], "little") & 0x3FFF
        height = int.from_bytes(data[28:30], "little") & 0x3FFF
        return "image/webp", width, height, 24
    if chunk == b"VP8L" and len(data) >= 25:
        if data[20] != 0x2F:
            return None
        bits = int.from_bytes(data[21:25], "little")
        width = (bits & 0x3FFF) + 1
        height = ((bits >> 14) & 0x3FFF) + 1
        return "image/webp", width, height, 32
    return None


def sniff_image(data: bytes) -> Optional[tuple]:
    """Return ``(mime, width, height, color_depth_bits)`` for *data*."""
    for sniffer in (_sniff_jpeg, _sniff_png, _sniff_webp):
        result = sniffer(data)
        if result is not None:
            return result
    return None


# ---------------------------------------------------------------------------
# Audio sniffing
# ---------------------------------------------------------------------------


def sniff_audio_format(head: bytes) -> str:
    """Identify an audio container from the first bytes of a stream."""
    if head.startswith(b"fLaC"):
        return "flac"
    if head.startswith(b"ID3"):
        return "mp3"
    if head.startswith(b"RIFF") and head[8:12] == b"WAVE":
        return "wav"
    if head.startswith(b"\x1a\x45\xdf\xa3"):
        return "matroska"
    if len(head) >= 12 and head[4:8] == b"ftyp":
        return "mp4"
    if head.startswith(b"FORM") and head[8:12] == b"AIFF":
        return "aiff"
    if head.startswith(_CAPTURE_PATTERN):
        if len(head) >= 27:
            # The payload of the first page starts after the fixed 27-byte header
            # and a segment table of up to 255 bytes, so the codec tag is only
            # reachable with a prefix of at least 290 bytes. Embedding cover art
            # pushes the whole comment packet onto the first page, which is
            # exactly when a short prefix would fail to see OpusHead.
            payload_start = 27 + head[26]
            if len(head) >= payload_start + 8:
                tag = head[payload_start : payload_start + 8]
                if tag.startswith(b"OpusHead"):
                    return "opus"
                # Vorbis identification and comment headers carry a leading
                # packet-type byte, so the codec name starts one byte in.
                if tag[1:7] == b"vorbis":
                    return "vorbis"
                if tag.startswith(b"fLaC"):
                    return "flac"
                if tag.startswith(b"\x80theora"):
                    return "theora"
        return "ogg"
    if len(head) >= 2 and head[0] == 0xFF and (head[1] & 0xE0) == 0xE0:
        # 12-bit sync. ADTS (AAC) leaves the layer bits at zero; MPEG audio does not.
        if (head[1] & 0x06) == 0:
            return "aac"
        return "mp3"
    return "unknown"


_FORMAT_DISPLAY = {
    "flac": "FLAC",
    "mp3": "MP3",
    "wav": "WAV",
    "ogg": "Ogg (unidentified codec)",
    "vorbis": "Vorbis",
    "matroska": "Matroska/WebM",
    "mp4": "MP4/M4A",
    "aac": "AAC",
    "aiff": "AIFF",
    "unknown": "unrecognised data",
}


def assert_complete_opus(data: bytes) -> dict:
    """Confirm *data* is a whole Ogg/Opus stream rather than a truncated transfer.

    The container is the only trustworthy integrity signal available here.
    Navidrome's ``Content-Length`` comes from ``estimateContentLength`` and assumes
    a constant bitrate, so with VBR libopus it routinely overstates the real size
    by around 1%; a strict length comparison would reject good downloads.

    Two real truncation signals are checked instead, both of which hold on genuine
    Navidrome output: a page whose declared payload never arrived, and a trailing
    packet that never terminated. Note that ffmpeg does *not* set the
    end-of-stream flag on the final page, so that cannot be required.
    """
    pages = 0
    last_granule = -1
    for page in iter_ogg_pages(data):  # raises OggError on CRC or truncation
        pages += 1
        last_granule = page.granule
    if pages < 2:
        raise OggError("stream is too short to contain both Opus header packets")
    if last_granule < 0:
        raise OggError("stream ends with an unfinished Ogg page")
    return {"pages": pages, "granule": last_granule}


# ---------------------------------------------------------------------------
# Cover art injection
# ---------------------------------------------------------------------------


def _consume_header_packets(pages: Iterator) -> tuple:
    packets: list = []
    serial = 0
    consumed = 0
    for page in pages:
        serial = page.serial
        consumed += 1
        packets.extend(page.packets)
        if len(packets) >= 2:
            if page.carry_after:
                raise OggError(
                    "Opus header packets do not end on an Ogg page boundary; "
                    "refusing to rewrite the stream"
                )
            return packets[:2], consumed, serial
    raise OggError("stream does not contain both OpusHead and OpusTags packets")


def embed_cover_art(opus_data: bytes, image: bytes) -> tuple:
    """Return ``(new_opus_bytes, picture_info)`` with *image* embedded."""
    info = sniff_image(image)
    if info is None:
        raise OggError("cover art format not recognised (expected JPEG, PNG or WebP)")
    mime, width, height, depth = info

    block = flac_picture_block(image, mime, width, height, depth)
    picture_b64 = base64.b64encode(block).decode("ascii")

    pages = iter_ogg_pages(opus_data)
    (opus_head, opus_tags), _consumed, serial = _consume_header_packets(pages)
    if not opus_head.startswith(b"OpusHead"):
        raise OggError("first Ogg packet is not an OpusHead header")

    vendor, comments = parse_opus_tags(opus_tags)
    kept = [(key, value) for key, value in comments if key.upper() != METADATA_BLOCK_PICTURE]
    kept.append((METADATA_BLOCK_PICTURE, picture_b64))
    new_tags = build_opus_tags(vendor, kept)

    header_pages = paginate_packets([opus_head, new_tags], serial)
    out = bytearray()
    for page in header_pages:
        out += page
    for offset, page in enumerate(pages):
        out += restamp_page(page.raw, serial=serial, sequence=len(header_pages) + offset)

    return bytes(out), parse_flac_picture_block(block)


def verify_opus_with_cover(data: bytes, expected_image: bytes) -> dict:
    """Re-parse *data* and confirm it is a valid Opus stream carrying *expected_image*.

    This runs on the bytes actually written to disk, so a corrupt rewrite is
    caught before the file replaces the good download.
    """
    pages = list(iter_ogg_pages(data))
    if not pages:
        raise OggError("rewritten stream contains no Ogg pages")
    if not (pages[0].header_type & _BEGINNING_OF_STREAM):
        raise OggError("rewritten stream is missing the beginning-of-stream flag")

    serial = pages[0].serial
    for index, page in enumerate(pages):
        if page.sequence != index:
            raise OggError(f"non-contiguous page sequence {page.sequence} at position {index}")
        if page.serial != serial:
            raise OggError(f"page {index} has a mismatched bitstream serial number")

    (opus_head, opus_tags), consumed, _ = _consume_header_packets(iter(pages))
    if not opus_head.startswith(b"OpusHead"):
        raise OggError("rewritten stream lost its OpusHead packet")
    vendor, comments = parse_opus_tags(opus_tags)

    pictures = [value for key, value in comments if key.upper() == METADATA_BLOCK_PICTURE]
    if not pictures:
        raise OggError("rewritten stream contains no METADATA_BLOCK_PICTURE comment")
    try:
        block = base64.b64decode(pictures[0], validate=True)
    except (ValueError, base64.binascii.Error) as exc:
        raise OggError("embedded picture comment is not valid base64") from exc
    info = parse_flac_picture_block(block)
    if info["length"] != info["declared_length"]:
        raise OggError("embedded picture block is truncated")
    if info["image"] != expected_image:
        raise OggError("embedded picture bytes do not match the downloaded cover art")
    if info["picture_type"] != PICTURE_TYPE_FRONT_COVER:
        raise OggError(f"unexpected picture type {info['picture_type']}")

    audio_pages = pages[consumed:]
    return {
        "pages": len(pages),
        "audio_pages": len(audio_pages),
        "audio_bytes": sum(len(page.payload) for page in audio_pages),
        "vendor": vendor,
        "picture": {key: value for key, value in info.items() if key != "image"},
    }


# ---------------------------------------------------------------------------
# Filenames and paths
# ---------------------------------------------------------------------------

_ILLEGAL_CHARS = re.compile(r'[\x00-\x1f\x7f<>:"/\\|?*]+')
_LINE_BREAKS = re.compile(r"[\t\n\v\f\r  ]+")
_WHITESPACE = re.compile(r" +")
_WINDOWS_RESERVED = frozenset(
    ["CON", "PRN", "AUX", "NUL"]
    + [f"COM{index}" for index in range(1, 10)]
    + [f"LPT{index}" for index in range(1, 10)]
)


def sanitize_component(name: str, max_length: int = MAX_NAME_LEN) -> str:
    """Make *name* safe to use as a single path component on Linux/macOS/Windows."""
    cleaned = unicodedata.normalize("NFC", name or "")
    # Tabs and newlines become plain spaces first, so they merge with their
    # neighbours instead of becoming underscores. Note that \s would also match
    # the C0 separators \x1c-\x1f, which are not whitespace in any useful sense.
    cleaned = _LINE_BREAKS.sub(" ", cleaned)
    cleaned = _ILLEGAL_CHARS.sub("_", cleaned)
    cleaned = _WHITESPACE.sub(" ", cleaned).strip()
    cleaned = cleaned.rstrip(". ")
    if cleaned.startswith("."):
        cleaned = "_" + cleaned
    if not cleaned:
        return "Unknown"
    if cleaned.split(".")[0].upper() in _WINDOWS_RESERVED:
        cleaned = "_" + cleaned
    if len(cleaned) > max_length:
        cleaned = cleaned[:max_length].rstrip(". ") or "Unknown"
    return cleaned


def _as_int(value: Any) -> int:
    try:
        if value is None or value == "":
            return 0
        return int(value)
    except (TypeError, ValueError):
        return 0


def build_album_dir_name(song: dict) -> str:
    artist = str(song.get("artist") or "").strip() or "Unknown Artist"
    album = str(song.get("album") or "").strip() or "Unknown Album"
    return sanitize_component(f"{artist} - {album}")


def build_track_filename(song: dict, extension: str) -> str:
    title = str(song.get("title") or "").strip() or "Unknown Title"
    track = _as_int(song.get("track"))
    disc = _as_int(song.get("discNumber"))
    if disc > 1:
        stem = f"{disc:02d}-{track:02d} - {title}"
    elif track > 0:
        stem = f"{track:02d} - {title}"
    else:
        stem = title
    return sanitize_component(stem) + extension


def _describe(song: dict) -> str:
    artist = song.get("artist") or "?"
    title = song.get("title") or "?"
    return f"{artist} - {title}"


# ---------------------------------------------------------------------------
# Subsonic client
# ---------------------------------------------------------------------------


def _as_entries(value: Any) -> list:
    """Subsonic collapses single-element lists into a bare object; undo that."""
    if isinstance(value, dict):
        return [value]
    if not value:
        return []
    return [item for item in value if isinstance(item, dict)]


def _playable_songs(value: Any) -> list:
    """Keep only real tracks: no directories, no entries missing a file suffix."""
    return [
        song
        for song in _as_entries(value)
        if song.get("id") and not song.get("isDir") and song.get("suffix")
    ]


def _normalise_song(song: dict) -> dict:
    """Flatten ``album``/``artist`` to plain names, as ``search3`` may nest them.

    Subsonic's spec lets search results carry the album and artist as objects;
    other fields (and every other endpoint) use a bare string. Path building and
    filtering need the string form, so collapse both shapes here.
    """
    flattened = dict(song)
    for key in ("album", "artist"):
        value = flattened.get(key)
        if isinstance(value, dict):
            flattened[key] = str(value.get("name") or value.get("title") or value.get("album") or "")
    return flattened


def _md5_hex(data: bytes) -> str:
    try:
        return hashlib.md5(data, usedforsecurity=False).hexdigest()
    except TypeError:  # pragma: no cover - very old interpreters
        return hashlib.md5(data).hexdigest()


def _retry_after_seconds(headers: Any) -> Optional[float]:
    if headers is None:
        return None
    try:
        value = headers.get("Retry-After")
    except AttributeError:
        return None
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        return None


def redact_url(url: str) -> str:
    """Strip credential-bearing query parameters before logging a URL."""
    parts = urllib.parse.urlsplit(url)
    if not parts.query:
        return url
    pairs = urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
    cleaned = [(key, "REDACTED" if key in ("t", "p", "jwt") else value) for key, value in pairs]
    return urllib.parse.urlunsplit(parts._replace(query=urllib.parse.urlencode(cleaned)))


class NavidromeClient:
    """Minimal Subsonic API client covering the calls this script needs."""

    def __init__(
        self,
        base_url: str,
        username: str,
        password: str,
        *,
        timeout: float = 60.0,
        insecure: bool = False,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.username = username
        self._password = password
        self.timeout = timeout
        # Albums that could not be listed during the last fetch_library() call.
        self.library_errors = 0
        handlers = []
        if insecure:
            context = ssl.create_default_context()
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
            LOG.warning("TLS certificate verification is disabled (--insecure)")
            handlers.append(urllib.request.HTTPSHandler(context=context))
        self._opener = urllib.request.build_opener(*handlers)

    # -- plumbing ----------------------------------------------------------

    def _auth_params(self) -> dict:
        salt = secrets.token_hex(8)
        return {"t": _md5_hex((self._password + salt).encode("utf-8")), "s": salt}

    def _build_url(self, endpoint: str, params: Optional[dict] = None) -> str:
        query = {
            "u": self.username,
            "v": SUBSONIC_API_VERSION,
            "c": CLIENT_NAME,
            "f": "json",
        }
        query.update(self._auth_params())
        if params:
            query.update({key: str(value) for key, value in params.items()})
        return f"{self.base_url}/rest/{endpoint}.view?{urllib.parse.urlencode(query)}"

    @staticmethod
    def _raise_for_subsonic_failure(endpoint: str, payload: dict) -> None:
        response = payload.get("subsonic-response") or {}
        if response.get("status") == "failed":
            error = response.get("error") or {}
            raise SubsonicError(
                f"{endpoint} failed: {error.get('message', 'unknown error')}",
                code=error.get("code"),
                http_status=200,
            )

    def call(self, endpoint: str, params: Optional[dict] = None, *, _retried: bool = False) -> dict:
        """Call *endpoint* and return the ``subsonic-response`` object."""
        url = self._build_url(endpoint, params)
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
        LOG.debug("GET %s", redact_url(url))
        try:
            with self._opener.open(request, timeout=self.timeout) as response:
                body = response.read()
                retry_after = _retry_after_seconds(response.headers)
        except urllib.error.HTTPError as exc:
            body = b""
            try:
                body = exc.read()
            except Exception:  # pragma: no cover - defensive
                pass
            retry_after = _retry_after_seconds(exc.headers)
            if exc.status == 401 and not _retried:
                LOG.debug("Authentication rejected, retrying once")
                return self.call(endpoint, params, _retried=True)
            raise SubsonicError(
                f"{endpoint} failed: HTTP {exc.status} {_short_body(body)}",
                http_status=exc.status,
                retry_after=retry_after,
            ) from exc
        except (urllib.error.URLError, socket.timeout, http.client.HTTPException) as exc:
            raise SubsonicError(f"{endpoint} failed: {exc}", retryable=True) from exc

        try:
            payload = json.loads(body)
        except ValueError as exc:
            raise SubsonicError(
                f"{endpoint} returned a non-JSON response: {_short_body(body)}", retryable=True
            ) from exc
        self._raise_for_subsonic_failure(endpoint, payload)
        return payload.get("subsonic-response") or {}

    def open_stream(self, song_id: str, fmt: str, bitrate: int) -> tuple:
        """Open a stream and return ``(response, first_bytes, headers)``.

        ``estimateContentLength`` is deliberately not requested: Navidrome derives
        it from the nominal bitrate, so VBR libopus output arrives with a
        ``Content-Length`` a percent or so above the real size. Completeness is
        verified from the Ogg container instead.
        """
        params = {"id": song_id, "format": fmt, "maxBitRate": str(bitrate)}
        url = self._build_url("stream", params)
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "audio/*"})
        LOG.debug("GET %s", redact_url(url))
        try:
            response = self._opener.open(request, timeout=self.timeout)
        except urllib.error.HTTPError as exc:
            body = b""
            try:
                body = exc.read()
            except Exception:  # pragma: no cover - defensive
                pass
            retry_after = _retry_after_seconds(exc.headers)
            if exc.status == 401:
                raise SubsonicError(
                    "stream failed: HTTP 401 (credentials rejected)",
                    http_status=401,
                    retry_after=retry_after,
                ) from exc
            raise SubsonicError(
                f"stream failed: HTTP {exc.status} {_short_body(body)}",
                http_status=exc.status,
                retry_after=retry_after,
            ) from exc
        except (urllib.error.URLError, socket.timeout, http.client.HTTPException) as exc:
            raise SubsonicError(f"stream failed: {exc}", retryable=True) from exc

        try:
            head = response.read(SNIFF_PREFIX_BYTES)
        except Exception as exc:  # pragma: no cover - defensive
            response.close()
            raise SubsonicError(f"stream failed while reading response: {exc}", retryable=True) from exc
        return response, head, response.headers

    # -- endpoints ---------------------------------------------------------

    def ping(self) -> dict:
        response = self.call("ping")
        LOG.info(
            "Connected to %s (Navidrome %s, API %s)",
            self.base_url,
            response.get("serverVersion", "unknown"),
            response.get("type", "unknown"),
        )
        return response

    def fetch_favorites(self) -> list:
        response = self.call("getStarred2")
        starred = response.get("starred2") or {}
        # getStarred2 also returns starred artists and albums; keep only real tracks.
        return _playable_songs(starred.get("song"))

    def fetch_song(self, song_id: str) -> Optional[dict]:
        """Return the metadata of one track, or ``None`` if it is not playable.

        One request per id, which is what makes ``--song-id`` usable on a huge
        library: nothing else is enumerated.
        """
        response = self.call("getSong", {"id": song_id})
        # getSong answers with a single song object rather than a list.
        entries = _as_entries(response.get("song"))
        if not entries:
            return None
        songs = _playable_songs([_normalise_song(entries[0])])
        return songs[0] if songs else None

    def search_songs(self, query: str) -> list:
        """Return the tracks matching *query*, one ``search3`` page at a time."""
        offset = 0
        found: list = []
        seen: set = set()
        for _page in range(MAX_SEARCH_PAGES):
            response = self.call(
                "search3",
                {
                    "query": query,
                    "songCount": str(SEARCH_PAGE_SIZE),
                    "songOffset": str(offset),
                    "artistCount": "0",
                    "albumCount": "0",
                },
            )
            results = response.get("searchResult3") or {}
            # search3 also returns matching artists and albums; only songs stream.
            songs = _playable_songs([_normalise_song(song) for song in _as_entries(results.get("song"))])
            if not songs:
                return found
            for song in songs:
                song_id = str(song["id"])
                if song_id in seen:
                    continue
                seen.add(song_id)
                found.append(song)
            if len(songs) < SEARCH_PAGE_SIZE:
                return found
            offset += SEARCH_PAGE_SIZE
        LOG.warning(
            "Stopped after %d search pages (%d matches) for %r; narrow the query or "
            "list the song ids instead",
            MAX_SEARCH_PAGES,
            len(found),
            query,
        )
        return found

    def iter_albums(self) -> Iterator[dict]:
        """Yield every album the account can see, one ``getAlbumList2`` page at a time."""
        offset = 0
        for _page in range(MAX_ALBUM_PAGES):
            response = self.call(
                "getAlbumList2",
                {
                    "type": "alphabeticalByName",
                    "size": str(ALBUM_PAGE_SIZE),
                    "offset": str(offset),
                },
            )
            albums = _as_entries((response.get("albumList2") or {}).get("album"))
            if not albums:
                return
            for album in albums:
                if album.get("id"):
                    yield album
            # A short page means the library ends here.
            if len(albums) < ALBUM_PAGE_SIZE:
                return
            offset += ALBUM_PAGE_SIZE
        LOG.warning(
            "Stopped after %d album pages (%d albums); the library may be bigger than that",
            MAX_ALBUM_PAGES,
            MAX_ALBUM_PAGES * ALBUM_PAGE_SIZE,
        )

    def fetch_album_songs(self, album_id: str) -> list:
        """Return every playable track of one album."""
        response = self.call("getAlbum", {"id": album_id})
        return _playable_songs((response.get("album") or {}).get("song"))

    def fetch_library(self, limit: int = 0) -> list:
        """Enumerate every track in the library.

        Albums are paged with ``getAlbumList2`` and expanded with ``getAlbum``,
        which returns a whole album in one response and therefore cannot leave a
        gap in the middle of a large library the way paging songs can.

        A single album that cannot be read is logged and counted in
        ``library_errors`` rather than failing the whole run; only a failure of
        the album list itself propagates. ``limit`` stops the scan as soon as
        enough tracks have been collected.
        """
        self.library_errors = 0
        tracks: list = []
        seen: set = set()
        albums = 0

        for album in self.iter_albums():
            albums += 1
            album_name = str(album.get("name") or album.get("album") or "?")
            try:
                songs = self.fetch_album_songs(str(album["id"]))
            except SubsonicError as exc:
                self.library_errors += 1
                LOG.warning("Skipping album %s: %s", album_name, exc)
                continue
            for song in songs:
                song_id = str(song["id"])
                if song_id in seen:
                    # The same track can be listed twice when albums move
                    # between scans; a track is only ever downloaded once.
                    continue
                seen.add(song_id)
                tracks.append(song)
            if albums % ALBUM_PROGRESS_EVERY == 0:
                LOG.info("Enumerated %d album(s), %d track(s) so far", albums, len(tracks))
            if limit and limit > 0 and len(tracks) >= limit:
                LOG.info("Stopping the scan at %d track(s) (--limit)", limit)
                return tracks[:limit]

        if self.library_errors:
            LOG.warning(
                "%d of %d album(s) could not be listed and were skipped", self.library_errors, albums
            )
        return tracks

    def fetch_cover_art(self, cover_id: str, size: int) -> bytes:
        if not cover_id:
            raise SubsonicError("no coverArt id available for this album")
        url = self._build_url("getCoverArt", {"id": cover_id, "size": str(size)})
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "image/*"})
        try:
            with self._opener.open(request, timeout=self.timeout) as response:
                return response.read()
        except urllib.error.HTTPError as exc:
            raise SubsonicError(
                f"getCoverArt failed: HTTP {exc.status}", http_status=exc.status
            ) from exc
        except (urllib.error.URLError, socket.timeout, http.client.HTTPException) as exc:
            raise SubsonicError(f"getCoverArt failed: {exc}", retryable=True) from exc


def _short_body(body: bytes, limit: int = 200) -> str:
    if not body:
        return ""
    text = body[:limit].decode("utf-8", "replace").strip()
    return f"({text})" if text else ""


# ---------------------------------------------------------------------------
# Track selection
# ---------------------------------------------------------------------------
#
# A run has one scope -- what it wants -- and one answer to "should I ask?".
# Only the scope is exclusive:
#
#   --song-id ......... one getSong per id, nothing else is enumerated
#   --search .......... paged search3, the server decides what matches
#   --artist/--album .. the normal library walk, then a substring filter
#   --favorites-only .. getStarred2
#   (none of those) ... the whole library
#
# Asking is the default whenever there is a terminal to ask on. --all turns that
# off, and --pick insists on it, so an unattended --pick fails loudly rather than
# quietly downloading everything. Both compose with a scope, which is what makes
# "--search something, then pick from the hits" a single command.
#
# ``Selection`` carries the answer plus everything the rest of the run needs to
# describe it: the noun for the "Found N ..." line, why an empty list is empty,
# and the count of explicit song ids the server could not resolve.


@dataclasses.dataclass
class Selection:
    songs: list
    scope: str
    errors: int = 0
    # Why the list is empty, when it is; a run with nothing to do is a success,
    # but only if the log says so rather than falling silent.
    note: str = ""


def _stdin_is_tty() -> bool:
    """Whether there is a human on the other end of stdin to answer a prompt."""
    try:
        return bool(sys.stdin) and sys.stdin.isatty()
    except (AttributeError, ValueError):  # pragma: no cover - closed stdin
        return False


def _split_ids(values: Optional[Sequence[str]]) -> list:
    """Flatten repeated and comma-joined flag values into one ordered id list."""
    ids: list = []
    for value in values or ():
        for part in str(value).split(","):
            part = part.strip()
            if part and part not in ids:
                ids.append(part)
    return ids


def filter_songs(songs: Sequence, artist: str = "", album: str = "") -> list:
    """Keep the tracks whose artist/album contain *artist*/*album*.

    Case-insensitive and substring based, so ``--artist radiohead`` matches
    "Radiohead" and ``--album "blue in"`` finds a multi-word album.
    """
    wanted_artist = artist.casefold().strip()
    wanted_album = album.casefold().strip()
    if not wanted_artist and not wanted_album:
        return list(songs)

    def matches(song: dict) -> bool:
        if wanted_artist and wanted_artist not in str(song.get("artist") or "").casefold():
            return False
        if wanted_album and wanted_album not in str(song.get("album") or "").casefold():
            return False
        return True

    return [song for song in songs if matches(song)]


def format_duration(seconds: Any) -> str:
    """Format a Subsonic ``duration`` as ``M:SS`` (``H:MM:SS`` when long)."""
    total = _as_int(seconds)
    if total <= 0:
        return "--:--"
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def _track_label(song: dict) -> str:
    """The track's position within its album, e.g. ``03`` or ``02-05``."""
    track = _as_int(song.get("track"))
    disc = _as_int(song.get("discNumber"))
    if disc > 1:
        return f"{disc:02d}-{track:02d}"
    if track > 0:
        return f"{track:02d}"
    return "--"


def _display_width(text: str) -> int:
    """How many terminal columns *text* occupies.

    CJK and emoji are two cells wide but one character, and a combining mark
    takes none, so ``len()`` would misalign every table containing them.
    """
    width = 0
    for char in text:
        if unicodedata.combining(char):
            continue
        width += 2 if unicodedata.east_asian_width(char) in ("W", "F") else 1
    return width


def _fit(text: str, width: int) -> str:
    """Pad, or truncate with no ellipsis, to exactly *width* columns."""
    text = str(text)
    current = _display_width(text)
    if current == width:
        return text
    if current < width:
        return text + " " * (width - current)

    kept = ""
    used = 0
    for char in text:
        size = _display_width(char)
        if used + size > width:
            break
        kept += char
        used += size
    return kept + " " * (width - used)


def _parse_token(token: str, count: int) -> list:
    """Expand one answer token into 0-based indexes: ``5`` or ``2-7``."""
    if token.isdigit():
        index = int(token)
        if not 1 <= index <= count:
            raise SelectionError(f"{index} is out of range; there are {count} candidate(s)")
        return [index - 1]

    parts = token.split("-")
    if len(parts) != 2 or not all(part.strip().isdigit() for part in parts):
        raise SelectionError(f"{token!r} is not a track number or a range like 2-7")
    low, high = (int(part) for part in parts)
    if low > high:
        low, high = high, low
    # Track numbers start at 1, so 0-2 would otherwise wrap round to the last track.
    if low < 1:
        raise SelectionError(f"{token!r} is out of range; track numbers start at 1")
    if high > count:
        raise SelectionError(f"{high} is out of range; there are {count} candidate(s)")
    return list(range(low - 1, high))


def parse_selection(text: str, count: int) -> list:
    """Turn an answer at the prompt into 0-based indexes into *count* items.

    Accepts ``2``, ``2,5``, ``2 5``, ``2-7``, ``2,5-7,9``, ``all``, ``none`` and
    ``q``. Anything else raises ``SelectionError`` with a message the prompt can
    show as-is, because a wrong guess here would download the wrong songs.
    """
    answer = (text or "").strip().casefold()
    if answer in (PICKER_QUIT, "quit", "exit"):
        return []
    if answer in (PICKER_ALL, "*", "a"):
        return list(range(count))
    if answer in (PICKER_NONE, "n", "nothing"):
        return []
    if count <= 0:
        raise SelectionError("there is nothing to choose from")

    chosen: list = []
    # "4 - 5" should mean the same as "4-5".
    answer = re.sub(r"\s*-\s*", "-", answer)
    for token in answer.replace(",", " ").split():
        chosen.extend(_parse_token(token, count))
    if not chosen:
        raise SelectionError("no track numbers given")
    # Listed order, and never the same track twice.
    return sorted(set(chosen))


def _write_candidate_table(
    songs: Sequence, writer, *, page_size: int = PICKER_PAGE_SIZE, reader=None
) -> None:
    """Print the numbered candidate list, a screenful at a time.

    Every candidate is always shown: a library too large for one screen is paged
    rather than truncated, because a hidden track cannot be selected and its
    absence would be invisible.
    """
    write = getattr(writer, "write", None)
    total = len(songs)
    total_width = len(str(total))

    def title_of(song: dict) -> str:
        return f"{song.get('artist') or '?'} - {song.get('title') or '?'}"

    title_width = min(60, max((_display_width(title_of(song)) for song in songs), default=1))
    album_width = min(40, max((_display_width(song.get("album") or "?") for song in songs), default=1))
    id_width = max((len(str(song.get("id") or "")) for song in songs), default=1)

    for start in range(0, total, page_size):
        page = songs[start : start + page_size]
        if start:
            write("\n")
        for offset, song in enumerate(page, start=start + 1):
            write(
                f"{offset:>{total_width}}  {_track_label(song):<5}  "
                f"{_fit(title_of(song), title_width)}  "
                f"{_fit(song.get('album') or '?', album_width)}  "
                f"{format_duration(song.get('duration')):>7}  "
                f"{_fit(song.get('id') or '', id_width)}\n"
            )
        remaining = total - (start + len(page))
        if remaining:
            write(f"-- {remaining} more candidate(s); press Enter for the next page --\n")
            if reader is not None:
                reader()


def prompt_for_tracks(
    songs: Sequence,
    *,
    reader=None,
    writer=None,
    page_size: int = PICKER_PAGE_SIZE,
) -> list:
    """Show *songs* and return the ones the user picked, in listed order.

    An empty list means the user quit, typed ``none``, or closed stdin. Only a
    candidate list with nothing in it raises, which is a bug rather than a choice.
    """
    if reader is None:
        reader = input
    if writer is None:
        writer = sys.stdout
    if not songs:
        raise SelectionError("there is nothing to choose from")
    write = getattr(writer, "write", None)
    total = len(songs)

    try:
        _write_candidate_table(songs, writer, page_size=page_size, reader=reader)
        while True:
            hint = f", 1-{total}" if total > 1 else ""
            write(f"\n{total} candidate(s). Which to download? [e.g. 2,5-7{hint}, all, none, q] ")
            try:
                chosen = parse_selection(reader(), total)
            except SelectionError as exc:
                write(f"{exc}. Try again.\n")
                continue
            return [songs[index] for index in chosen]
    except EOFError:
        # Ctrl-D: the same as typing q, not a crash.
        write("\n")
        return []


def resolve_scope(client: NavidromeClient, args: argparse.Namespace) -> Selection:
    """Work out which tracks this run should download.

    Raises ``SelectionError`` for a scope that cannot produce a list at all
    (no resolvable song ids, or a ``--pick`` with no terminal to ask on) and lets
    ``SubsonicError`` from the API calls propagate.
    """
    prompting = _should_prompt(args)

    if args.song_id:
        # An explicit list of ids is already the whole answer.
        return _select_by_song_id(client, _split_ids(args.song_id))

    if args.search:
        songs = client.search_songs(args.search)
        return _finish(
            songs,
            f"track(s) matching {args.search!r}",
            f"no track matched {args.search!r}",
            prompting,
        )

    if args.artist or args.album:
        # A filter needs every track to match against: stopping the scan early
        # could miss the album the wanted artist is on, and would answer a
        # different question than the one asked. So --limit cannot shorten this
        # walk, and is applied to the matches instead.
        wanted = " and ".join(
            part
            for part in (
                f"artist ~ {args.artist!r}" if args.artist else "",
                f"album ~ {args.album!r}" if args.album else "",
            )
            if part
        )
        candidates = _enumerate(client, args, whole_library=True, complete=True)
        return _finish(
            filter_songs(candidates, args.artist or "", args.album or ""),
            f"track(s) with {wanted}",
            f"no track in the library has {wanted}",
            prompting,
        )

    if args.favorites_only:
        return _finish(
            client.fetch_favorites(),
            "favorite track(s)",
            f"no favorites found for user {args.user!r}",
            prompting,
        )

    return _finish(
        _enumerate(client, args, whole_library=True, complete=prompting),
        "track(s) in the library",
        f"no tracks found in the library visible to user {args.user!r}",
        prompting,
    )


def _finish(candidates: list, scope: str, note: str, prompting: bool) -> Selection:
    """Apply the prompt, if one is due, and describe the outcome.

    Candidates are only asked about when there are any: an empty list has nothing
    to choose from, and the reason it is empty is more useful than a prompt.
    """
    if not prompting or not candidates:
        return Selection(songs=candidates, scope=scope, note=note)

    picks = prompt_for_tracks(candidates)
    return Selection(
        songs=picks,
        # "chosen track(s) from 3 candidate(s)" restates what the prompt showed.
        scope=f"chosen track(s) from {len(candidates)} candidate(s)",
        note="nothing was selected, so there is nothing to download",
    )


def _should_prompt(args: argparse.Namespace) -> bool:
    """Whether to stop and ask which of the candidates to download.

    Prompting needs a terminal, and is the default there. ``--all`` turns it
    off; ``--pick`` insists on it, so an unattended ``--pick`` fails loudly
    instead of quietly downloading everything.
    """
    if args.all:
        return False
    if args.pick:
        if not _stdin_is_tty():
            raise SelectionError(
                "--pick needs a terminal to ask on: run it interactively, or use "
                "--search/--artist/--album/--song-id, or drop --pick to take everything"
            )
        return True
    return _stdin_is_tty()


def _enumerate(
    client: NavidromeClient, args: argparse.Namespace, *, whole_library: bool, complete: bool = False
) -> list:
    """List the candidate tracks for a scope.

    ``complete`` forces the full walk, because a filter needs every track to
    match against and the prompt must never be handed a list that is quietly
    missing tracks. Otherwise ``--limit`` is allowed to stop the scan early,
    which is the whole point of it on a large library.
    """
    if not whole_library:
        return client.fetch_favorites()
    return client.fetch_library(limit=0 if complete else max(args.limit, 0))


def _select_by_song_id(client: NavidromeClient, song_ids: Sequence[str]) -> Selection:
    songs: list = []
    unresolved: list = []
    for song_id in song_ids:
        try:
            song = client.fetch_song(song_id)
        except SubsonicError as exc:
            unresolved.append(song_id)
            LOG.warning("Could not look up song %s: %s", song_id, exc)
            continue
        if song is None:
            unresolved.append(song_id)
            LOG.warning("No playable track with id %s", song_id)
            continue
        if song not in songs:
            songs.append(song)

    if not songs:
        raise SelectionError(
            "none of the requested song ids could be read: " + ", ".join(unresolved)
        )
    if unresolved:
        LOG.warning("%d song id(s) could not be read: %s", len(unresolved), ", ".join(unresolved))
    return Selection(
        songs=songs,
        scope=f"requested track(s) ({len(songs)} of {len(song_ids)} id(s))",
        errors=len(unresolved),
    )


# ---------------------------------------------------------------------------
# Manifest
# ---------------------------------------------------------------------------


class Manifest:
    """Append-only JSONL record of every track attempt.

    Also keeps an in-memory index of which song produced which file, so a
    second run can tell "already downloaded" apart from "a different track
    happens to normalise to the same filename".
    """

    def __init__(self, path: Path, *, enabled: bool = True) -> None:
        self.path = path
        self.enabled = enabled
        self._lock = threading.Lock()
        self.by_path: dict = {}
        if enabled:
            self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            with open(self.path, encoding="utf-8") as handle:
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        record = json.loads(line)
                    except ValueError:
                        continue
                    if record.get("status") == "downloaded" and record.get("path"):
                        self.by_path[str(record["path"])] = str(record.get("id") or "")
        except OSError as exc:
            LOG.warning("Could not read existing manifest %s: %s", self.path, exc)

    def owner_of(self, path: Path) -> Optional[str]:
        """Return the song id that produced *path*, if known."""
        return self.by_path.get(str(path))

    def reserve(self, canonical: Path, song_id: str) -> Path:
        """Claim a free path for *song_id*, avoiding names taken by other tracks.

        Callers must have already decided that *canonical* is not usable, so this
        may return it unchanged. Claims are serialised because ``--workers`` runs
        several tracks at once.
        """
        with self._lock:
            candidate = canonical
            index = 2
            while (self.by_path.get(str(candidate)) not in (None, song_id)) or (
                candidate != canonical and candidate.exists()
            ):
                candidate = canonical.with_name(f"{canonical.stem} ({index}){canonical.suffix}")
                index += 1
            self.by_path[str(candidate)] = song_id
            return candidate

    def write(self, record: dict) -> None:
        if not self.enabled:
            return
        line = json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n"
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.path, "a", encoding="utf-8") as handle:
                handle.write(line)
                handle.flush()
                os.fsync(handle.fileno())
            if record.get("status") == "downloaded" and record.get("path"):
                self.by_path[str(record["path"])] = str(record.get("id") or "")


# ---------------------------------------------------------------------------
# Downloader
# ---------------------------------------------------------------------------


@dataclasses.dataclass
class TrackResult:
    song: dict
    status: str  # downloaded | skipped | failed
    path: Optional[Path] = None
    size: int = 0
    attempts: int = 0
    audio_format: str = ""
    content_type: str = ""
    cover_embedded: bool = False
    cover_written: bool = False
    error: str = ""


class FavSyncer:
    def __init__(self, client: NavidromeClient, args: argparse.Namespace) -> None:
        self.client = client
        self.args = args
        self.out_dir = Path(args.out).expanduser()
        self.manifest = Manifest(self.out_dir / args.manifest_name, enabled=not args.dry_run)
        self.stop = threading.Event()
        self._cover_lock = threading.Lock()
        self._cover_cache: dict = {}
        self.stats = {"downloaded": 0, "skipped": 0, "failed": 0}
        self._stats_lock = threading.Lock()
        self.failures: list = []

    # -- helpers -----------------------------------------------------------

    def _bump(self, key: str) -> None:
        with self._stats_lock:
            self.stats[key] += 1

    def _backoff(self, attempt: int, retry_after: Optional[float] = None) -> float:
        if retry_after is not None:
            return min(retry_after, DEFAULT_BACKOFF_CAP)
        window = min(DEFAULT_BACKOFF_CAP, DEFAULT_BACKOFF_BASE * (2**attempt))
        return random.uniform(0, window)  # noqa: S311 - jitter, not cryptography

    def _is_retryable(self, error: Exception) -> tuple:
        """Decide whether *error* is worth another attempt, and how long to wait."""
        if isinstance(error, NotTranscodedError):
            # A server misconfiguration; retrying cannot change the answer.
            return False, None
        if isinstance(error, SubsonicError):
            if error.retryable is not None:
                return error.retryable, error.retry_after
            if error.http_status == 401:
                return False, None
            if error.http_status is not None:
                return (error.http_status in RETRYABLE_HTTP_STATUS, error.retry_after)
            if error.code is not None:
                return (error.code in RETRYABLE_SUBSONIC_CODES, error.retry_after)
            # Neither an HTTP status nor a Subsonic code: a transport-level
            # problem, which is exactly what retrying helps with.
            return True, error.retry_after
        if isinstance(error, (OSError, http.client.HTTPException)):
            return True, None
        return False, None

    @staticmethod
    def _is_transcoder_fault(error: Exception) -> bool:
        text = str(error).lower()
        return any(marker in text for marker in TRANSCODER_FAULT_MARKERS)

    # -- cover art ---------------------------------------------------------

    def _cover_for(self, song: dict, album_dir: Path) -> Optional[bytes]:
        """Return cover bytes for *song*'s album, fetching at most once per album."""
        if self.args.no_cover:
            return None
        cover_path = album_dir / COVER_FILENAME
        key = str(song.get("coverArt") or song.get("albumId") or album_dir.name)

        with self._cover_lock:
            if key in self._cover_cache:
                return self._cover_cache[key]
            if cover_path.exists() and cover_path.stat().st_size > 0:
                data = cover_path.read_bytes()
                LOG.debug("Reusing existing %s", cover_path)
                self._cover_cache[key] = data
                return data

            cover_id = song.get("coverArt") or song.get("albumId")
            if not cover_id:
                return None
            try:
                data = self.client.fetch_cover_art(str(cover_id), self.args.cover_size)
            except SubsonicError as exc:
                LOG.warning("Cover art unavailable for %s: %s", album_dir.name, exc)
                self._cover_cache[key] = b""
                return None
            if not data:
                self._cover_cache[key] = b""
                return None

            try:
                cover_path.write_bytes(data)
            except OSError as exc:
                LOG.warning("Could not write %s: %s", cover_path, exc)
            self._cover_cache[key] = data
            LOG.info("Saved %s (%s)", cover_path.name, _format_bytes(len(data)))
            return data

    def _embed_cover(self, track_path: Path, cover: bytes) -> None:
        if self.args.no_embed or not cover:
            return
        try:
            original = track_path.read_bytes()
            original_audio = _audio_payload_size(original)
            rewritten, info = embed_cover_art(original, cover)
            if _audio_payload_size(rewritten) != original_audio:
                raise OggError("audio payload size changed while rewriting the container")
            verify_opus_with_cover(rewritten, cover)
        except (OSError, OggError) as exc:
            LOG.warning("Leaving %s without embedded art: %s", track_path.name, exc)
            return

        temp_path = track_path.with_name(track_path.name + PARTIAL_SUFFIX)
        try:
            with open(temp_path, "wb") as handle:
                handle.write(rewritten)
                handle.flush()
                os.fsync(handle.fileno())
            # Confirm what actually landed on disk before replacing the good file.
            on_disk = temp_path.read_bytes()
            verify_opus_with_cover(on_disk, cover)
            os.replace(temp_path, track_path)
        except (OSError, OggError) as exc:
            LOG.warning("Could not embed art in %s: %s", track_path.name, exc)
            temp_path.unlink(missing_ok=True)
            return
        LOG.info(
            "Embedded cover art in %s (%sx%s %s, %.1f KiB)",
            track_path.name,
            info["width"],
            info["height"],
            info["mime"],
            len(cover) / 1024,
        )

    # -- per-track work ----------------------------------------------------

    def _stream_once(self, song: dict, destination: Path) -> tuple:
        """Fetch one track to *destination*. Returns ``(bytes, fmt, content_type)``."""
        song_id = str(song["id"])
        response, head, headers = self.client.open_stream(song_id, self.args.format, self.args.bitrate)
        try:
            content_type = (headers.get("Content-Type") or "").split(";")[0].strip()
            audio_format = sniff_audio_format(head)

            if not head:
                raise SubsonicError("server returned an empty audio stream", retryable=True)
            if audio_format not in ("opus", "ogg"):
                # A 200 response here means Subsonic returned an error document.
                body = head + response.read(65536)
                subsonic = self._parse_error_document(body)
                if subsonic is not None:
                    raise subsonic
                raise NotTranscodedError(self._not_transcoded_message(audio_format, content_type))

            expected = _as_int(headers.get("Content-Length")) or 0
            written = 0
            with open(destination, "wb") as handle:
                handle.write(head)
                written += len(head)
                while True:
                    if self.stop.is_set():
                        raise KeyboardInterrupt
                    chunk = response.read(65536)
                    if not chunk:
                        break
                    handle.write(chunk)
                    written += len(chunk)
                handle.flush()
                os.fsync(handle.fileno())

            if expected and written < expected * LENGTH_TOLERANCE:
                raise SubsonicError(
                    f"truncated download: expected about {expected} bytes, received {written}",
                    retryable=True,
                )
            if written <= 0:
                raise SubsonicError("server returned an empty audio stream", retryable=True)

            # Verify the bytes that actually landed, not just the ones counted.
            try:
                stats = assert_complete_opus(Path(destination).read_bytes())
            except OggError as exc:
                raise SubsonicError(f"incomplete audio stream: {exc}", retryable=True) from exc
            LOG.debug("Received a complete %d-page Ogg stream (granule %d)", stats["pages"], stats["granule"])
            return written, audio_format, content_type
        finally:
            response.close()

    @staticmethod
    def _parse_error_document(body: bytes) -> Optional[SubsonicError]:
        stripped = body.lstrip()
        if not stripped.startswith(b"{"):
            return None
        try:
            payload = json.loads(body)
        except ValueError:
            return None
        response = payload.get("subsonic-response") or {}
        if response.get("status") != "failed":
            return None
        error = response.get("error") or {}
        return SubsonicError(
            f"stream failed: {error.get('message', 'unknown error')}",
            code=error.get("code"),
            http_status=200,
        )

    def _not_transcoded_message(self, audio_format: str, content_type: str) -> str:
        got = _FORMAT_DISPLAY.get(audio_format, audio_format)
        detail = f" (Content-Type: {content_type})" if content_type else ""
        return (
            f"server returned {got} instead of the requested {self.args.format} transcode{detail}. "
            "Navidrome streams the original file whenever no matching transcoding is configured, "
            "so either ffmpeg is missing from the Navidrome host, or its transcoding configuration "
            f"has no target format '{self.args.format}'. Check the Navidrome logs for "
            "'Error starting transcoder'."
        )

    def _resolve_paths(self, song: dict) -> tuple:
        """Work out where *song* should live and whether we already have it.

        Returns ``(album_dir, path, already_downloaded)``. The canonical name is
        used whenever it is free or already holds this same song, so re-runs
        resume instead of piling up ``(2)`` duplicates. A suffix is only added
        when the canonical name belongs to a *different* track.
        """
        album_dir = self.out_dir / build_album_dir_name(song)
        canonical = album_dir / build_track_filename(song, f".{self.args.format}")
        song_id = str(song.get("id") or "")

        exists = canonical.exists() and canonical.stat().st_size > 0
        owner = self.manifest.owner_of(canonical)
        if exists and not self.args.force and (owner is None or owner == song_id):
            return album_dir, canonical, True
        return album_dir, self.manifest.reserve(canonical, song_id), False

    def process(self, song: dict) -> TrackResult:
        label = f"{song.get('artist', '?')} - {song.get('title', '?')}"
        album_dir, track_path, already = self._resolve_paths(song)
        result = TrackResult(song=song, status="failed", path=track_path)

        if already:
            LOG.info("Skipping %s (already downloaded)", label)
            result.status = "skipped"
            result.size = track_path.stat().st_size
            return result

        if self.args.dry_run:
            result.status = "skipped"
            LOG.info("[dry-run] %s -> %s", label, track_path)
            return result

        try:
            album_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            result.error = f"could not create {album_dir}: {exc}"
            return result

        cover = self._cover_for(song, album_dir)
        part_path = track_path.with_name(track_path.name + PARTIAL_SUFFIX)
        transcoder_fault = False

        for attempt in range(1, self.args.retries + 1):
            if self.stop.is_set():
                result.error = "interrupted"
                break
            result.attempts = attempt
            try:
                size, audio_format, content_type = self._stream_once(song, part_path)
                result.size = size
                result.audio_format = audio_format
                result.content_type = content_type
                result.error = ""  # a previous attempt may have failed
                os.replace(part_path, track_path)
                LOG.info("Downloaded %s (%s)", label, _format_bytes(size))
                break
            except KeyboardInterrupt:
                result.error = "interrupted"
                break
            except (SubsonicError, NotTranscodedError, OggError, OSError, http.client.HTTPException) as exc:
                part_path.unlink(missing_ok=True)
                result.error = str(exc)
                retryable, retry_after = self._is_retryable(exc)
                if not retryable or attempt >= self.args.retries:
                    if self._is_transcoder_fault(exc):
                        transcoder_fault = True
                    break
                delay = self._backoff(attempt - 1, retry_after)
                LOG.warning(
                    "Attempt %d/%d failed for %s: %s (retrying in %.1fs)",
                    attempt,
                    self.args.retries,
                    label,
                    exc,
                    delay,
                )
                if self.stop.wait(delay):
                    result.error = "interrupted"
                    break
        part_path.unlink(missing_ok=True)

        if result.error:
            if transcoder_fault:
                LOG.error("Server-side transcoding looks misconfigured; this will affect every track.")
            return result

        if cover:
            result.cover_written = (album_dir / COVER_FILENAME).exists()
            self._embed_cover(track_path, cover)
            result.cover_embedded = _has_embedded_cover(track_path)
        result.status = "downloaded"
        return result

    # -- run loop ----------------------------------------------------------

    def run(self, songs: Sequence, scope: str = "track(s)") -> dict:
        LOG.info("Found %d %s", len(songs), scope)
        results: list = []
        workers = max(1, self.args.workers)

        if workers == 1:
            for index, song in enumerate(songs, start=1):
                LOG.info("[%d/%d] %s", index, len(songs), _describe(song))
                results.append(self.process(song))
                self._record(results[-1])
                if self.stop.is_set():
                    LOG.warning("Interrupted; stopping after %d of %d tracks", index, len(songs))
                    break
        else:
            processed = 0
            with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="track") as pool:
                futures = [pool.submit(self.process, song) for song in songs]
                for future in as_completed(futures):
                    processed += 1
                    try:
                        result = future.result()
                    except Exception as exc:  # pragma: no cover - defensive
                        result = TrackResult(song={}, status="failed", error=f"unexpected error: {exc}")
                    results.append(result)
                    self._record(result)
                    if self.stop.is_set():
                        LOG.warning("Interrupted after %d of %d tracks", processed, len(songs))
                        break

        for result in results:
            self._bump(result.status)
            if result.status == "failed":
                self.failures.append(result)

        return self._summarise(len(songs), len(results))

    def _record(self, result: TrackResult) -> None:
        song = result.song
        record = {
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "id": song.get("id"),
            "artist": song.get("artist"),
            "album": song.get("album"),
            "title": song.get("title"),
            "disc": song.get("discNumber"),
            "track": song.get("track"),
            "path": str(result.path) if result.path else None,
            "status": result.status,
            "bytes": result.size,
            "audio_format": result.audio_format,
            "content_type": result.content_type,
            "cover_embedded": result.cover_embedded,
            "cover_file": result.cover_written,
            "attempts": result.attempts,
            "error": result.error or None,
        }
        try:
            self.manifest.write(record)
        except OSError as exc:
            LOG.error("Could not write manifest: %s", exc)

    def _summarise(self, total: int, processed: int) -> dict:
        summary = {
            "total": total,
            "processed": processed,
            "downloaded": self.stats["downloaded"],
            "skipped": self.stats["skipped"],
            "failed": self.stats["failed"],
            "failures": [
                {"track": _describe(result.song), "error": result.error, "attempts": result.attempts}
                for result in self.failures
            ],
        }
        return summary


def _has_embedded_cover(track_path: Path) -> bool:
    try:
        data = track_path.read_bytes()
    except OSError:
        return False
    try:
        (_head, tags), _consumed, _serial = _consume_header_packets(iter_ogg_pages(data))
        _vendor, comments = parse_opus_tags(tags)
    except OggError:
        return False
    return any(key.upper() == METADATA_BLOCK_PICTURE for key, _value in comments)


def _audio_payload_size(data: bytes) -> int:
    """Total payload bytes in an Opus stream, excluding the header pages."""
    pages = iter_ogg_pages(data)
    try:
        _packets, _consumed, _serial = _consume_header_packets(pages)
    except OggError:
        return -1
    return sum(len(page.payload) for page in pages)


def _format_bytes(count: int) -> str:
    value = float(count)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if value < 1024 or unit == "GiB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{count} B"


# ---------------------------------------------------------------------------
# Command line
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="navidrome_favsync",
        description=(
            "Download chosen tracks from Navidrome as transcoded Opus files. "
            "In a terminal it asks which of the candidates you want; "
            "pass a scope flag to choose up front."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
examples:
  navidrome_favsync.py --url https://music.example.com --user alice
  navidrome_favsync.py --url https://music.example.com --user alice --all
  navidrome_favsync.py --url https://music.example.com --user alice --search airbag
  navidrome_favsync.py --url https://music.example.com --user alice --search airbag --pick
  navidrome_favsync.py --url https://music.example.com --user alice --artist radiohead
  navidrome_favsync.py --url https://music.example.com --user alice --song-id 0rVIJb7CSgetU1nTv0ARaF
  ND_URL=https://music.example.com navidrome_favsync.py --out ~/Music/music --workers 3
  navidrome_favsync.py --url https://music.example.com --user alice --favorites-only
  navidrome_favsync.py --url https://music.example.com --user alice --dry-run

One scope flag per run: --favorites-only, --song-id, --search, --artist or
--album. With none of them, the whole library is the candidate list and a
terminal is asked which tracks to download; --all skips the question and
--pick insists on it.

The Navidrome server needs ffmpeg and an Opus target format in its transcoding
configuration, otherwise it will stream the original files unchanged.
""",
    )
    parser.add_argument("--url", default=os.environ.get("ND_URL"), help="Navidrome base URL (env: ND_URL)")
    parser.add_argument("--user", "--username", dest="user", default=os.environ.get("ND_USERNAME"),
                        help="Navidrome username (env: ND_USERNAME)")
    parser.add_argument("--password", default=os.environ.get("ND_PASSWORD"),
                        help="Navidrome password (env: ND_PASSWORD; prefer the env var)")
    parser.add_argument("--out", default="music", help="output directory (default: ./music)")
    parser.add_argument("--favorites-only", action="store_true",
                        help="scope: download only starred tracks instead of the whole library")
    parser.add_argument("--song-id", action="append", metavar="ID",
                        help="scope: download these exact song ids (repeatable, or comma-separated)")
    parser.add_argument("--search", metavar="QUERY",
                        help="scope: download every track the server finds for QUERY (title/artist/album)")
    parser.add_argument("--artist", metavar="TEXT",
                        help="scope: download tracks whose artist contains TEXT (case-insensitive)")
    parser.add_argument("--album", metavar="TEXT",
                        help="scope: download tracks whose album contains TEXT (case-insensitive)")
    parser.add_argument("--pick", action="store_true",
                        help="ask which of the candidates to download, and fail without a terminal")
    parser.add_argument("--all", action="store_true",
                        help="never ask; take every candidate track (the default when there is no terminal)")
    parser.add_argument("--format", default="opus", help="target format requested from the server (default: opus)")
    parser.add_argument("--bitrate", type=int, default=192,
                        help=f"target bitrate in kbps, {MIN_OPUS_BITRATE}-{MAX_OPUS_BITRATE} (default: 192)")
    parser.add_argument("--workers", type=int, default=1,
                        help="parallel downloads; each one spawns an ffmpeg transcode on the server (default: 1)")
    parser.add_argument("--retries", type=int, default=5, help="attempts per track (default: 5)")
    parser.add_argument("--timeout", type=float, default=60.0, help="socket timeout in seconds (default: 60)")
    parser.add_argument("--cover-size", type=int, default=DEFAULT_COVER_SIZE,
                        help=f"cover art edge size in pixels (default: {DEFAULT_COVER_SIZE})")
    parser.add_argument("--no-cover", action="store_true", help="do not fetch or save cover art")
    parser.add_argument("--no-embed", action="store_true", help="save cover.jpg but do not embed it in the Opus file")
    parser.add_argument("--force", action="store_true", help="re-download tracks that already exist")
    parser.add_argument("--limit", type=int, default=0,
                        help="only process the first N tracks found (0 = all); stops the library scan early")
    parser.add_argument("--dry-run", action="store_true", help="list what would be downloaded and exit")
    parser.add_argument("--insecure", action="store_true", help="skip TLS certificate verification")
    parser.add_argument("--manifest-name", default="manifest.jsonl", help="manifest filename inside --out")
    parser.add_argument("--log-file", help="also write logs to this file")
    parser.add_argument("--log-level", default="INFO",
                        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"])
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return parser


def configure_logging(args: argparse.Namespace) -> None:
    handlers: list = [logging.StreamHandler(sys.stderr)]
    if args.log_file:
        handlers.append(logging.FileHandler(args.log_file, encoding="utf-8"))
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
        handlers=handlers,
    )


def validate_args(args: argparse.Namespace) -> list:
    problems = []
    if not args.url:
        problems.append("--url is required (or set ND_URL)")
    if not args.user:
        problems.append("--user is required (or set ND_USERNAME)")
    if args.password is None:
        problems.append("--password is required (or set ND_PASSWORD)")
    problems.extend(validate_scope_args(args))
    if not MIN_OPUS_BITRATE <= args.bitrate <= MAX_OPUS_BITRATE:
        problems.append(f"--bitrate must be between {MIN_OPUS_BITRATE} and {MAX_OPUS_BITRATE} kbps")
    if args.retries < 1:
        problems.append("--retries must be at least 1")
    if args.workers < 1:
        problems.append("--workers must be at least 1")
    if args.timeout <= 0:
        problems.append("--timeout must be positive")
    if args.cover_size < 1:
        problems.append("--cover-size must be positive")
    return problems


def validate_scope_args(args: argparse.Namespace) -> list:
    """Check that the run asks for exactly one thing, and that it is a real one.

    One scope per run means a command can never mean two different things, and a
    typo in a filter fails immediately instead of quietly matching everything.
    ``--pick`` and ``--all`` are not scopes: they answer "should I ask?", and are
    allowed with any scope -- except together, which is a contradiction.
    """
    problems = []
    scopes = (
        ("--favorites-only", args.favorites_only),
        ("--song-id", args.song_id),
        ("--search", args.search),
        ("--artist", args.artist),
        ("--album", args.album),
    )
    given = [flag for flag, value in scopes if value]
    if len(given) > 1:
        problems.append(f"choose one scope, not several: {' and '.join(given)}")
    for flag, value in (("--search", args.search), ("--artist", args.artist), ("--album", args.album)):
        if value is not None and not value.strip():
            problems.append(f"{flag} needs a non-empty value")
    if args.pick and args.all:
        problems.append("--pick asks which tracks to download, so it cannot be combined with --all")
    return problems


def _install_signal_handlers(syncer: FavSyncer) -> None:
    def handler(signum, _frame):
        if syncer.stop.is_set():
            raise KeyboardInterrupt
        LOG.warning("Received %s, finishing up (press again to abort)", signal.Signals(signum).name)
        syncer.stop.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, handler)
        except (ValueError, OSError):  # pragma: no cover - not on the main thread
            pass


def main(argv: Optional[list] = None) -> int:
    args = build_parser().parse_args(argv)
    configure_logging(args)

    problems = validate_args(args)
    if problems:
        for problem in problems:
            LOG.error("%s", problem)
        return 2

    if args.url and "://" not in args.url:
        args.url = f"https://{args.url}"

    out_dir = Path(args.out).expanduser()
    if not args.dry_run:
        try:
            out_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            LOG.error("Cannot create output directory %s: %s", out_dir, exc)
            return 2

    client = NavidromeClient(args.url, args.user, args.password, timeout=args.timeout, insecure=args.insecure)

    try:
        client.ping()
    except SubsonicError as exc:
        if exc.code == SUBSONIC_AUTH_FAIL or exc.http_status in (401, 403):
            LOG.error("Authentication failed for user %r at %s. Check --user/--password.", args.user, args.url)
        elif exc.http_status in RETRYABLE_HTTP_STATUS:
            LOG.error("Server is unavailable: %s", exc)
        else:
            LOG.error("Could not reach Navidrome: %s", exc)
        return 2

    try:
        selection = resolve_scope(client, args)
    except SelectionError as exc:
        LOG.error("%s", exc)
        return 2
    except SubsonicError as exc:
        LOG.error("Could not work out which tracks to download: %s", exc)
        return 2
    except KeyboardInterrupt:
        LOG.warning("Aborted while listing tracks")
        return 130

    songs = selection.songs
    if not songs:
        LOG.info("%s", selection.note or "nothing to download")
        return 0

    if args.limit and args.limit > 0 and len(songs) > args.limit:
        LOG.info("Limiting run to the first %d of %d track(s) (--limit)", args.limit, len(songs))
        songs = songs[: args.limit]

    syncer = FavSyncer(client, args)
    _install_signal_handlers(syncer)

    try:
        summary = syncer.run(songs, selection.scope)
    except KeyboardInterrupt:
        LOG.warning("Aborted")
        return 130

    LOG.info(
        "Done: %d downloaded, %d skipped, %d failed (of %d)",
        summary["downloaded"],
        summary["skipped"],
        summary["failed"],
        summary["total"],
    )
    if summary["failures"]:
        LOG.warning("%d track(s) failed:", len(summary["failures"]))
        for failure in summary["failures"]:
            LOG.warning("  %s -> %s", failure["track"], failure["error"])
    if client.library_errors:
        LOG.warning(
            "%d album(s) could not be listed, so those tracks are missing from this run",
            client.library_errors,
        )
        return 1
    if selection.errors:
        LOG.warning(
            "%d requested song id(s) could not be read, so those tracks are missing from this run",
            selection.errors,
        )
        return 1
    return 1 if summary["failed"] else 0


if __name__ == "__main__":
    sys.exit(main())
