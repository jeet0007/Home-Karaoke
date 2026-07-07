"""Core music -> MIDI pipeline, separate from presentation.

This module is the ENGINE. Given a song identity it produces the reusable
artifacts a karaoke session needs and persists each to the ArtifactStore:

    lyrics       -> synced lyric lines
    vocals       -> isolated vocal stem (only when the ML deps are installed)
    instrumental -> mix minus vocals: the playable backing track, on the
                    same timeline as everything above by construction
    melody       -> note segments + a real .mid file (the "music -> MIDI" output)

The melody is produced ONLY from the isolated vocal (Demucs -> Basic Pitch).
There is deliberately no full-mix fallback: transcribing the dominant pitch
of a full mix produces a guide that tracks the bass/accompaniment as often
as the vocal, which is worse than no guide. When the ML deps are absent (or
transcription fails), the song is left playable with lyrics but no note
guide, rather than shipping a bad one.

The player page and the live scorer (audio_grading.py) are PRESENTATION:
they consume these artifacts and never produce them. Keeping that boundary
here means the transcription core can be reused, re-run, or driven from a
CLI/cron without dragging the web layer along.

Resumability / separate reprocessing: each stage checks the store and skips
when its artifact already exists. Delete just `melody.mid` and the next run
regenerates it from the kept vocal stem without re-running Demucs; delete
`vocals.wav` too and it re-separates from the kept mix without re-downloading.
That is the point of persisting intermediates instead of using a tempdir.

Network-bound steps (lyrics lookup, karaoke-video ranking, yt-dlp stream
resolution) are injected as callables so this module stays import-light and
unit-testable without network/ffmpeg/torch. The local transcription modules
(vocal_transcribe, tempo, midi) are imported directly - importing them is
cheap; vocal_transcribe/tempo only pull in torch/librosa lazily, inside
their functions, when actually used.
"""

import logging
import time
import uuid

from core import artifacts
from core import midi
from core import tempo
from core import vocal_transcribe
from core.library import ProcessingError

# Module-level getLogger is a no-op until something calls
# logging_config.configure() (app.py's start_library_worker() does, in
# production) - this module deliberately does NOT import core.logging_config
# or attach a handler itself, so importing core.pipeline (every pipeline
# test does) stays a zero-side-effect operation: no logs/ dir, no handler.
_logger = logging.getLogger("karaoke.pipeline")

# Notes falling outside the sung sections of the song (intros, solos,
# outros) are dropped - the synced lyrics tell us when someone is actually
# singing. LEAD_MS: how early before a lyric line a note may start and still
# count (singers lead in). MAX_LINE_MS: how long a line with no successor
# (or a big gap to the next) is assumed sung before it's an instrumental gap.
LYRIC_GATE_LEAD_MS = 700
LYRIC_GATE_MAX_LINE_MS = 10000


def gate_notes_to_lyrics(notes, synced_lyrics):
    """Drop melody notes outside the song's sung sections, using the synced
    lyric line timings as the "someone is singing here" windows. With no
    usable synced lyrics the notes pass through unchanged."""
    starts = sorted(
        {int(line["time_ms"]) for line in (synced_lyrics or []) if isinstance(line, dict) and "time_ms" in line}
    )
    if not starts or not notes:
        return notes

    windows = []
    for i, start in enumerate(starts):
        next_start = starts[i + 1] if i + 1 < len(starts) else start + LYRIC_GATE_MAX_LINE_MS
        windows.append((start - LYRIC_GATE_LEAD_MS, min(next_start, start + LYRIC_GATE_MAX_LINE_MS)))

    def in_sung_section(note):
        return any(note["start_ms"] < end and note["end_ms"] > start for start, end in windows)

    return [note for note in notes if in_sung_section(note)]


def _record(produced, store, song_id, kind):
    produced.append({"kind": kind, "path": store.path(song_id, kind), "bytes": store.size(song_id, kind)})


