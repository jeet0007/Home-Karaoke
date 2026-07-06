# Home Karaoke

A tiny self-hosted karaoke app (built to run comfortably on a low-power NAS): search any song via
YouTube Music, get synced lyrics from multiple sources, sing along to an auto-picked karaoke backing
track with a visual note guide, and get scored live against the song's actual melody. Candidate
videos are automatically ranked by karaoke signals (karaoke/instrumental/backing-track/no-vocals
versions boosted, covers/reactions penalised), served through a minimal dark-themed vanilla JS GUI.

## Setup

```bash
pip install -r requirements.txt
python app.py
```

Then open http://localhost:5000 in your browser.

The main app binds to `127.0.0.1:5000` by default. Set `APP_PORT` or `APP_HOST` to change that:

```bash
APP_PORT=5050 python app.py
```

On macOS, port 5000 is commonly used by AirPlay Receiver (`ControlCenter`). If startup says port 5000
is already in use, set `APP_PORT=<other port>` and retry, or free port 5000 in System Settings.

## Project layout

```
app.py            Flask entry point: HTTP/WebSocket routes + wiring
core/             the "music -> MIDI" engine + persistence (no web deps)
  pipeline.py       per-song stage orchestration (resumable)
  library.py        SQLite song library + processing queue
  artifacts.py      on-disk store for reusable per-song files
  vocal_transcribe.py  Demucs -> Basic Pitch vocal transcription (the melody source)
  tempo.py          librosa tempo/BPM estimation from the decoded mix
  midi.py           dependency-free Standard MIDI File writer
  audio_grading.py  live pitch/melody scoring (the /grade WebSocket)
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
templates/ static/  the vanilla-JS player + search GUI
tests/            unittest suite (mirrors the modules above)
```

## Architecture: core pipeline vs. presentation

The code is split along one line: a **core "music → MIDI" pipeline** (`core/`) that produces reusable
artifacts, and a **presentation layer** (the player page + live scorer) that only consumes them.

- **Core** (`core/pipeline.py`): given a song, it resolves audio and produces synced lyrics, an
  isolated vocal stem, and a reference melody (note segments **and** a real `.mid` file) — persisting
  each to disk via `core/artifacts.py`. It has no idea a web player exists; it could be driven from a
  CLI or cron just as well.
- **Presentation** (`templates/player.html`, `core/audio_grading.py`): reads the stored melody/lyrics
  to draw the note guide and score singing. It never *produces* an artifact.

### Artifacts (reusable intermediates)

Expensive intermediates are persisted per song under `KARAOKE_DATA_DIR` (default `./data/<song_id>/`)
instead of being thrown away in a tempdir, so each stage is independently re-runnable:

| artifact | file | reused for |
|---|---|---|
| decoded mix | `mix.wav` | re-separating vocals without re-downloading |
| vocal stem | `vocals.wav` | re-transcribing without re-running Demucs |
| lyrics | `lyrics.json` | — |
| melody | `melody.json` | the player note guide + grading |
| MIDI | `melody.mid` | portable/downloadable transcription (`GET /library/song/<id>/midi`) |

Each pipeline stage skips when its artifact already exists — delete just `melody.mid` and the next
run regenerates it from the kept vocal stem; delete `vocals.wav` too and it re-separates from the
kept mix. Artifact paths are tracked in the library DB (`artifacts` table). Note: `mix.wav` +
`vocals.wav` are large — point `KARAOKE_DATA_DIR` at a roomy NAS volume.

## Song library & processing queue

Every song picked in the player is queued for background processing into a SQLite library
(`library.db` next to `app.py`; relocate it with `LIBRARY_DB=/path/to/library.db`). A single worker
thread drains the queue one song at a time (deliberately single — the target host is a small NAS,
and each song already fans out yt-dlp/ffmpeg/Demucs subprocesses) and runs the core pipeline above.
A "ready" library song then plays instantly from the database with zero live lookups, complete with
the note guide. The queue *is* the songs table (a `status` column walking pending → processing →
ready/failed), so queued work survives restarts and failures stay visible with their reason.

```
GET  /library                → all stored songs with queue status (?status=ready|pending|processing|failed)
POST /library/add            → {"artist", "title", "duration_seconds"?, "ytmusic_video_id"?} — enqueue
GET  /library/song/<id>      → full stored payload (lyrics/melody + artifact paths + processing report)
GET  /library/song/<id>/midi → download the transcribed melody as a Standard MIDI File
POST /library/seed-charts    → {"limit": 20, "country": "ZZ"} — enqueue the current YouTube Music top
                               charts so the library builds itself over time (run occasionally/cron)
```

The search page shows the library under the search box: ready songs play instantly, in-flight rows
show worker progress (auto-refreshing), failed rows say why. Re-adding a failed song retries it.

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

### Lyric sync offset

Lyrics and the note guide are timed to the *original* recording, but playback is a *different*
karaoke backing track whose intro can be a different length — so they can drift (e.g. the lyrics
start singing while the backing track's intro is still playing). The player has a **Sync** control
(the `[` / `]` keys, or the "Lyrics -/+" buttons) that nudges the lyric/melody timeline against the
audio; the offset is remembered per backing track in `localStorage`.

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
this app on port 5000. Set `APP_PORT` or `APP_HOST` to change this app's bind address. Set
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
