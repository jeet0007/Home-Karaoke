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
  vocal_transcribe.py  Demucs -> Basic Pitch vocal transcription — the ONLY melody source, opt-in via requirements-ml.txt; the same separation also yields the instrumental backing track
  midi.py           dependency-free Standard MIDI File writer
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
| vocal stem | `vocals.wav` | re-transcribing without re-running Demucs; singer-assist track |
| instrumental | `instrumental.wav` | the player's preferred backing track (single-source playback) |
| lyrics | `lyrics.json` | — |
| melody | `melody.json` | player note guide + grading |
| MIDI | `melody.mid` | portable/downloadable transcription |

Artifact paths are tracked in the library DB (`artifacts` table). The WAVs (`mix.wav`/`vocals.wav`/`instrumental.wav`) are large — `KARAOKE_DATA_DIR` should point at a roomy volume on constrained hosts (this app targets low-power NAS deployment).

### Song library & processing queue

Songs picked in the player are queued into a SQLite library (`library.db`, relocatable via `LIBRARY_DB`). A single `LibraryWorker` thread drains the queue one song at a time (deliberately single-threaded — each song fans out yt-dlp/ffmpeg/Demucs subprocesses, and the target host is a small NAS) and runs the core pipeline. The queue *is* the songs table — a `status` column walks `pending → processing → ready/failed`, so it survives restarts. Claiming orders by `priority DESC, id` — a user picking a song at the player (`PRIORITY_USER`, the enqueue default) jumps ahead of `library/seed-charts` backfill work (`PRIORITY_BACKFILL`); re-picking a song already queued at backfill priority promotes it in place.

While a song is in flight, a side thread in `LibraryWorker._process_one` calls `SongLibrary.beat()` every `HEARTBEAT_SECONDS` (30s), stamping `songs.heartbeat_at`. `claim_next_pending()` judges staleness from `COALESCE(heartbeat_at, updated_at)` against `STALE_PROCESSING_SECONDS` (150s) — a legitimately slow stage (Demucs can run minutes) keeps beating and is never touched; a worker killed by a crash or dev-reload stops beating and is reclaimed in ~2.5 minutes instead of the old 15-minute claim-age horizon. `songs.current_stage` (set via `SongLibrary.set_current_stage()`) names the in-flight stage for the UI; both routes below are guarded by `AND status = 'processing'` so a straggler call after the song settles can't resurrect stale bookkeeping.

### Pipeline lineage & structured logging

Each processing run gets a `run_id` (generated in `LibraryWorker._process_one`); every stage's outcome is folded into the overwritten-each-run `songs.report_json` (for the UI) and appended as its own row to `stage_runs` (song_id, run_id, stage, status, started_at/finished_at, duration_ms, input/output sha256 hashes via `ArtifactStore.content_hash()`, error/detail) — an append-only table, so history survives reprocessing instead of being clobbered. `SongLibrary.list_stage_runs(song_id)` / `list_stage_runs_for_run(run_id)` read it back; `SongLibrary.stage_stats()` (served at `GET /library/stats`) aggregates it into per-stage count/avg/max duration plus a songs-by-status census — the "where does the time go / is it getting slower" view without opening sqlite by hand.

Persistence is incremental, not batched at end-of-run: `RunContext` (`core/pipeline.py`) takes an optional `observer(event, payload)` callback, threaded in from `build_processor(...).process(song, run_id, observer)`. Each `_stage()` call fires `observer("stage_begin", stage_name)` before running and `observer("stage_end", stage_run_entry)` after — the worker's observer persists `stage_end` rows via `SongLibrary.record_stage_run()` immediately and updates `current_stage` on `stage_begin`, so a crash mid-run loses at most the one in-flight stage's row, not the whole run's lineage (this replaced an end-of-run batch `record_stage_runs()` call, which is kept only for callers that don't wire an observer). Observer errors are swallowed inside `RunContext.notify()` — telemetry must never fail a song. `pipeline.py` still stays DB-agnostic (no `SongLibrary` import); the observer is the worker's hook in, not a reverse dependency.

The melody stage's slow inner steps — resolving/decoding the original recording, the multi-minute Demucs separation, and Basic Pitch transcription — are their own `decode`/`separate`/`transcribe` stage_runs rows (`in_report=False`: they carry lineage/timing but don't clutter the UI-facing `report`, where `melody` still summarizes). Before this split a 2-minute Demucs run was one opaque `melody` blob in the lineage; now `GET /library/stats` can show exactly which sub-step dominates.