class RunContext:
    """Mutable per-run state threaded through every pipeline stage - the
    mechanical rename of the old bare `report` dict parameter into one
    object that also carries stage_runs lineage, so every existing call site
    that used to receive `report` stays a single parameter, not a widened
    signature.

    `report` is unchanged (still what songs.report_json stores). `stage_runs`
    is new: one entry per _stage() call, with timing + content hashes, for
    the stage_runs table (persisted by library.py - this module has no idea
    that table exists).

    `observer` is an optional live-progress hook: observer("stage_begin",
    stage_name) as each stage starts, observer("stage_end", stage_run_entry)
    as each finishes. The worker uses it to show the in-flight stage and to
    persist lineage incrementally (crash-proof); pipeline tests and direct
    callers just leave it None. Observer errors are swallowed - telemetry
    must never fail a song."""

    __slots__ = ("run_id", "song_id", "report", "stage_runs", "observer")

    def __init__(self, run_id, song_id, observer=None):
        self.run_id = run_id
        self.song_id = song_id
        self.report = {}
        self.stage_runs = []
        self.observer = observer

    def notify(self, event, payload):
        if self.observer is None:
            return
        try:
            self.observer(event, payload)
        except Exception:
            pass

    def begin(self, stage):
        self.notify("stage_begin", stage)


def _stage(ctx, name, status, detail, *, started_at=None, input_hashes=None, output_path=None, output_hash=None, in_report=True):
    """Record one stage's outcome. status is ok/reused/skipped/failed; detail
    is a short human-readable explanation of what passed / why it didn't.

    Populates ctx.report (unchanged shape/consumer) and ctx.stage_runs
    (timing + content-hash lineage), logs one line per call - INFO, or ERROR
    when status is "failed" - and notifies ctx's observer so the worker can
    persist the row immediately. in_report=False keeps a sub-stage (decode/
    separate/transcribe) out of the UI-facing report - its umbrella stage
    (melody) summarizes there - while still recording full lineage."""
    finished_at = time.time()
    started_at = finished_at if started_at is None else started_at
    duration_ms = int(round((finished_at - started_at) * 1000))

    if in_report:
        ctx.report[name] = {"status": status, "detail": detail}
    ctx.stage_runs.append(
        {
            "stage": name,
            "status": status,
            "started_at": started_at,
            "finished_at": finished_at,
            "duration_ms": duration_ms,
            "input_hashes": input_hashes,
            "output_path": output_path,
            "output_hash": output_hash,
            "error": detail if status == "failed" else None,
            "detail": detail,
        }
    )
    ctx.notify("stage_end", ctx.stage_runs[-1])

    _logger.log(
        logging.ERROR if status == "failed" else logging.INFO,
        "stage %s %s: %s",
        name,
        status,
        detail,
        extra={
            "song_id": ctx.song_id,
            "run_id": ctx.run_id,
            "stage": name,
            "status": status,
            "duration_ms": duration_ms,
        },
    )


def _stage_lyrics(song, store, produced, fetch_lyrics, ctx):
    started_at = time.time()
    ctx.begin("lyrics")
    lyrics = fetch_lyrics(song["artist"], song["title"], song["duration_seconds"])
    if not lyrics or not lyrics.get("synced"):
        _stage(ctx, "lyrics", "failed", "no synced lyrics found in any source", started_at=started_at)
        raise ProcessingError("no synced lyrics found in any source", report=ctx.report, stage_runs=ctx.stage_runs)
    store.write_json(song["id"], artifacts.KIND_LYRICS, lyrics)
    _record(produced, store, song["id"], artifacts.KIND_LYRICS)
    _stage(
        ctx,
        "lyrics",
        "ok",
        f"{len(lyrics['synced'])} synced lines from {lyrics.get('source') or 'unknown source'}",
        started_at=started_at,
        output_path=store.path(song["id"], artifacts.KIND_LYRICS),
        output_hash=store.content_hash(song["id"], artifacts.KIND_LYRICS),
    )
    return lyrics


