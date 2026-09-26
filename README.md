# navidrome_favsync

Download your [Navidrome](https://www.navidrome.org) library as Opus files,
transcoded on the server to a bitrate you choose. The whole library is mirrored
by default; `--favorites-only` narrows it to the tracks you've starred.

- Walks every album in the library and saves tracks as `Artist - Album/NN - Title.opus`
- `--favorites-only` downloads just your starred tracks instead
- Asks the server for a real Opus transcode, and **refuses** to save a FLAC wearing
  an `.opus` extension
- Saves a `cover.jpg` per album *and* embeds the artwork into each `.opus` file
- Skips anything already downloaded, so re-running is safe and cheap
- Backs off and retries through flaky networks and server-side rate limits
- Records every attempt in `manifest.jsonl`

No dependencies. Python 3.9+ standard library only — no `pip install`, no
`requests`, no `ffmpeg` on your machine.

---

## Requirements

| Where | What you need |
|---|---|
| **Your machine** | Python 3.9 or newer. Nothing else. |
| **The Navidrome server** | `ffmpeg`, plus an Opus target format in Navidrome's transcoding config |

That second row is the one people trip over. Navidrome transcodes **server-side**.
If `ffmpeg` is missing from the server or its container, or no Opus target format
is configured, Navidrome quietly streams your original FLAC instead. This script
detects that and fails the track with a clear explanation rather than handing you
a mislabelled file.

Your instance is fine if transcoding already works in the web player with an Opus
format selected. To check on the server:

```bash
ffmpeg -version                       # must exist
# then in Navidrome: Settings -> Transcoding -> confirm an "opus" target format
```

## Install

It's a single self-contained file. Copy it anywhere and run it:

```bash
cp navidrome_favsync.py ~/.local/bin/  && chmod +x ~/.local/bin/navidrome_favsync.py
```

No dependencies to install, no virtualenv, no `pip`. `test_navidrome_favsync.py` is
optional and only needed if you want to run the test suite.

## Quick start

Try it without writing anything:

```bash
./navidrome_favsync.py \
  --url https://navi.550441.xyz \
  --user test \
  --password 'your-password' \
  --dry-run
```

Then for real:

```bash
./navidrome_favsync.py \
  --url https://navi.550441.xyz \
  --user test \
  --password 'your-password' \
  --out ~/Music
```

Keeping the password out of your shell history and out of `ps` output is nicer:

```bash
export ND_URL=https://navi.550441.xyz
export ND_USERNAME=test
export ND_PASSWORD='your-password'      # or: read -rs ND_PASSWORD
./navidrome_favsync.py --out ~/Music
```

Just the starred tracks, as before:

```bash
./navidrome_favsync.py --favorites-only --out ~/Music/favorites
```

A scheduled sync, e.g. nightly at 4am:

```cron
17 4 * * * ND_URL=https://navi.550441.xyz ND_USERNAME=test ND_PASSWORD=... \
           /path/to/navidrome_favsync.py --out /srv/music >> /var/log/favsync.log 2>&1
```

## What gets downloaded

By default: **every track in every album the account can see**. The script
enumerates the library before downloading anything:

1. `getAlbumList2` with `type=alphabeticalByName` pages through the albums, 500
   at a time
2. one `getAlbum` call per album returns that album's tracks

So the scan costs one extra small JSON request per album, negligible next to
streaming a full transcode of every track. What it buys is completeness:
`getAlbum` returns a whole album in a single response, so a big library can't
lose tracks to a paging gap the way paging songs by offset can.

A few things worth knowing about a large library:

- The scan logs progress (`Enumerated 500 album(s), 7421 track(s) so far`) every
  25 albums, so a slow start is visible rather than looking hung
- `Ctrl-C` during the scan aborts cleanly with exit code `130`
- An album that can't be listed is logged, counted, and skipped — the rest of the
  run continues, and the exit code is `1` so you know the mirror is incomplete
- Albums added *while* a long run is in progress may fall into a shifted page and
  be missed. Run it again; already-downloaded tracks are skipped, so that's cheap
- `--limit N` stops the scan as soon as `N` tracks have been collected, so
  `--limit 1` doesn't walk the whole library

`--favorites-only` uses `getStarred2` instead and downloads the starred tracks
only, which is instant regardless of library size.

## What you get

```
music/
├── Miles Davis - Kind of Blue/
│   ├── cover.jpg
│   ├── 01 - So What.opus
│   └── 02 - Blue in Green.opus
├── Radiohead - OK Computer/
│   ├── cover.jpg
│   └── 01 - Airbag.opus
└── manifest.jsonl
```

- One folder per album, named `Artist - Album`
- `NN - Title.opus`, zero-padded track number
- Multi-disc albums get `02-05 - Title.opus` (disc-track) so nothing collides
- Non-ASCII titles are kept as-is — CJK, accents, emoji all survive

### Cover art

Per album, the script fetches `cover.jpg` at 1000px and writes it into the album
folder *and* embeds it into every `.opus` file on that album.

Opus files are read-only for ordinary tags, so embedding means rewriting the
Ogg comment header to add a `METADATA_BLOCK_PICTURE` entry. That is done in pure
Python here (no `ffmpeg` needed on your machine), and the result is re-parsed and
checked before it replaces the downloaded file. Audio data itself is never
touched — it is copied through byte for byte.

Use `--no-embed` to keep `cover.jpg` but leave the `.opus` files alone, or
`--no-cover` to skip artwork entirely.

> **One known limitation.** If a track's comment header does not end on an Ogg page
> boundary — a layout some encoders produce — the script logs a warning, skips
> embedding for that file, and leaves the `cover.jpg` sidecar. It never guesses,
> because a wrong guess would corrupt your audio.

## Options

| Flag | Default | Meaning |
|---|---|---|
| `--url` | `$ND_URL` | Navidrome base URL. `https://` is assumed if omitted |
| `--user` | `$ND_USERNAME` | Username (alias: `--username`) |
| `--password` | `$ND_PASSWORD` | Password. Prefer the env var |
| `--out DIR` | `./music` | Where to write files |
| `--favorites-only` | off | Download only starred tracks instead of the whole library |
| `--format` | `opus` | Target format to request (see Limitations) |
| `--bitrate` | `192` | Target bitrate in kbps, 6–256 |
| `--workers N` | `1` | Parallel downloads. Each spawns an ffmpeg transcode **on the server** |
| `--retries N` | `5` | Attempts per track, not per request |
| `--timeout` | `60` | Socket timeout in seconds |
| `--cover-size` | `1000` | Cover art edge size in pixels |
| `--no-cover` | off | Don't fetch or save artwork |
| `--no-embed` | off | Save `cover.jpg` but don't embed it |
| `--force` | off | Re-download files that already exist |
| `--limit N` | `0` | Only process the first N tracks found (`0` = all) |
| `--dry-run` | off | Show what would happen; write nothing |
| `--insecure` | off | Skip TLS certificate verification (self-signed certs) |
| `--manifest-name` | `manifest.jsonl` | Manifest filename inside `--out` |
| `--log-file` | — | Also write logs to this file |
| `--log-level` | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL` |

### About `--workers`

The default of `1` is deliberate. Every parallel download is a separate ffmpeg
process chewing CPU on your server, and Navidrome caps concurrent transcodes —
exceed the cap and you get HTTP 429. Raise it to 2–3 if your server is idle and
has plenty of cores. Going much above that just trades errors for speed.

## Re-running is safe

Run it as often as you like:

- **Already-downloaded tracks are skipped**, using both the file on disk and
  `manifest.jsonl` to work out whether a file belongs to the track you are
  looking at
- Downloads stream to a `.part` file and are moved into place atomically, so an
  interrupted run can never leave a truncated file that looks complete
- `.part` files are cleaned up after both success and failure

Two *different* tracks that normalise to the same filename get ` (2)`, ` (3)`
and so on rather than overwriting each other.

## When things go wrong

Every track is handled independently: one failure never stops the rest. At the
end you get a summary, and the exit code tells you whether it was clean.

| Situation | What happens |
|---|---|
| HTTP 429, or "too many concurrent transcodes" | Waits for `Retry-After`, then retries |
| HTTP 408/425/500/502/503/504 | Retries with exponential backoff + jitter |
| Connection reset, timeout, mid-transfer drop | Retried; the file restarts from scratch |
| Truncated or malformed audio | Detected from the Ogg container and retried |
| HTTP 401, or Subsonic "Wrong username or password" | Not retried — your credentials are wrong |
| HTTP 404 on a track | Not retried — the track is gone |
| Server returns FLAC/MP3 instead of Opus | Not retried — see Troubleshooting |
| Empty response | Retried |
| An album can't be listed | Logged and skipped; the run continues and exits `1` |

`--retries` bounds all of this per track. Backoff is randomised so parallel
workers don't resynchronise into a thundering herd.

### Sample run

Real output, with the server line from a live run against Navidrome 0.64.2 and the
rest captured from the test suite. The scan logs every 25 albums, so a three-album
library stays quiet. Track 2 hits the server's concurrent-transcode limit and
recovers; track 3 hits the misconfiguration described above.

```
12:15:09 INFO    Connected to https://navi.550441.xyz (Navidrome 0.64.2, API navidrome)
12:15:09 INFO    Found 3 track(s) in the library
12:15:09 INFO    [1/3] Artist - 白夜
12:15:09 INFO    Saved cover.jpg (2.0 KiB)
12:15:09 INFO    Downloaded Artist - 白夜 (5.6 MiB)
12:15:09 INFO    Embedded cover art in 01 - 白夜.opus (1000x1000 image/jpeg, 57.2 KiB)
12:15:09 INFO    [2/3] Artist - Reol
12:15:09 WARNING Attempt 1/2 failed for Artist - Reol: stream failed: HTTP 429
                 ({"subsonic-response": {"status": "failed", "error": {"code": 0,
                 "message": "too many concurrent transcodes"}}}) (retrying in 4.2s)
12:15:09 INFO    Downloaded Artist - Reol (4.1 MiB)
12:15:09 INFO    Embedded cover art in 02 - Reol.opus (1000x1000 image/jpeg, 41.0 KiB)
12:15:09 INFO    [3/3] Artist - Airbag
12:15:09 ERROR   Server-side transcoding looks misconfigured; this will affect every track.
12:15:09 INFO    Done: 2 downloaded, 0 skipped, 1 failed (of 3)
12:15:09 WARNING 1 track(s) failed:
12:15:09 WARNING   Artist - Airbag -> server returned FLAC instead of the requested
                 opus transcode (Content-Type: audio/flac). Navidrome streams the
                 original file whenever no matching transcoding is configured, so
                 either ffmpeg is missing from the Navidrome host, or its
                 transcoding configuration has no target format 'opus'. Check the
                 Navidrome logs for 'Error starting transcoder'.
```

### Interrupt it any time

`Ctrl-C` abandons the track in flight, deletes its partial file, stops before
starting the next one, and prints the summary. Press it again to abort
immediately without waiting for the summary.

```
^C
12:14:08 WARNING Received SIGINT, finishing up (press again to abort)
12:14:09 WARNING Interrupted; stopping after 1 of 4 tracks
12:14:09 INFO    Done: 0 downloaded, 0 skipped, 1 failed (of 4)
```

During the library scan, which happens before any download, `Ctrl-C` instead
prints `Aborted while listing tracks` and exits `130`.

## Exit codes

| Code | Meaning |
|---|---|
| `0` | Everything succeeded (or nothing to do) |
| `1` | At least one track failed — including the track interrupted by `Ctrl-C`, or an album that couldn't be listed |
| `2` | Couldn't start: bad arguments, wrong credentials, server unreachable, album list unavailable |
| `130` | Force-aborted with a second `Ctrl-C`, or during the library scan |

Useful in scripts: `navidrome_favsync.py ... || echo "some tracks failed, see the log"`.

## The manifest

`manifest.jsonl` is append-only, one JSON object per line per track attempt —
ideal for `jq`, and for re-running later. A real captured line:

```json
{"album":"Fixture Album","artist":"Fixture Artist","attempts":1,"audio_format":"opus",
 "bytes":5523398,"content_type":"audio/ogg","cover_embedded":true,"cover_file":true,
 "disc":1,"error":null,"id":"0rVIJb7CSgetU1nTv0ARaF",
 "path":"/srv/music/Reol - 白夜/01 - 白夜.opus","status":"downloaded",
 "timestamp":"2026-09-26T04:17:09+0800","title":"白夜","track":1}
```

`status` is one of `downloaded`, `skipped`, or `failed`. On failure, `error`
holds the reason and `attempts` says how many tries it took. Because it is
append-only, the same track appears once per run, so the history of a flaky
download is all there.

```bash
# what failed, and why
jq -r 'select(.status=="failed") | "\(.artist) - \(.title): \(.error)"' music/manifest.jsonl
```

## Troubleshooting

**`server returned FLAC instead of the requested opus transcode`**

By far the most common issue, and the one this script is most careful about. Your
server has no way to produce Opus, so it sent the original file.

1. Is `ffmpeg` installed *on the Navidrome host*? (Inside the container if it's
   containerised — a host install isn't enough.)
2. Does Navidrome's transcoding config have an Opus target format? Reinstalling
   a container can drop custom transcoding rows; check
   `Settings -> Transcoding`, or the `transcoding` table.
3. Grep the Navidrome logs for `Error starting transcoder` — it says exactly which
   binary it couldn't run.

The script logs `Server-side transcoding looks misconfigured; this will affect
every track` so you know it's systemic, not one bad track.

**`Authentication failed` / `Invalid username or password`**

Check the username and password. Note that Navidrome rate-limits auth attempts
per IP *and* username, so repeatedly guessing just gets you throttled — wait a
few seconds. Also confirm you're pointed at the right URL; a reverse proxy with
a path prefix needs that prefix included, e.g. `https://host/music`.

**`No tracks found in the library`**

The API returned an empty library for this account. Check that the username is
the one you actually use in Navidrome, that it has access to at least one music
folder, and that the server has finished scanning it.

**`No favorites found`**

Only possible with `--favorites-only`: the account genuinely has nothing starred.
Star something in Navidrome and try again.

**A full run spends a long time before the first download**

That's the library scan — one `getAlbum` request per album, every album in the
library, including the ones you've already downloaded. It's only slow the first
time: `Enumerated 500 album(s), 7421 track(s) so far` tells you where it is. Use
`--limit N` to take a short sample, or `--favorites-only` if you only want the
starred tracks.

**Tracks are skipped with no title, or `Unknown Title` everywhere**

Navidrome's `Subsonic.MinimalClients` config strips everything but id and title
for listed client names. If this script's client name ended up in that list,
check `Settings -> Subsonic`.

**`429` errors that never clear**

Your server is at its concurrent-transcode cap. Leave `--workers` at `1`; other
clients or an active jukebox session may be using the budget.

**Slow or timing-out downloads**

Raise `--timeout`. A slow server transcoding a long track in real time needs
longer than 60 seconds per read.

**TLS errors on a self-signed certificate**

Use `--insecure`. Better: trust the certificate on your machine.

## How it works

Navidrome only exposes transcoded audio through the Subsonic API (`/rest`) —
there is no native streaming endpoint — so the script speaks Subsonic end to end:

| Call | Purpose |
|---|---|
| `ping` | Validate URL and credentials before doing any work |
| `getAlbumList2` | Page through every album the account can see |
| `getAlbum` | The tracks of one album |
| `getStarred2` | Your favorite tracks, with `--favorites-only` |
| `getCoverArt` | One cover per album, reused across its tracks |
| `stream` | The audio, with `format=opus&maxBitRate=192` |

Authentication uses Subsonic's token scheme, `t = md5(password + salt)`, with a
fresh random salt per request. **Your password never appears in a URL**, so it
cannot end up in Navidrome's access logs, a proxy log, or a browser history. Log
lines have credential parameters redacted.

### Verifying a download

A `200 OK` does not mean success — Subsonic reports errors with an HTTP 200 and a
JSON body, so the script inspects the actual bytes before trusting them.

It also does not trust `Content-Length`. Navidrome's length is derived from the
*nominal* bitrate, so with VBR Opus it routinely overstates the real size by
around 1% — an exact comparison would reject good downloads. Instead the Ogg
container itself is the integrity check: every page CRC must validate, no packet
may be left unterminated, and the final granule position must be real. Navidrome
makes a *complete* file structurally distinguishable from a cut-off one.

## Limitations

- **`--format` is effectively Opus-only.** It's plumbed through to the server, but
  the format check accepts only Opus responses, so other values will fail the
  track. Keep the default.
- **Metadata is preserved but not extended.** Tags come from the server's
  transcode; the script only adds cover art.
- **Nothing is deleted.** Removed albums, and tracks un-starred between runs, stay
  on disk. This only ever adds files.
- **Multi-library accounts get everything they're allowed to see.** There's no
  `--music-folder` to pick one library; the scan covers all of them.
- **Not a two-way sync.** Nothing is ever un-favorited or deleted on the server.

## Tests

```bash
python3 -m unittest test_navidrome_favsync -v
```

106 tests, no network access required, about 10 seconds. They cover:

- The Ogg CRC against an independent bitwise reference **and** against real `.oga`
  files from the system sound theme
- Cover embedding across JPEG, PNG and all three WebP flavours, including
  artwork large enough to force a multi-page comment header
- Proof that audio bytes survive embedding unchanged
- Library enumeration: every album downloaded by default, multi-page
  `getAlbumList2` offsets, duplicate tracks collapsed, albums with no playable
  tracks skipped, `--limit` stopping the scan early, an unreadable album skipped
  while the run reports exit code `1`, and `--favorites-only` never touching the
  album endpoints
- A mock Navidrome server that misbehaves on cue: 429s, lying `Content-Length`,
  mid-transfer disconnects, HTTP 200 carrying an error document, empty bodies,
  and streaming the original file instead of a transcode
- Credential redaction, filename sanitisation (including Windows reserved names
  and Unicode), manifest integrity, and resume behaviour

