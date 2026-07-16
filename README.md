# Home Karaoke

A tiny self-hosted karaoke app (built to run comfortably on a low-power NAS): a TV displays a fixed
QR code, phones scan it to join, search any song via YouTube Music, and add it to the room's shared
queue; the TV plays each pick in turn with synced lyrics from multiple sources, an auto-picked
karaoke backing track, a visual note guide, and live scoring against the song's actual melody (mic
captured on the singer's own phone). Candidate videos are automatically ranked by karaoke signals
(karaoke/instrumental/backing-track/no-vocals versions boosted, covers/reactions penalised).

## Setup

```bash
pip install -r requirements.txt
git clone https://github.com/Wilooper/Lyrica sidecar/lyrica   # one-time
./start.sh
```

`./start.sh` is the standard way to run this locally - it installs Lyrica's own dependencies, starts
the sidecar, waits for it to be ready, then starts the main app wired to it (`LYRICA_URL`). Lyrica is
the primary lyrics/metadata source and, per `lyrics/lyrics_filter.py`, is what `/unified-search`'s
pre-selection lyrics check talks to - running it every time (not just occasionally) is what keeps
search fast and results accurate. Without it, the app still runs (`python app.py` alone works and
falls back to the direct LRCLIB API - see [Lyrics](#lyrics) below), but that fallback path is slower
and depends on `lrclib.net` being reachable from wherever you're running this.

Then open http://localhost:3000/tv on the TV's browser - it boots straight into the Sing Room lobby
and shows a QR code. Scan it with a phone on the same network to join, search, and queue songs (see
"TV + phone room pairing" in CLAUDE.md for the `APP_HOST=0.0.0.0` / `APP_LAN_HOST` requirements a
phone needs to actually reach it).

The main app binds to `127.0.0.1:3000` by default. Set `APP_PORT` or `APP_HOST` to change that:

```bash
APP_PORT=5050 ./start.sh
```

(Deliberately not port 5000: macOS's AirPlay Receiver, `ControlCenter`, squats on it by default and
silently swallows requests instead of a clean port-in-use error.) If startup says your chosen port is
already in use, set `APP_PORT=<other port>` and retry.

## Docker deployment (NAS-friendly)

```bash
docker compose up --build                     # web + pipeline
docker compose --profile lyrica up --build     # + the Lyrica lyrics sidecar too
```

Two containers, split specifically so the heavy one can be turned off independently:

- **`web`** — the search UI, player, streaming, lyrics, live grading. Small image, no ML deps. Always
  on; port `3000` (override with `APP_PORT`).
- **`pipeline`** — the background processing queue (Demucs vocal separation, Basic Pitch
  transcription, tempo estimation). This is the resource-heavy half — no exposed port, nothing talks
  to it directly. Turn it off whenever the NAS needs its CPU back:

  ```bash
  docker compose stop pipeline   # web keeps serving ready songs + fresh live picks
  docker compose start pipeline  # queued songs resume processing right where they left off
  ```

  With `pipeline` stopped, newly-picked songs just stay `pending` — the web container keeps working
  exactly as it does today for any not-yet-processed song (lyrics + the picked karaoke video, no note
  guide/instrumental yet). Nothing breaks; the queue just waits.

The two containers **share a SQLite database and a data volume, not a network API** — `core/library.py`
already handles multi-process concurrent access (WAL mode), and `core/artifacts.py` is deliberately
DB-agnostic and network-free, so splitting into two containers needed zero new code beyond a
standalone entrypoint (`worker.py`) for the queue side. A named volume (`karaoke-data`, mounted at
`/data` in both containers) holds `library.db`, every song's artifacts, and logs in one place — handy
for backing up the NAS's karaoke state as a single directory.

**Lyrica** (the optional lyrics sidecar) is a third, profile-gated service — it only builds/starts
with `--profile lyrica`, and only if you've cloned it to `sidecar/lyrica` first (see
[Running with the sidecar](#running-with-the-sidecar) below). Without it, lyrics fall back to the
direct LRCLIB API automatically, same as running outside Docker.

`DEMUCS_DEVICE` defaults to `auto` in the `pipeline` container, which resolves to `cpu` on ordinary
NAS hardware (no CUDA/Apple Silicon MPS available) — see [Note guide & melody scoring](#note-guide--melody-scoring)
for `KARAOKE_TORCH_THREADS` if you want to leave more CPU headroom for other NAS apps.

## Project layout

```
app.py            Flask entry point: HTTP/WebSocket routes + wiring
worker.py         standalone pipeline-queue entrypoint (used by docker/pipeline.Dockerfile)
docker/           web.Dockerfile (lean) + pipeline.Dockerfile (heavy/ML) - see docker-compose.yml
core/             the "music -> MIDI" engine + persistence (no web deps)
  pipeline.py       per-song stage orchestration (resumable)
  library.py        SQLite song library + processing queue + append-only stage_runs lineage
  artifacts.py      on-disk store for reusable per-song files + sha256 content hashing
  logging_config.py structured JSON logging (KARAOKE_LOG_DIR, default ./logs/)
  vocal_transcribe.py  Demucs -> Basic Pitch vocal transcription (the melody source)
  tempo.py          librosa tempo/BPM estimation from the decoded mix
  midi.py           dependency-free Standard MIDI File writer
  audio_grading.py  live pitch/melody scoring - reference implementation + final fallback tier for /grade
search/           finding songs + backing videos
  song_search.py    ytmusicapi song-identity search + charts
  karaoke_search.py yt-dlp karaoke-video ranking
  song_selection.py duration-aware best-candidate pick
  fallback_search.py video-title -> artist/title parsing
lyrics/           multi-source lyrics
  lyrica_client.py  Lyrica sidecar client (primary)
  lrclib_client.py  direct LRCLIB API client (fallback)
  lyrics_sources.py Lyrica-first / LRCLIB-fallback ordering
  lyrics_filter.py  pre-selection lyrics-availability filtering
wasm/grading/     Rust port of the YIN pitch detector, compiled to WASM (dev-time-only Rust dep;
                  compiled output is checked into static/player/wasm/, see wasm/grading/README.md)
templates/ static/  the vanilla-JS TV player + phone GUIs (TV+phone room pairing - see CLAUDE.md) -
                  live grading runs client-side via the WASM module in an AudioWorklet, falling back
                  to the /grade WebSocket only if WebAssembly is unavailable
scripts/bootstrap_ml.py  detects OS/CPU/Python version, installs requirements-ml.txt + the right extra
tests/            unittest suite (mirrors the modules above)
```

## Architecture: core pipeline vs. presentation

The code is split along one line: a **core "music → MIDI" pipeline** (`core/`) that produces reusable
artifacts, and a **presentation layer** (the player page + live scorer) that only consumes them.

- **Core** (`core/pipeline.py`): given a song, it resolves audio and produces synced lyrics, an
  isolated vocal stem, and a reference melody (note segments **and** a real `.mid` file) — persisting
  each to disk via `core/artifacts.py`. It has no idea a web player exists; it could be driven from a
  CLI or cron just as well.
- **Presentation** (`templates/player.html`, `wasm/grading/`, `core/audio_grading.py`): reads the
  stored melody/lyrics to draw the note guide and score singing — primarily client-side via the
  Rust/WASM port running in an AudioWorklet, falling back to `core/audio_grading.py`'s original
  server-side scorer (the `/grade` WebSocket) only if WebAssembly is unavailable. Presentation never
  *produces* an artifact.

### Artifacts (reusable intermediates)

Expensive intermediates are persisted per song under `KARAOKE_DATA_DIR` (default `./data/<song_id>/`)
instead of being thrown away in a tempdir, so each stage is independently re-runnable:

| artifact | file | reused for |
|---|---|---|
| decoded mix | `mix.wav` | re-separating vocals without re-downloading |
| vocal stem | `vocals.wav` | re-transcribing without re-running Demucs; singer-assist track |
| instrumental | `instrumental.wav` | the player's preferred backing track (single-source playback) |
| lyrics | `lyrics.json` | — |
| melody | `melody.json` | the player note guide + grading |
| MIDI | `melody.mid` | portable/downloadable transcription (`GET /library/song/<id>/midi`) |

Each pipeline stage skips when its artifact already exists — delete just `melody.mid` and the next
run regenerates it from the kept vocal stem; delete `vocals.wav` too and it re-separates from the
kept mix (re-decoding it first if that's gone too). Artifact paths are tracked in the library DB
(`artifacts` table). Note: `vocals.wav`/`instrumental.wav` are large — point `KARAOKE_DATA_DIR` at a
roomy NAS volume.

**`mix.wav` doesn't stick around.** Since `instrumental = mix - vocals` (how separation produces it),
`mix.wav` is exactly `vocals.wav + instrumental.wav` — once both stems exist it's pure redundant
storage, about a third of a song's WAV footprint, and it's never read during playback. The pipeline
deletes it as soon as both stems are on disk; the only cost if you ever need to regenerate a stem
from scratch is a ~2s re-decode from the source URL instead of skipping straight to Demucs.

## Song library & processing queue

Every song picked in the player is queued for background processing into a SQLite library
(`library.db` next to `app.py`; relocate it with `LIBRARY_DB=/path/to/library.db`). A single worker
thread drains the queue one song at a time (deliberately single — the target host is a small NAS,
and each song already fans out yt-dlp/ffmpeg/Demucs subprocesses) and runs the core pipeline above.
A "ready" library song then plays instantly from the database with zero live lookups, complete with
the note guide. The queue *is* the songs table (a `status` column walking pending → processing →
ready/failed), so queued work survives restarts and failures stay visible with their reason.

**Karaoke first, pipeline second.** A first-time pick never waits on melody/vocal/instrumental
extraction — those only ever happen in the background queue. You're singing (lyrics + the picked
karaoke video's audio) within the couple of seconds the live lyrics+video lookup takes; the *next*
pick of that song, by anyone, gets the fully processed version (note guide, single-source
instrumental, singer-assist) instantly. The one piece of duplicate work this could cause — the
background job re-resolving the same lyrics/video the live pick just found — is deduped: a live
success is handed straight to the queued row, and the pipeline reuses it instead of re-querying
Lyrica/yt-dlp for the identical identity. A live miss still gets a full independent retry in the
background, so it's never locked into the same failure.

```
GET  /library                → all stored songs with queue status (?status=ready|pending|processing|failed)
POST /library/add            → {"artist", "title", "duration_seconds"?, "ytmusic_video_id"?} — enqueue
GET  /library/song/<id>      → full stored payload (lyrics/melody + artifact paths + processing report)
GET  /library/song/<id>/midi → download the transcribed melody as a Standard MIDI File
GET  /library/song/<id>/vocals → stream the isolated vocal stem for the singer-assist toggle
GET  /library/song/<id>/instrumental → stream the separated backing track (single-source playback)
GET  /library/stats          → per-stage timing (count/avg/max duration_ms) + songs-by-status counts
POST /library/seed-charts    → {"limit": 20, "country": "ZZ"} — enqueue the current YouTube Music top
                               charts so the library builds itself over time (run occasionally/cron)
```

The search page shows the library under the search box: ready songs play instantly, in-flight rows
show worker progress (auto-refreshing, including which pipeline stage is running — "processing ·
separate"), failed rows say why. Re-adding a failed song retries it. Picking a song at the player
jumps it ahead of any `seed-charts` backfill work still queued behind it (priority queue: user picks
always claim before backfill, FIFO within the same priority).

The worker keeps a heartbeat while a song is in flight, so a song orphaned by a crash or restart —
not a slow stage, which keeps beating — is reclaimed in under 3 minutes instead of sitting dead in
the queue. `GET /library/stats` aggregates the stage-timing lineage below into per-stage count/avg/max
duration plus a songs-by-status count — a quick "where's the time going, is it getting slower" view.

### Per-song processing report (what passed / what failed)

Every song records how it was processed, so you can tell *why* a song is the way it is — especially
why a "ready" song might have no note guide. Each pipeline stage records an outcome:

- `ok` — passed (e.g. `42 synced lines from lrclib`, `transcribed 88 notes from the isolated vocal`)
- `reused` — loaded from a previous run's artifact
- `skipped` — deliberately not run, with the reason (`no source-recording id`,
  `vocal-transcription add-on not installed`)
- `failed` — errored, with the message (`vocal transcription failed: <error>`)

`GET /library/song/<id>` returns the full `report` (each stage + a human `detail`); `GET /library`
includes a compact `stages` map (`{"lyrics":"ok","video":"ok","melody":"skipped"}`). The library list
on the search page renders these as per-stage badges (✓ lyrics · – guide · ✓ backing), so at a glance
you see what passed, what was skipped, and what failed. Failed songs still carry their one-line
`error`; the report adds the granularity for the best-effort stages (the note guide) that otherwise
leave no trace.

Every stage run is also appended to a `stage_runs` lineage table (survives reprocessing, unlike the
report above which is overwritten each run) and logged as structured JSON to `KARAOKE_LOG_DIR`
(default `./logs/`) — see CLAUDE.md for the schema. Each row is persisted the moment its stage
finishes (not batched at the end of the run), so a crash mid-song loses at most the one in-flight
stage's row. The melody stage's slow inner steps (decode, Demucs separation, Basic Pitch transcription)
get their own lineage rows too, so a multi-minute run is attributable instead of one opaque blob —
`GET /library/stats` is the fastest way to see this.

**Speeding up Demucs.** `DEMUCS_DEVICE` defaults to `auto`: it picks CUDA, then Apple Silicon MPS,
then CPU, and falls back to CPU if a GPU/MPS separation errors — several times faster than CPU when a
GPU or Apple Silicon is available. Pin one explicitly (e.g. `DEMUCS_DEVICE=cpu`) on a NAS with no
accelerator; `KARAOKE_TORCH_THREADS` caps CPU thread count (default: all cores minus one) so
separation doesn't starve the rest of the host.

## Note guide & melody scoring

The library worker extracts a reference melody from the *original* recording and the player draws it
as a scrolling piano-roll lane with a live dot showing your own pitch. Live scoring (the `/grade`
WebSocket) grades you *against that melody* — octave-folded, so singing in your own octave counts as
on-pitch — blended with pitch stability. Songs without a processed melody keep the original
stability-only scoring.

The melody comes **only** from the isolated vocal: `vocal_transcribe.py` runs
[Demucs](https://github.com/adefossez/demucs) vocal separation, then
[Basic Pitch](https://github.com/spotify/basic-pitch) transcription on the isolated vocal — clean
monophonic vocal MIDI with none of the bass/instrument confusion. It's an **opt-in add-on**:
`python scripts/bootstrap_ml.py` (detects your OS/CPU/Python version and installs
`requirements-ml.txt` plus the platform-specific extra basic-pitch needs — pulls in torch; on Linux
Basic Pitch uses the lightweight TensorFlow Lite runtime). Expect a few minutes of CPU per song — it
runs only in the background queue, so it never blocks playback.

There is deliberately **no full-mix fallback**. Transcribing the dominant pitch of a full mix tracks
the bass/accompaniment as often as the vocal, so a wrong guide is worse than none — without the ML
add-on the song still plays with lyrics and scoring, just no note guide (the player says so). The
notes are gated to the synced lyric timings, so intros/solos/outros can't put accompaniment bars into
the guide.

**Tempo / BPM.** When the add-on is installed, `tempo.py` also estimates the song's BPM with
[librosa](https://librosa.org) (from the decoded full mix — the drums/bass drive the beat). The BPM
is stored, written into the exported `.mid` as its real tempo (instead of a nominal 120), reported
per song, and shown as a chip on the player. Best-effort: if librosa isn't installed or estimation
fails, the song simply carries no BPM.

### Singer assist

The player can toggle the isolated vocal stem — the same `vocals.wav` the melody stage already
produces — in as an adjustable-volume guide track alongside the backing video's audio
(`GET /library/song/<id>/vocals`, `static/player/singer-assist.js`). The toggle button only appears
for songs with a processed stem; its volume slider only shows while the toggle is on. This is a
*mix* control (how much of the original vocal to blend in as a guide), separate from overall
loudness — there's no on-screen master-volume control, since this targets a TV (remote) or device
that already has its own hardware/OS volume.

### Single-source playback & lyric sync

Once the ML add-on has processed a song, the player's backing track is the *original recording's own
instrumental* — the decoded mix minus its Demucs vocal stem (`instrumental.wav`, the same separation
that produces the singer-assist stem, served at `GET /library/song/<id>/instrumental`). Backing
audio, lyrics, note guide, and the singer-assist vocal then all come from **one** recording (indeed
one separation of one file), so they are in sync by construction: nothing to correct, and the Sync
control is hidden.

Songs not yet processed (or processed without the ML add-on) fall back to streaming the auto-picked
karaoke video's audio. That's a *different* upload whose intro can be a different length than the
recording the lyrics were timed to, so they can drift (e.g. the lyrics start singing while the
backing track's intro is still playing). In fallback mode the player keeps its **Sync** control (the
`[` / `]` keys, or the "Lyrics -/+" buttons) to nudge the lyric/melody timeline against the audio;
the offset is remembered per backing track in `localStorage`.

## Lyrics

Lyrics come from multiple sources: the [Lyrica](https://github.com/Wilooper/Lyrica) sidecar is
primary (it races LRCLIB, YouTube, NetEase, Megalobiz and SimpMusic internally), with a direct
[LRCLIB](https://lrclib.net) API fallback when Lyrica is down, absent, or has no *synced* lyrics for
the song — so synced lyrics keep working even with no sidecar installed at all. Lyrica also serves
song metadata (cover art, duration). It is not vendored in this repo; clone it once:

```bash
git clone https://github.com/Wilooper/Lyrica sidecar/lyrica
```

### Running with the sidecar

```bash
./start.sh
```

This installs Lyrica's own dependencies, starts it on port 5001, waits for it to come up, then starts
this app on port 3000. Set `APP_PORT` or `APP_HOST` to change this app's bind address. Set
`LYRICA_PORT` to change the sidecar's port, or `LYRICA_URL` to point at an already-running / remote
Lyrica instance instead (e.g. `LYRICA_URL=https://wilooper-lyrica.hf.space`).

If `sidecar/lyrica` isn't present, `start.sh` skips it and only starts the main app — `/metadata`
will then respond with 404s and `/lyrics` falls back to the direct LRCLIB source; search and
playback keep working normally.

### Endpoints

```
GET /lyrics?artist=<str>&title=<str>&duration=<int>
  → 200 {"synced": [{"time_ms": int, "text": str}, ...], "plain": "...", "source": "lrclib"}
  → 404 {"error": "no lyrics found"}
```

`synced` is `[]` when Lyrica has no timestamped (LRC) version for the song, even if `plain` is
non-empty. `duration` is accepted but currently unused — Lyrica's `/lyrics/` endpoint has no way to
disambiguate by track length.

```
GET /metadata?artist=<str>&title=<str>
  → 200 {"cover_art": url, "genre": str, "duration_s": int, "release_date": str, ...}
  → 404 {"error": "no metadata found"}
```

`genre` is derived from Lyrica's `tags` list (comma-joined); other fields Lyrica returns (album,
popularity, links, etc.) are passed through as-is.

Melody extraction (the note guide) requires `ffmpeg` on the server's `PATH` — it's not vendored,
install it separately (e.g. `brew install ffmpeg` / `apt install ffmpeg`). Without it, the note
guide is simply skipped; playback and lyrics are unaffected.

## Notes

- Search is powered by the bundled `binaries/yt-dlp` binary (already included in this repo) — no separate yt-dlp install is required for search itself, though `yt-dlp` is still listed in `requirements.txt` as a Python fallback/dependency.
