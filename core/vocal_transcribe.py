"""Isolated-vocal melody: Demucs separation -> Basic Pitch transcription.

The ONLY source for the player's note guide. It deliberately crosses this
project's earlier "no heavy audio processing" line: Demucs pulls in torch.
Both heavy deps are therefore OPTIONAL and LAZY-imported - available()
reports whether they're installed. When they're not, the pipeline produces
NO melody (the song still plays with lyrics, just no guide); there is
deliberately no full-mix fallback, because transcribing the dominant pitch
of a full studio mix tracks the bass/accompaniment as often as the vocal - a
guide that's wrong is worse than no guide at all.

Why isolating the vocal first matters: on a full mix the loudest periodic
source in a frame is often the bass or a loud instrument, not the lead
vocal. Demucs strips everything but the vocal, so Basic Pitch transcribes a
signal that is (almost) only the singer - there is nothing left to confuse
it with.

Pipeline (orchestrated by pipeline.py, which persists the intermediate mix
and vocal stem as reusable artifacts - see artifacts.py):

    audio-only stream URL of the ORIGINAL recording (resolved by the caller
    via yt-dlp)
      -> _decode_to_wav: ffmpeg decode to a 44.1kHz stereo WAV (Demucs' rate)
      -> separate_vocals: Demucs -> isolated vocal stem WAV
      -> transcribe: Basic Pitch -> polyphonic note events
      -> note_events_to_segments: monophonic reduction + segment shaping
         -> [{start_ms,end_ms,midi}]

Testability: the ML stages (separate_vocals, transcribe) are thin
module-level wrappers so tests can monkeypatch them; all the pure
conversion logic (note_events_to_segments, reduce_to_monophonic) is
import-light and covered without torch/tensorflow present.
"""

import os
import shutil
import subprocess

# Demucs' models are trained at 44.1kHz stereo; feeding that rate in avoids
# an internal resample and keeps the separated vocal at full quality for
# Basic Pitch.
DEMUCS_SAMPLE_RATE_HZ = 44100

# Two-stem separation (vocals vs. everything else) is roughly half the work
# of the default 4-stem split and all we need. Overridable for hosts that
# want a different/smaller model.
DEMUCS_MODEL = os.environ.get("DEMUCS_MODEL", "htdemucs")
DEMUCS_DEVICE = os.environ.get("DEMUCS_DEVICE", "cpu")

# Notes shorter than this out of Basic Pitch are transcription flecks, not
# sung notes - drop them (mirrors melody.MIN_NOTE_MS).
MIN_NOTE_MS = 120

_FFMPEG_BINARY = "ffmpeg"

# available() is pure dependency probing; cache it so the worker doesn't
# re-import torch on every song.
_availability = None


def available():
    """True only when BOTH heavy deps import cleanly. Cached. The import is
    the availability test - a partially-installed torch/demucs surfaces here
    as unavailable rather than crashing mid-pipeline later."""
    global _availability
    if _availability is None:
        _availability = _probe_dependencies() and shutil.which(_FFMPEG_BINARY) is not None
    return _availability


def _probe_dependencies():
    try:
        import demucs.api  # noqa: F401
        import basic_pitch  # noqa: F401
        from basic_pitch.inference import predict  # noqa: F401
    except Exception:
        return False
    return True


def _decode_to_wav(audio_url, out_path, timeout=120):
    """Decode `audio_url` to a 44.1kHz stereo WAV via ffmpeg (Demucs wants a
    file at its native rate, in stereo, on disk - it operates on
    files/tensors, not a raw PCM stream)."""
    ffmpeg_path = shutil.which(_FFMPEG_BINARY)
    if not ffmpeg_path:
        raise RuntimeError("ffmpeg not found on PATH")

    cmd = [
        ffmpeg_path,
        "-v",
        "error",
        "-y",
        "-i",
        audio_url,
        "-vn",
        "-ac",
        "2",
        "-ar",
        str(DEMUCS_SAMPLE_RATE_HZ),
        out_path,
    ]
    proc = subprocess.run(cmd, capture_output=True, timeout=timeout)
    if proc.returncode != 0:
        stderr = proc.stderr.decode("utf-8", "replace").strip() or "unknown ffmpeg error"
        raise RuntimeError(f"ffmpeg failed: {stderr}")
    return out_path