def _stage_video(song, find_video, ctx):
    started_at = time.time()
    ctx.begin("video")
    video = find_video(song["artist"], song["title"], song["duration_seconds"])
    if not video or not video.get("video_id"):
        _stage(ctx, "video", "failed", "no karaoke backing track found", started_at=started_at)
        raise ProcessingError("no karaoke backing track found", report=ctx.report, stage_runs=ctx.stage_runs)
    _stage(ctx, "video", "ok", f"picked backing video {video['video_id']}", started_at=started_at)
    return video


def _estimate_tempo(store, song_id, ctx):
    """Best-effort BPM of the decoded full mix (librosa; see tempo.py). The
    mix drives the beat, so tempo is estimated from it, not the vocal. Every
    outcome is recorded on ctx; returns the BPM or None."""
    started_at = time.time()
    ctx.begin("tempo")
    if not tempo.available():
        _stage(ctx, "tempo", "skipped", "tempo add-on (librosa) not installed", started_at=started_at)
        return None
    if not store.exists(song_id, artifacts.KIND_MIX):
        _stage(ctx, "tempo", "skipped", "no decoded mix to analyze", started_at=started_at)
        return None
    input_hashes = [store.content_hash(song_id, artifacts.KIND_MIX)]
    try:
        bpm = tempo.estimate_bpm(store.path(song_id, artifacts.KIND_MIX))
    except Exception as exc:
        _stage(
            ctx, "tempo", "failed", f"tempo estimation failed: {exc}", started_at=started_at, input_hashes=input_hashes
        )
        return None
    if bpm is None:
        _stage(ctx, "tempo", "failed", "could not estimate a tempo", started_at=started_at, input_hashes=input_hashes)
        return None
    _stage(ctx, "tempo", "ok", f"{bpm} BPM", started_at=started_at, input_hashes=input_hashes)
    return bpm


def _substage_decode(song, store, produced, resolve_audio_url, ctx):
    """Resolve the original recording's stream URL and decode it to the mix
    WAV. Lineage-only sub-stage (in_report=False): the melody stage
    summarizes for the UI; this row makes the minutes attributable."""
    song_id = song["id"]
    started_at = time.time()
    ctx.begin("decode")
    mix_path = store.path(song_id, artifacts.KIND_MIX)
    if store.exists(song_id, artifacts.KIND_MIX):
        _stage(
            ctx, "decode", "reused", "reused decoded mix from a previous run",
            started_at=started_at, output_path=mix_path,
            output_hash=store.content_hash(song_id, artifacts.KIND_MIX), in_report=False,
        )
        return mix_path
    try:
        vocal_transcribe._decode_to_wav(resolve_audio_url(song["ytmusic_video_id"]), mix_path)
    except Exception as exc:
        _stage(ctx, "decode", "failed", f"stream resolve/decode failed: {exc}", started_at=started_at, in_report=False)
        raise
    _record(produced, store, song_id, artifacts.KIND_MIX)
    _stage(
        ctx, "decode", "ok", "resolved + decoded the original recording",
        started_at=started_at, output_path=mix_path,
        output_hash=store.content_hash(song_id, artifacts.KIND_MIX), in_report=False,
    )
    return mix_path


