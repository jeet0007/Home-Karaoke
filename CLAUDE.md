# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
pip install -r requirements.txt          # core deps
pip install -r requirements-ml.txt       # optional: Demucs + Basic Pitch for accurate melody extraction
# ^ on Apple Silicon: Basic Pitch only supports Python 3.10, so create that
# venv first, e.g. `pyenv install 3.10.20 && ~/.pyenv/versions/3.10.20/bin/python3.10 -m venv .venv-ml`

python app.py                            # run the app (127.0.0.1:5000 by default; APP_HOST/APP_PORT to override)
./start.sh                                # also launches the Lyrica lyrics sidecar first (sidecar/lyrica, port 5001)

python -m unittest discover -s tests -p "test_*.py"   # run the full suite
python -m unittest tests.test_pipeline                 # run one test module
python -m unittest tests.test_pipeline.PipelineTestCase.test_some_case  # run one test
```

There is no linter/formatter config in this repo — don't invent one.

`sidecar/lyrica` is a separately-cloned repo (`git clone https://github.com/Wilooper/Lyrica sidecar/lyrica`), gitignored here, and not part of this codebase's source — treat it as a vendored external dependency, not something to edit.

## Architecture

The code is split along one line: a **core "music → MIDI" pipeline** (`core/`) that produces reusable artifacts on disk, and a **presentation layer** (Flask routes + the player page) that only consumes them. `core/pipeline.py` has no idea a web player exists.

```
app.py            Flask entry point: HTTP/WebSocket routes + wiring
core/             the "music -> MIDI" engine + persistence (no web deps)
  pipeline.py       per-song stage orchestration (resumable; each stage skips if its artifact exists)
  library.py        SQLite song library + background processing queue (SongLibrary, LibraryWorker)
  artifacts.py      on-disk store for reusable per-song files (KARAOKE_DATA_DIR, default ./data/<song_id>/)
  vocal_transcribe.py  Demucs -> Basic Pitch vocal transcription — the ONLY melody source, opt-in via requirements-ml.txt
  midi.py           dependency-free Standard MIDI File writer
  audio.py          shared ffmpeg audio-decode helper
  audio_grading.py  live pitch/melody scoring - reference implementation + final fallback tier for the /grade WebSocket; see wasm/grading/ for the primary, client-side path
search/           finding songs + backing videos
  song_search.py    ytmusicapi song-identity search + charts
  karaoke_search.py yt-dlp karaoke-video ranking (karaoke/instrumental boosted, covers/reactions penalised)
  song_selection.py duration-aware best-candidate pick
  fallback_search.py video-title -> artist/title parsing
lyrics/           multi-source lyrics
  lyrica_client.py  Lyrica sidecar client (primary)
  lrclib_client.py  direct LRCLIB API client (fallback, used if Lyrica is down/absent/has no synced lyrics)
  lyrics_sources.py Lyrica-first / LRCLIB-fallback ordering
  lyrics_filter.py  pre-selection lyrics-availability filtering
wasm/grading/     Rust port of core/audio_grading.py's YIN pitch detector + RealtimeGrader, compiled to WASM (dev-time-only Rust dep; compiled output is checked into static/player/wasm/, see wasm/grading/README.md)
templates/ static/  the vanilla-JS player + search GUI (static/player/grading.js runs live grading client-side via the WASM module inside static/grading-worklet.js, an AudioWorklet, falling back to the /grade WebSocket only if WebAssembly is unavailable)
tests/            unittest suite (mirrors the modules above)
```

### Artifacts (reusable intermediates)

Each song's expensive intermediates persist under `KARAOKE_DATA_DIR/<song_id>/` instead of a tempdir, so pipeline stages are independently re-runnable — delete just `melody.mid` and the next run regenerates it from the kept vocal stem; delete `vocals.wav` too and it re-separates from the kept mix.

| artifact | file | reused for |
|---|---|---|
| decoded mix | `mix.wav` | re-separating vocals without re-downloading |
| vocal stem | `vocals.wav` | re-transcribing without re-running Demucs |
| lyrics | `lyrics.json` | — |
| melody | `melody.json` | player note guide + grading |
| MIDI | `melody.mid` | portable/downloadable transcription |

Artifact paths are tracked in the library DB (`artifacts` table). `mix.wav`/`vocals.wav` are large — `KARAOKE_DATA_DIR` should point at a roomy volume on constrained hosts (this app targets low-power NAS deployment).

### Song library & processing queue

Songs picked in the player are queued into a SQLite library (`library.db`, relocatable via `LIBRARY_DB`). A single `LibraryWorker` thread drains the queue one song at a time (deliberately single-threaded — each song fans out yt-dlp/ffmpeg/Demucs subprocesses, and the target host is a small NAS) and runs the core pipeline. The queue *is* the songs table — a `status` column walks `pending → processing → ready/failed`, so it survives restarts.

### Melody extraction — isolated-vocal only, no fallback

`vocal_transcribe.py` — Demucs vocal separation then Basic Pitch transcription on the isolated vocal — is the ONLY melody source. Requires `requirements-ml.txt` (pulls in torch); opt-in and best-effort. There is deliberately no full-mix pitch-tracking fallback: transcribing the dominant pitch of a full mix tracks the bass/accompaniment as often as the vocal, and a wrong note guide is worse than none. When the ML deps aren't installed, `ytmusic_video_id` is missing, or transcription fails, `_stage_melody` (`core/pipeline.py`) returns `None` — the song stays playable with lyrics and live scoring, just no note guide. Every outcome is recorded per-song in the pipeline's `report` dict (ok/reused/skipped/failed + detail).

Notes are gated to synced lyric timings so intro/solo/outro instrumental sections can't leak into the note guide.

On Apple Silicon, Basic Pitch only supports Python 3.10, and PyPI's `demucs` release predates the `demucs.api` module this app needs — see the comments in `requirements-ml.txt` for the working install (a 3.10 venv, Demucs from GitHub main, `numpy<2`).

### Lyric sync offset

Lyrics/melody are timed to the *original* recording, but playback uses a *different* karaoke backing track with a possibly different intro length, so they can drift. The player has a Sync control (`[`/`]` keys) that nudges the lyric/melody timeline against the audio, remembered per backing track in `localStorage`.

### Lyrics sourcing

Lyrica sidecar (races LRCLIB, YouTube, NetEase, Megalobiz, SimpMusic internally) is primary; direct LRCLIB API is the fallback when Lyrica is down, absent, or has no *synced* version — so synced lyrics keep working with no sidecar installed at all. Lyrica also supplies metadata (cover art, duration, genre via its `tags`).

Melody extraction additionally requires `ffmpeg` on `PATH` (not vendored) — without it the note guide is silently skipped, playback/lyrics unaffected.

## Endpoints (see README.md for full request/response shapes)

```
GET  /library, POST /library/add, GET /library/song/<id>, GET /library/song/<id>/midi, POST /library/seed-charts
GET  /lyrics?artist=&title=&duration=
GET  /metadata?artist=&title=
/grade    WebSocket — live pitch/melody grading against the stored melody (octave-folded)
```
