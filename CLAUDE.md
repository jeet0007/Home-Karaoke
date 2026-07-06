# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
pip install -r requirements.txt          # core deps
python scripts/bootstrap_ml.py           # optional: Demucs + Basic Pitch for accurate melody extraction
# ^ detects OS/CPU/Python version and installs requirements-ml.txt plus the
# platform-specific extra basic-pitch needs (e.g. a scikit-learn pin on Apple
# Silicon, tflite-runtime elsewhere) - avoids chasing backend-mismatch
# warnings by hand. `pip install -r requirements-ml.txt` still works, it just
# leaves those extras for you to sort out.
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
  library.py        SQLite song library + background processing queue (SongLibrary, LibraryWorker) + append-only stage_runs lineage
  artifacts.py      on-disk store for reusable per-song files (KARAOKE_DATA_DIR, default ./data/<song_id>/) + sha256 content_hash()
  logging_config.py structured JSON logging (lazy/idempotent; KARAOKE_LOG_DIR, default ./logs/pipeline.log)
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
templates/ static/  the vanilla-JS player + search GUI (static/player/grading.js runs live grading client-side via the WASM module inside static/grading-worklet.js, an AudioWorklet, falling back to the /grade WebSocket only if WebAssembly is unavailable; static/player/singer-assist.js toggles the isolated vocal stem in as a guide track)
scripts/bootstrap_ml.py  detects OS/CPU/Python version and installs requirements-ml.txt + the right platform-specific extra
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

### Pipeline lineage & structured logging

Each processing run gets a `run_id` (generated in `LibraryWorker._process_one`); every stage's outcome is both folded into the overwritten-each-run `songs.report_json` (for the UI) *and* appended as its own row to `stage_runs` (song_id, run_id, stage, status, started_at/finished_at, duration_ms, input/output sha256 hashes via `ArtifactStore.content_hash()`, error/detail) — an append-only table, so history survives reprocessing instead of being clobbered. `SongLibrary.list_stage_runs(song_id)` / `list_stage_runs_for_run(run_id)` read it back. `pipeline.py` stays DB-agnostic (no `SongLibrary` import) — it returns `stage_runs` on its result dict (or on a raised `ProcessingError`), and the worker is the sole persistence point. Deliberately no full orchestrator (Dagster/Prefect evaluated and rejected — both want a persistent daemon + metadata DB, which fights the one-thread/one-.db-file/no-broker NAS-footprint stance above) and no declarative stage-descriptor refactor (flagged as a clean follow-up, not done — today's `_stage_*` functions are still ad hoc/non-uniform).

Structured JSON logs (`song_id`/`run_id`/`stage`/`status`/`duration_ms`) go to `KARAOKE_LOG_DIR/pipeline.log` (default `./logs/`, rotating). `core/logging_config.configure()` is lazy/idempotent and only called from `app.py`'s `start_library_worker()` — importing `core.pipeline` never creates the log directory as a side effect (matters for the test suite).

### Melody extraction — isolated-vocal only, no fallback

`vocal_transcribe.py` — Demucs vocal separation then Basic Pitch transcription on the isolated vocal — is the ONLY melody source. Requires `requirements-ml.txt` (pulls in torch); opt-in and best-effort. There is deliberately no full-mix pitch-tracking fallback: transcribing the dominant pitch of a full mix tracks the bass/accompaniment as often as the vocal, and a wrong note guide is worse than none. When the ML deps aren't installed, `ytmusic_video_id` is missing, or transcription fails, `_stage_melody` (`core/pipeline.py`) returns `None` — the song stays playable with lyrics and live scoring, just no note guide. Every outcome is recorded per-song in the pipeline's `report` dict (ok/reused/skipped/failed + detail).

Notes are gated to synced lyric timings so intro/solo/outro instrumental sections can't leak into the note guide.

On Apple Silicon, Basic Pitch only supports Python 3.10, and PyPI's `demucs` release predates the `demucs.api` module this app needs — see the comments in `requirements-ml.txt` for the working install (a 3.10 venv, Demucs from GitHub main, `numpy<2`).

### Lyric sync offset

Lyrics/melody are timed to the *original* recording, but playback uses a *different* karaoke backing track with a possibly different intro length, so they can drift. The player has a Sync control (`[`/`]` keys) that nudges the lyric/melody timeline against the audio, remembered per backing track in `localStorage`.

### Lyrics sourcing

Lyrica sidecar (races LRCLIB, YouTube, NetEase, Megalobiz, SimpMusic internally) is primary; direct LRCLIB API is the fallback when Lyrica is down, absent, or has no *synced* version — so synced lyrics keep working with no sidecar installed at all. Lyrica also supplies metadata (cover art, duration, genre via its `tags`).

Melody extraction additionally requires `ffmpeg` on `PATH` (not vendored) — without it the note guide is silently skipped, playback/lyrics unaffected.

### Singer assist

The player can toggle the isolated vocal stem (`artifacts.KIND_VOCALS`, the same file the melody stage already produces) in as an adjustable-volume guide track, via `GET /library/song/<id>/vocals` and `static/player/singer-assist.js`. Best-effort like the note guide: the toggle button stays hidden for songs with no processed stem (no ML add-on, or not processed yet). No on-screen master-volume control — this targets a TV (remote) or device with its own hardware/OS volume, so that would be redundant; the singer-assist slider is a distinct *mix* control (how much original vocal to blend in), not overall loudness.

## Endpoints (see README.md for full request/response shapes)

```
GET  /library, POST /library/add, GET /library/song/<id>, GET /library/song/<id>/midi, GET /library/song/<id>/vocals, POST /library/seed-charts
GET  /lyrics?artist=&title=&duration=
GET  /metadata?artist=&title=
/grade    WebSocket — final fallback tier only; primary live grading runs client-side via wasm/grading/ inside an AudioWorklet
```