def _substage_separate(song_id, mix_path, vocal_path, store, produced, ctx):
    """Demucs separation - by far the slowest step, so it gets its own
    stage_runs row and current_stage window instead of hiding inside a
    silent multi-minute melody blob."""
    started_at = time.time()
    ctx.begin("separate")
    input_hashes = [store.content_hash(song_id, artifacts.KIND_MIX)]
    try:
        # One Demucs pass yields both stems: the vocal (transcribed next,
        # and the singer-assist track) and the instrumental (the playable
        # backing track - recorded by _stage_instrumental, which owns its
        # bookkeeping).
        vocal_transcribe.separate_vocals(mix_path, vocal_path, store.path(song_id, artifacts.KIND_INSTRUMENTAL))
    except Exception as exc:
        _stage(
            ctx, "separate", "failed", f"vocal separation failed: {exc}",
            started_at=started_at, input_hashes=input_hashes, in_report=False,
        )
        raise
    _record(produced, store, song_id, artifacts.KIND_VOCALS)
    _stage(
        ctx, "separate", "ok", "Demucs split the mix into vocal + instrumental stems",
        started_at=started_at, input_hashes=input_hashes, output_path=vocal_path,
        output_hash=store.content_hash(song_id, artifacts.KIND_VOCALS), in_report=False,
    )


def _substage_transcribe(song_id, vocal_path, store, ctx):
    started_at = time.time()
    ctx.begin("transcribe")
    input_hashes = [store.content_hash(song_id, artifacts.KIND_VOCALS)]
    try:
        segments = vocal_transcribe.note_events_to_segments(vocal_transcribe.transcribe(vocal_path))
    except Exception as exc:
        _stage(
            ctx, "transcribe", "failed", f"transcription failed: {exc}",
            started_at=started_at, input_hashes=input_hashes, in_report=False,
        )
        raise
    _stage(
        ctx, "transcribe", "ok", f"Basic Pitch produced {len(segments)} note segments",
        started_at=started_at, input_hashes=input_hashes, in_report=False,
    )
    return segments


def _build_vocal_melody(song, store, produced, resolve_audio_url, ctx):
    """Isolated-vocal path: keep the mix and the vocal stem as artifacts so
    either can be reused. Each sub-step (decode/separate/transcribe) is its
    own observable sub-stage, skipped when its file already exists; failures
    propagate to _stage_melody, which reports the umbrella outcome."""
    song_id = song["id"]
    vocal_path = store.path(song_id, artifacts.KIND_VOCALS)

    if not store.exists(song_id, artifacts.KIND_VOCALS):
        mix_path = _substage_decode(song, store, produced, resolve_audio_url, ctx)
        _substage_separate(song_id, mix_path, vocal_path, store, produced, ctx)

    segments = _substage_transcribe(song_id, vocal_path, store, ctx)
    duration_s = max((n["end_ms"] for n in segments), default=0) / 1000.0
    bpm = _estimate_tempo(store, song_id, ctx)
    return {"notes": segments, "duration_s": duration_s, "source": "demucs+basic-pitch", "bpm": bpm}