def separate_vocals(input_wav_path, out_vocal_path):
    """Isolate the vocal stem from `input_wav_path` into `out_vocal_path`
    (WAV) using Demucs. Lazy-imports torch/demucs; only called when
    available() is True."""
    from demucs.api import Separator, save_audio

    separator = Separator(model=DEMUCS_MODEL, device=DEMUCS_DEVICE)
    _origin, stems = separator.separate_audio_file(input_wav_path)
    if "vocals" not in stems:
        raise RuntimeError(f"Demucs model {DEMUCS_MODEL!r} produced no 'vocals' stem")
    save_audio(stems["vocals"], out_vocal_path, samplerate=separator.samplerate)
    return out_vocal_path


def transcribe(vocal_wav_path):
    """Basic Pitch note events for a (vocal) WAV. Returns the raw list of
    (start_s, end_s, pitch_midi, amplitude, pitch_bends) tuples. Lazy-imports
    basic_pitch; only called when available() is True."""
    from basic_pitch import ICASSP_2022_MODEL_PATH
    from basic_pitch.inference import predict

    _model_output, _midi_data, note_events = predict(vocal_wav_path, ICASSP_2022_MODEL_PATH)
    return note_events


def reduce_to_monophonic(segments):
    """Collapse overlapping notes to a single melody line: where two notes
    overlap in time, keep the louder (higher-amplitude) one and trim/drop the
    other. Basic Pitch on an isolated vocal is mostly monophonic already, but
    breaths, harmonies, and octave doublings produce occasional overlaps that
    would clutter the note guide.

    `segments` is a list of {start_ms, end_ms, midi, amplitude}; returns the
    same shape, amplitude retained (dropped later in note_events_to_segments).
    """
    ordered = sorted(segments, key=lambda s: (s["start_ms"], -s["amplitude"]))
    kept = []
    for seg in ordered:
        overlap = next((k for k in kept if seg["start_ms"] < k["end_ms"] and seg["end_ms"] > k["start_ms"]), None)
        if overlap is None:
            kept.append(dict(seg))
            continue
        if seg["amplitude"] <= overlap["amplitude"]:
            # Quieter note: keep only the part after the louder one ends.
            if seg["end_ms"] > overlap["end_ms"]:
                trimmed = dict(seg, start_ms=overlap["end_ms"])
                if trimmed["end_ms"] > trimmed["start_ms"]:
                    kept.append(trimmed)
        else:
            # Louder note wins the overlap: shorten the earlier, quieter one.
            overlap["end_ms"] = min(overlap["end_ms"], seg["start_ms"])
            kept.append(dict(seg))
    kept = [k for k in kept if k["end_ms"] - k["start_ms"] >= MIN_NOTE_MS]
    kept.sort(key=lambda s: s["start_ms"])
    return kept


def note_events_to_segments(note_events):
    """Convert Basic Pitch's (start_s, end_s, pitch, amplitude, bends) tuples
    into the player's {start_ms, end_ms, midi} segments, monophonic-reduced.
    Pure - the unit-testable heart of this module."""
    raw = []
    for event in note_events:
        start_s, end_s, pitch = event[0], event[1], event[2]
        amplitude = event[3] if len(event) > 3 else 1.0
        start_ms = int(round(float(start_s) * 1000))
        end_ms = int(round(float(end_s) * 1000))
        if end_ms <= start_ms:
            continue
        raw.append(
            {"start_ms": start_ms, "end_ms": end_ms, "midi": int(round(float(pitch))), "amplitude": float(amplitude)}
        )

    mono = reduce_to_monophonic(raw)
    return [{"start_ms": s["start_ms"], "end_ms": s["end_ms"], "midi": s["midi"]} for s in mono]
