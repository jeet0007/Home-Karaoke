"""Core music -> MIDI pipeline, separate from presentation.

This module is the ENGINE. Given a song identity it produces the reusable
artifacts a karaoke session needs and persists each to the ArtifactStore:

    lyrics   -> synced lyric lines
    vocals   -> isolated vocal stem (only when the ML deps are installed)
    melody   -> note segments + a real .mid file (the "music -> MIDI" output)

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

from core import artifacts
from core import midi
from core import tempo
from core import vocal_transcribe
from core.library import ProcessingError

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


def _stage(report, name, status, detail):
    """Record one stage's outcome. status is ok/reused/skipped/failed; detail
    is a short human-readable explanation of what passed / why it didn't."""
    report[name] = {"status": status, "detail": detail}


def _stage_lyrics(song, store, produced, fetch_lyrics, report):
    lyrics = fetch_lyrics(song["artist"], song["title"], song["duration_seconds"])
    if not lyrics or not lyrics.get("synced"):
        _stage(report, "lyrics", "failed", "no synced lyrics found in any source")
        raise ProcessingError("no synced lyrics found in any source", report=report)
    store.write_json(song["id"], artifacts.KIND_LYRICS, lyrics)
    _record(produced, store, song["id"], artifacts.KIND_LYRICS)
    _stage(
        report,
        "lyrics",
        "ok",
        f"{len(lyrics['synced'])} synced lines from {lyrics.get('source') or 'unknown source'}",
    )
    return lyrics


def _stage_video(song, find_video, report):
    video = find_video(song["artist"], song["title"], song["duration_seconds"])
    if not video or not video.get("video_id"):
        _stage(report, "video", "failed", "no karaoke backing track found")
        raise ProcessingError("no karaoke backing track found", report=report)
    _stage(report, "video", "ok", f"picked backing video {video['video_id']}")
    return video


def _estimate_tempo(store, song_id, report):
    """Best-effort BPM of the decoded full mix (librosa; see tempo.py). The
    mix drives the beat, so tempo is estimated from it, not the vocal. Every
    outcome is recorded in `report`; returns the BPM or None."""
    if not tempo.available():
        _stage(report, "tempo", "skipped", "tempo add-on (librosa) not installed")
        return None
    if not store.exists(song_id, artifacts.KIND_MIX):
        _stage(report, "tempo", "skipped", "no decoded mix to analyze")
        return None
    try:
        bpm = tempo.estimate_bpm(store.path(song_id, artifacts.KIND_MIX))
    except Exception as exc:
        _stage(report, "tempo", "failed", f"tempo estimation failed: {exc}")
        return None
    if bpm is None:
        _stage(report, "tempo", "failed", "could not estimate a tempo")
        return None
    _stage(report, "tempo", "ok", f"{bpm} BPM")
    return bpm


def _build_vocal_melody(song, store, produced, resolve_audio_url, report):
    """Isolated-vocal path: keep the mix and the vocal stem as artifacts so
    either can be reused. Each sub-step is skipped when its file already
    exists."""
    song_id = song["id"]
    vocal_path = store.path(song_id, artifacts.KIND_VOCALS)

    if not store.exists(song_id, artifacts.KIND_VOCALS):
        mix_path = store.path(song_id, artifacts.KIND_MIX)
        if not store.exists(song_id, artifacts.KIND_MIX):
            vocal_transcribe._decode_to_wav(resolve_audio_url(song["ytmusic_video_id"]), mix_path)
            _record(produced, store, song_id, artifacts.KIND_MIX)
        vocal_transcribe.separate_vocals(mix_path, vocal_path)
        _record(produced, store, song_id, artifacts.KIND_VOCALS)

    segments = vocal_transcribe.note_events_to_segments(vocal_transcribe.transcribe(vocal_path))
    duration_s = max((n["end_ms"] for n in segments), default=0) / 1000.0
    bpm = _estimate_tempo(store, song_id, report)
    return {"notes": segments, "duration_s": duration_s, "source": "demucs+basic-pitch", "bpm": bpm}


def _stage_melody(song, lyrics, store, produced, resolve_audio_url, report):
    """Produce the melody json + .mid artifacts from the ISOLATED VOCAL only
    (Demucs -> Basic Pitch). Vocal-only and best-effort: if the ML deps are
    not installed, the song has no ytmusic id, or transcription fails, the
    song is left playable with no note guide (returns None) - there is no
    low-quality full-mix fallback (see the module docstring). Every outcome
    is recorded in `report` so it's inspectable per song."""
    song_id = song["id"]
    if store.exists(song_id, artifacts.KIND_MELODY):
        # Already transcribed on a previous run - reuse, and make sure the
        # MIDI exists too (it may have been deleted to force regeneration).
        result = store.read_json(song_id, artifacts.KIND_MELODY)
        if not store.exists(song_id, artifacts.KIND_MIDI):
            store.write_bytes(song_id, artifacts.KIND_MIDI, _melody_midi_bytes(result))
        _record(produced, store, song_id, artifacts.KIND_MELODY)
        _record(produced, store, song_id, artifacts.KIND_MIDI)
        _stage(report, "melody", "reused", f"reused {len(result.get('notes') or [])} notes from a previous run")
        if result.get("bpm"):
            _stage(report, "tempo", "reused", f"{result['bpm']} BPM (from a previous run)")
        return result

    if not song.get("ytmusic_video_id"):
        _stage(report, "melody", "skipped", "no source-recording id to transcribe from")
        return None
    if not vocal_transcribe.available():
        _stage(report, "melody", "skipped", "vocal-transcription add-on (Demucs + Basic Pitch) not installed")
        return None

    try:
        result = _build_vocal_melody(song, store, produced, resolve_audio_url, report)
    except Exception as exc:
        _stage(report, "melody", "failed", f"vocal transcription failed: {exc}")
        return None

    result["notes"] = gate_notes_to_lyrics(result.get("notes") or [], (lyrics or {}).get("synced"))
    store.write_json(song_id, artifacts.KIND_MELODY, result)
    store.write_bytes(song_id, artifacts.KIND_MIDI, _melody_midi_bytes(result))
    _record(produced, store, song_id, artifacts.KIND_MELODY)
    _record(produced, store, song_id, artifacts.KIND_MIDI)
    _stage(report, "melody", "ok", f"transcribed {len(result['notes'])} notes from the isolated vocal")
    return result


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
    reused this run, and a `report` mapping each stage to its outcome
    (ok/reused/skipped/failed + a human-readable detail) so a song's
    processing is fully inspectable afterwards.
    """

    def process(song):
        produced = []
        report = {}
        lyrics = _stage_lyrics(song, store, produced, fetch_lyrics, report)
        video = _stage_video(song, find_video, report)
        melody_result = _stage_melody(song, lyrics, store, produced, resolve_audio_url, report)

        return {
            "lyrics": lyrics,
            "video_id": video["video_id"],
            "duration_seconds": video.get("duration_seconds"),
            "melody": melody_result,
            "artifacts": produced,
            "report": report,
        }

    return process