def _stage_melody(song, lyrics, store, produced, resolve_audio_url, ctx):
    """Produce the melody json + .mid artifacts from the ISOLATED VOCAL only
    (Demucs -> Basic Pitch). Vocal-only and best-effort: if the ML deps are
    not installed, the song has no ytmusic id, or transcription fails, the
    song is left playable with no note guide (returns None) - there is no
    low-quality full-mix fallback (see the module docstring). Every outcome
    is recorded on ctx so it's inspectable per song."""
    song_id = song["id"]
    started_at = time.time()
    ctx.begin("melody")
    if store.exists(song_id, artifacts.KIND_MELODY):
        # Already transcribed on a previous run - reuse, and make sure the
        # MIDI exists too (it may have been deleted to force regeneration).
        result = store.read_json(song_id, artifacts.KIND_MELODY)
        if not store.exists(song_id, artifacts.KIND_MIDI):
            store.write_bytes(song_id, artifacts.KIND_MIDI, _melody_midi_bytes(result))
        _record(produced, store, song_id, artifacts.KIND_MELODY)
        _record(produced, store, song_id, artifacts.KIND_MIDI)
        _stage(
            ctx,
            "melody",
            "reused",
            f"reused {len(result.get('notes') or [])} notes from a previous run",
            started_at=started_at,
            output_path=store.path(song_id, artifacts.KIND_MELODY),
            output_hash=store.content_hash(song_id, artifacts.KIND_MELODY),
        )
        if result.get("bpm"):
            _stage(ctx, "tempo", "reused", f"{result['bpm']} BPM (from a previous run)", started_at=started_at)
        return result

    if not song.get("ytmusic_video_id"):
        _stage(ctx, "melody", "skipped", "no source-recording id to transcribe from", started_at=started_at)
        return None
    if not vocal_transcribe.available():
        _stage(
            ctx,
            "melody",
            "skipped",
            "vocal-transcription add-on (Demucs + Basic Pitch) not installed",
            started_at=started_at,
        )
        return None

    try:
        result = _build_vocal_melody(song, store, produced, resolve_audio_url, ctx)
    except Exception as exc:
        _stage(
            ctx,
            "melody",
            "failed",
            f"vocal transcription failed: {exc}",
            started_at=started_at,
            # Best-effort: the vocal stem may never have been written (e.g.
            # decode/separation itself is what failed) - content_hash()
            # returns None for a missing file rather than raising.
            input_hashes=[store.content_hash(song_id, artifacts.KIND_VOCALS)],
        )
        return None

    result["notes"] = gate_notes_to_lyrics(result.get("notes") or [], (lyrics or {}).get("synced"))
    store.write_json(song_id, artifacts.KIND_MELODY, result)
    store.write_bytes(song_id, artifacts.KIND_MIDI, _melody_midi_bytes(result))
    _record(produced, store, song_id, artifacts.KIND_MELODY)
    _record(produced, store, song_id, artifacts.KIND_MIDI)
    _stage(
        ctx,
        "melody",
        "ok",
        f"transcribed {len(result['notes'])} notes from the isolated vocal",
        started_at=started_at,
        input_hashes=[store.content_hash(song_id, artifacts.KIND_VOCALS)],
        output_path=store.path(song_id, artifacts.KIND_MELODY),
        output_hash=store.content_hash(song_id, artifacts.KIND_MELODY),
    )
    return result


def _stage_instrumental(song, store, produced, resolve_audio_url, ctx):
    """Make sure the song has a playable instrumental (the decoded original
    mix minus its Demucs vocal stem). This is THE backing track the player
    prefers: it comes from the same recording (indeed the same separation of
    the same file) the lyrics, melody, and singer-assist stem are timed to,
    so everything is in sync by construction - no cross-video offset to
    correct. Best-effort like melody: without it the player falls back to
    streaming the picked karaoke video, with its manual sync trim.

    A fresh melody run already wrote the instrumental during separation (see
    _build_vocal_melody) - this stage then just records it. For songs
    processed before the instrumental existed (melody reused, stem absent),
    it re-separates from the kept mix, re-decoding the mix first if that was
    cleaned up too."""
    song_id = song["id"]
    started_at = time.time()
    ctx.begin("instrumental")

    if store.exists(song_id, artifacts.KIND_INSTRUMENTAL):
        # Written this run alongside the vocal stem (a fresh KIND_VOCALS in
        # `produced` is the tell), or kept from a previous run.
        fresh = any(entry["kind"] == artifacts.KIND_VOCALS for entry in produced)
        _record(produced, store, song_id, artifacts.KIND_INSTRUMENTAL)
        _stage(
            ctx,
            "instrumental",
            "ok" if fresh else "reused",
            "separated alongside the vocal stem" if fresh else "reused instrumental from a previous run",
            started_at=started_at,
            output_path=store.path(song_id, artifacts.KIND_INSTRUMENTAL),
            output_hash=store.content_hash(song_id, artifacts.KIND_INSTRUMENTAL),
        )
        return

    if not vocal_transcribe.available():
        _stage(
            ctx,
            "instrumental",
            "skipped",
            "vocal-separation add-on (Demucs) not installed",
            started_at=started_at,
        )
        return
    if not store.exists(song_id, artifacts.KIND_MIX) and not song.get("ytmusic_video_id"):
        _stage(ctx, "instrumental", "skipped", "no source recording to separate from", started_at=started_at)
        return

    try:
        mix_path = store.path(song_id, artifacts.KIND_MIX)
        if not store.exists(song_id, artifacts.KIND_MIX):
            vocal_transcribe._decode_to_wav(resolve_audio_url(song["ytmusic_video_id"]), mix_path)
            _record(produced, store, song_id, artifacts.KIND_MIX)
        vocal_transcribe.separate_vocals(
            mix_path,
            store.path(song_id, artifacts.KIND_VOCALS),
            store.path(song_id, artifacts.KIND_INSTRUMENTAL),
        )
    except Exception as exc:
        _stage(
            ctx,
            "instrumental",
            "failed",
            f"vocal separation failed: {exc}",
            started_at=started_at,
            input_hashes=[store.content_hash(song_id, artifacts.KIND_MIX)],
        )
        return

    # The vocal stem was refreshed as a side product of the same separation.
    _record(produced, store, song_id, artifacts.KIND_VOCALS)
    _record(produced, store, song_id, artifacts.KIND_INSTRUMENTAL)
    _stage(
        ctx,
        "instrumental",
        "ok",
        "separated the instrumental from the original recording",
        started_at=started_at,
        input_hashes=[store.content_hash(song_id, artifacts.KIND_MIX)],
        output_path=store.path(song_id, artifacts.KIND_INSTRUMENTAL),
        output_hash=store.content_hash(song_id, artifacts.KIND_INSTRUMENTAL),
    )


