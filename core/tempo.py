"""Tempo (BPM) estimation via librosa.

Part of the optional ML add-on (requirements-ml.txt). BPM is estimated from
the decoded FULL MIX, not the isolated vocal - the drums and bass drive the
beat, so beat tracking wants the whole song. The pipeline already decodes
the mix for Demucs, so this rides that artifact.

Lazy-imported and best-effort, same pattern as vocal_transcribe: available()
reports whether librosa is installed. When it isn't (or estimation fails)
the song simply carries no BPM - the note guide still works and the exported
MIDI just falls back to a nominal 120 tempo.
"""

import math

# available() is pure dependency probing; cache it so the worker doesn't
# re-import librosa on every song.
_availability = None


def available():
    global _availability
    if _availability is None:
        _availability = _probe_dependencies()
    return _availability


def _probe_dependencies():
    try:
        import librosa  # noqa: F401
    except Exception:
        return False
    return True


def estimate_bpm(audio_path):
    """Estimate the global tempo (BPM) of the audio at `audio_path`, or None
    if it can't be determined. Lazy-imports librosa; only call when
    available()."""
    import librosa
    import numpy as np

    y, sr = librosa.load(audio_path, mono=True)
    tempo, _beat_frames = librosa.beat.beat_track(y=y, sr=sr)

    # librosa >= 0.10 returns tempo as an array; older releases as a scalar.
    values = np.atleast_1d(tempo).ravel()
    if values.size == 0:
        return None
    bpm = float(values[0])
    if not math.isfinite(bpm) or bpm <= 0:
        return None
    return round(bpm, 1)