Deliberately no full orchestrator (Dagster/Prefect evaluated and rejected — both want a persistent daemon + metadata DB, which fights the one-thread/one-.db-file/no-broker NAS-footprint stance above) and no declarative stage-descriptor refactor (flagged as a clean follow-up, not done — today's `_stage_*` functions are still ad hoc/non-uniform).

Structured JSON logs (`song_id`/`run_id`/`stage`/`status`/`duration_ms`) go to `KARAOKE_LOG_DIR/pipeline.log` (default `./logs/`, rotating). `core/logging_config.configure()` is lazy/idempotent and only called from `app.py`'s `start_library_worker()` — importing `core.pipeline` never creates the log directory as a side effect (matters for the test suite).

### Melody extraction — isolated-vocal only, no fallback

`vocal_transcribe.py` — Demucs vocal separation then Basic Pitch transcription on the isolated vocal — is the ONLY melody source. Requires `requirements-ml.txt` (pulls in torch); opt-in and best-effort. There is deliberately no full-mix pitch-tracking fallback: transcribing the dominant pitch of a full mix tracks the bass/accompaniment as often as the vocal, and a wrong note guide is worse than none. When the ML deps aren't installed, `ytmusic_video_id` is missing, or transcription fails, `_stage_melody` (`core/pipeline.py`) returns `None` — the song stays playable with lyrics and live scoring, just no note guide. Every outcome is recorded per-song in the pipeline's `report` dict (ok/reused/skipped/failed + detail).

Notes are gated to synced lyric timings so intro/solo/outro instrumental sections can't leak into the note guide.

On Apple Silicon, Basic Pitch only supports Python 3.10, and PyPI's `demucs` release predates the `demucs.api` module this app needs — see the comments in `requirements-ml.txt` for the working install (a 3.10 venv, Demucs from GitHub main, `numpy<2`).

`vocal_transcribe.DEMUCS_DEVICE` defaults to `"auto"`: `_pick_device()` picks CUDA, then MPS, then CPU (several times faster than CPU on a GPU/Apple Silicon host); a GPU/MPS separation that raises falls back to CPU rather than failing the song. Set `DEMUCS_DEVICE` to pin one explicitly (e.g. `cpu` on a NAS with no accelerator). On CPU, `_cap_cpu_threads()` calls `torch.set_num_threads(cores - 1)` (override via `KARAOKE_TORCH_THREADS`) so Demucs doesn't starve the rest of the host while it runs — this app's target host is a small NAS that's doing other jobs too.

The vocal/instrumental stems are WAV, not a compressed format, despite the disk cost — deliberately (see the comment on `artifacts.FILENAMES`). The player's singer-assist resync loop sets `<audio>.currentTime` directly up to 60x/second to keep two independent `<audio>` elements aligned; WAV's time→byte mapping is exact and instant, while a compressed format needs the browser to decode toward a seek point, which is slower and less precise. FLAC was tried here and reverted after a real regression (2026-07-07): repeated re-seeks on the compressed stems reintroduced audible drift between the backing track and the singer-assist vocal. Don't reintroduce a compressed format for these two artifacts without changing the sync mechanism itself (e.g. Web Audio API buffer-based mixing instead of two `<audio>` elements).

### Single-source playback & lyric sync

For a processed song the player's backing track is the *original recording's own instrumental* — `instrumental.wav` (mix minus the Demucs vocal stem, written by the SAME separation that produces `vocals.wav`, so both stems are sample-aligned by construction), served via `GET /library/song/<id>/instrumental` and flagged as `has_instrumental` on `/select-song`. In this mode backing audio, lyrics, note guide, and singer-assist all share the original recording's timeline — nothing can drift, and the player hides its Sync control (`_stage_instrumental` in `core/pipeline.py` also backfills the stem for songs processed before it existed, re-separating from the kept mix).

Fallback (song not yet processed, or no ML add-on): playback streams the auto-picked karaoke video's audio — a *different* upload with a possibly different intro length, so lyrics/melody timed to the original recording can drift against it. In that mode the player keeps its Sync control (`[`/`]` keys) that nudges the lyric/melody timeline against the audio, remembered per backing track in `localStorage`.

### Lyrics sourcing

Lyrica sidecar (races LRCLIB, YouTube, NetEase, Megalobiz, SimpMusic internally) is primary; direct LRCLIB API is the fallback when Lyrica is down, absent, or has no *synced* version — so synced lyrics keep working with no sidecar installed at all. Lyrica also supplies metadata (cover art, duration, genre via its `tags`).

Melody extraction additionally requires `ffmpeg` on `PATH` (not vendored) — without it the note guide is silently skipped, playback/lyrics unaffected.

`/select-song`'s live path (song not yet in the library) also caches its response per `(artist, title)` for `_LIVE_SELECT_TTL_S` (5 minutes, `app.py`'s `_live_select_cache`) — while a first-time pick is still being processed by the background worker, a repeat load of the same player page answers from that cache instead of re-running the whole metadata+lyrics+yt-dlp fan-out. The library fast path (`song_library.find_ready`) is checked first, so the moment the song turns `ready` this cache is naturally bypassed. Tests that hit `/select-song` with a reused identity across cases must clear `app_module._live_select_cache` in setUp (see `tests/test_select_song.py`, `tests/test_library_routes.py`) — it's a plain module-level dict, not scoped to the swapped-in test `song_library`.

### Singer assist

The player can toggle the isolated vocal stem (`artifacts.KIND_VOCALS`, the same file the melody stage already produces) in as an adjustable-volume guide track, via `GET /library/song/<id>/vocals` and `static/player/singer-assist.js`. Best-effort like the note guide: the toggle button stays hidden for songs with no processed stem (no ML add-on, or not processed yet). No on-screen master-volume control — this targets a TV (remote) or device with its own hardware/OS volume, so that would be redundant; the singer-assist slider is a distinct *mix* control (how much original vocal to blend in), not overall loudness.

## Endpoints (see README.md for full request/response shapes)

```
GET  /library, POST /library/add, GET /library/song/<id>, GET /library/song/<id>/midi, GET /library/song/<id>/vocals, GET /library/song/<id>/instrumental, GET /library/stats, POST /library/seed-charts
GET  /lyrics?artist=&title=&duration=
GET  /metadata?artist=&title=
/grade    WebSocket — final fallback tier only; primary live grading runs client-side via wasm/grading/ inside an AudioWorklet
```