def _melody_midi_bytes(result):
    """Serialize a melody result to MIDI bytes at its estimated tempo (or the
    nominal default when no BPM was determined)."""
    return midi.notes_to_midi_bytes(result.get("notes") or [], bpm=result.get("bpm") or midi.DEFAULT_BPM)


def build_processor(store, *, fetch_lyrics, find_video, resolve_audio_url):
    """Compose the per-song pipeline over an ArtifactStore.

    Injected callables (all network/host bound, supplied by app.py):
      fetch_lyrics(artist, title, duration)  -> {"synced": [...], ...} or None
      find_video(artist, title, duration)    -> {"video_id", "duration_seconds"} or None
      resolve_audio_url(ytmusic_video_id)    -> a playable audio-only URL of the ORIGINAL recording

    Synced lyrics and a backing video are required (missing either fails the
    song); vocals/melody/MIDI are best-effort extras. Returns the result dict
    the worker stores, including an `artifacts` list of every file produced or
    reused this run, a `report` mapping each stage to its outcome
    (ok/reused/skipped/failed + a human-readable detail), and `stage_runs` -
    the same per-stage outcomes plus timing/content-hash lineage, which the
    library layer (not this module - see RunContext) persists to the
    stage_runs table.

    `run_id` identifies this processing attempt for lineage/log correlation.
    LibraryWorker._process_one always supplies one (uuid4 hex); callers that
    invoke process() directly (every existing pipeline test) get one
    generated here so they don't have to care. `observer` is the optional
    live-progress hook threaded onto RunContext (see its docstring); the
    worker supplies one, tests/CLI callers may not.
    """

    def process(song, run_id=None, observer=None):
        run_id = run_id or uuid.uuid4().hex
        ctx = RunContext(run_id, song["id"], observer=observer)
        produced = []
        lyrics = _stage_lyrics(song, store, produced, fetch_lyrics, ctx)
        video = _stage_video(song, find_video, ctx)
        melody_result = _stage_melody(song, lyrics, store, produced, resolve_audio_url, ctx)
        _stage_instrumental(song, store, produced, resolve_audio_url, ctx)

        return {
            "lyrics": lyrics,
            "video_id": video["video_id"],
            "duration_seconds": video.get("duration_seconds"),
            "melody": melody_result,
            "artifacts": produced,
            "report": ctx.report,
            "stage_runs": ctx.stage_runs,
            "run_id": run_id,
        }

    return process
