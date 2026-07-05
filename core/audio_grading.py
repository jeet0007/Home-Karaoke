"""Real-time pitch + energy based performance scoring.

Two grading modes, chosen per session by whether a reference melody is
available (see vocal_transcribe.py / pipeline.py for where one comes from):

Without a reference melody (the original v1 behavior), this scores the
*microphone input alone*:

  - energy/voice-activity: is there singing at all, or silence/background
    noise?
  - pitch stability: once a note is detected, is it held steadily or
    wavering/off? A voice sliding around is scored lower than one holding a
    clean, steady pitch.

With a reference melody - note segments transcribed from the ORIGINAL
recording's isolated vocal by the library's background pipeline
(vocal_transcribe.py/pipeline.py), plus playback-position sync frames from
the client (set_position) - the score
also measures *melody accuracy*: how close the sung pitch is to the target
note active at the current playback position, octave-folded so singing in
your own octave (very common: a male voice on a female vocal line and vice
versa) is not punished. Accuracy and stability are blended (accuracy
dominating); stretches where the melody has no active note (instrumental
breaks) fall back to stability-only, same as no-reference mode.

Pitch detection uses a from-scratch YIN autocorrelation implementation
(de Cheveigne & Kawahara, 2002) on top of numpy - see PR body for why this
was chosen over aubio (native build failed in this environment) and over
librosa (works, but pulls in scipy/numba/scikit-learn for a batch/file
oriented API when we only need small streaming frames).
"""

import collections
import math

import numpy as np

MIN_FREQUENCY_HZ = 70.0
MAX_FREQUENCY_HZ = 1000.0
YIN_THRESHOLD = 0.15

# Below this RMS amplitude (float32 samples in [-1, 1]) input is treated as
# silence/room noise rather than singing. Mic gain varies a lot across
# hardware; this is a starting point, tunable without code changes.
SILENCE_RMS_THRESHOLD = 0.01

# How much recent audio each pitch estimate looks at, and how often a score
# update is produced. A short analysis window still captures a couple of
# periods even at the bottom of the vocal range (70 Hz -> ~14ms/period), and
# emitting only every ~200ms keeps updates to a "few times a second" instead
# of flooding the socket every hop.
FRAME_SECONDS = 0.09
HOP_SECONDS = 0.2

# Number of recent pitch estimates (in cents) used to judge stability - a
# held note wavers less across this window than a sliding/wrong one.
STABILITY_WINDOW = 8

# Cents of standard deviation across STABILITY_WINDOW estimates at which the
# stability score bottoms out at 0. Natural vibrato is commonly well under
# this; a full semitone (100 cents) of wander scores near-zero.
STABILITY_CENTS_FOR_ZERO = 150.0

# Score awarded to frames with energy but no confident pitch (a wrong /
# non-tonal noise, not silence) - some credit for "singing", little for
# accuracy.
NOISE_SCORE = 10.0

# Baseline stability score for the first pitched frame(s), before enough
# history has accumulated to judge steadiness.
FIRST_PITCH_SCORE = 45.0

# Exponential smoothing applied to the emitted score so it doesn't jump
# frame-to-frame; lower = smoother/slower to react.
SCORE_SMOOTHING = 0.35

CENTS_REFERENCE_HZ = 440.0

# -- Reference-melody accuracy scoring ---------------------------------

# Octave-folded distance from the target note (in cents) at which the
# accuracy score bottoms out at 0. 250 means: dead-on = 100, a
# quarter-tone off (50c) = 80, a full semitone off (100c) = 60, two
# semitones (200c) = 20 - generous enough for home karaoke, strict enough
# that singing a different melody scores near zero.
ACCURACY_CENTS_FOR_ZERO = 250.0

# Blend between melody accuracy and pitch stability while a target note is
# active. Accuracy dominates - hitting the right note matters more than
# holding a wrong one rock-steady.
ACCURACY_WEIGHT = 0.7

# Safety cap on client-supplied melodies (see parse_melody) - even a long,
# busy song segments to well under a thousand notes.
MELODY_MAX_NOTES = 5000

_A4_MIDI = 69.0


def compute_rms(samples):
    if samples.size == 0:
        return 0.0
    return float(np.sqrt(np.mean(np.square(samples, dtype=np.float64))))


def _difference_function(samples, tau_max):
    n = len(samples)
    diff = np.zeros(tau_max, dtype=np.float64)
    samples = samples.astype(np.float64, copy=False)
    for tau in range(1, tau_max):
        delta = samples[: n - tau] - samples[tau:]
        diff[tau] = np.dot(delta, delta)
    return diff


def _cumulative_mean_normalized_difference(diff):
    cmnd = np.ones_like(diff)
    running_sum = 0.0
    for tau in range(1, len(diff)):
        running_sum += diff[tau]
        cmnd[tau] = diff[tau] * tau / running_sum if running_sum > 0 else 1.0
    return cmnd


def _absolute_threshold(cmnd, tau_min, tau_max, threshold):
    """First tau past tau_min where cmnd dips below threshold and is a local
    minimum (the classic YIN "first dip" search). Returns None (no confident
    pitch) rather than falling back to a low-confidence global minimum -
    for grading, an unclear pitch should read as noise, not a guess."""
    tau = tau_min
    while tau < tau_max - 1:
        if cmnd[tau] < threshold:
            while tau + 1 < tau_max and cmnd[tau + 1] < cmnd[tau]:
                tau += 1
            return tau
        tau += 1
    return None


def _parabolic_interpolation(cmnd, tau):
    if tau <= 0 or tau + 1 >= len(cmnd):
        return float(tau)

    s0, s1, s2 = cmnd[tau - 1], cmnd[tau], cmnd[tau + 1]
    denom = s0 - 2 * s1 + s2
    if denom == 0:
        return float(tau)

    offset = 0.5 * (s0 - s2) / denom
    return tau + offset


def yin_pitch(samples, sample_rate, fmin=MIN_FREQUENCY_HZ, fmax=MAX_FREQUENCY_HZ, threshold=YIN_THRESHOLD):
    """Estimate the fundamental frequency of `samples` in Hz, or None if no
    confident periodic pitch is found (silence, noise, or below fmin/above
    fmax)."""
    tau_min = max(1, int(sample_rate / fmax))
    tau_max = min(len(samples) // 2, int(sample_rate / fmin))
    if tau_max <= tau_min + 1:
        return None

    diff = _difference_function(samples, tau_max)
    cmnd = _cumulative_mean_normalized_difference(diff)
    tau = _absolute_threshold(cmnd, tau_min, tau_max, threshold)
    if tau is None:
        return None

    refined_tau = _parabolic_interpolation(cmnd, tau)
    if refined_tau <= 0:
        return None

    frequency = sample_rate / refined_tau
    if not (fmin <= frequency <= fmax):
        return None
    return frequency


def _hz_to_cents(frequency, reference_hz=CENTS_REFERENCE_HZ):
    return 1200.0 * math.log2(frequency / reference_hz)


def _stability_score(cents_history):
    if len(cents_history) < 2:
        return FIRST_PITCH_SCORE
    std_cents = float(np.std(np.asarray(cents_history, dtype=np.float64)))
    return max(0.0, 100.0 * (1.0 - std_cents / STABILITY_CENTS_FOR_ZERO))


def _hz_to_midi(frequency_hz):
    return _A4_MIDI + 12.0 * math.log2(frequency_hz / CENTS_REFERENCE_HZ)


def parse_melody(raw):
    """Sanitize a client-supplied melody (from the /grade handshake) into a
    sorted list of {"start_ms", "end_ms", "midi"} note dicts, or None when
    nothing usable was provided. Client input is untrusted: non-numeric
    fields, inverted/degenerate segments, and absurd lengths are dropped
    rather than erroring the session - a bad melody just means falling back
    to no-reference grading."""
    if not isinstance(raw, list):
        return None

    notes = []
    for entry in raw[:MELODY_MAX_NOTES]:
        if not isinstance(entry, dict):
            continue
        try:
            start_ms = float(entry["start_ms"])
            end_ms = float(entry["end_ms"])
            midi = float(entry["midi"])
        except (KeyError, TypeError, ValueError):
            continue
        if not (math.isfinite(start_ms) and math.isfinite(end_ms) and math.isfinite(midi)):
            continue
        if end_ms <= start_ms or start_ms < 0:
            continue
        notes.append({"start_ms": start_ms, "end_ms": end_ms, "midi": midi})

    if not notes:
        return None
    notes.sort(key=lambda n: n["start_ms"])
    return notes


def _note_at(notes, pos_ms):
    """The melody note active at pos_ms, or None (instrumental gap). Binary
    search on start_ms; `notes` must be sorted (see parse_melody)."""
    low, high = 0, len(notes) - 1
    candidate = None
    while low <= high:
        mid = (low + high) // 2
        if notes[mid]["start_ms"] <= pos_ms:
            candidate = notes[mid]
            low = mid + 1
        else:
            high = mid - 1
    if candidate is not None and pos_ms < candidate["end_ms"]:
        return candidate
    return None


def _octave_folded_cents_off(detected_midi, target_midi):
    """Distance (cents) from the target note, folded to the nearest octave:
    singing the right note in a different octave counts as on-pitch."""
    cents_off = (detected_midi - target_midi) * 100.0
    return abs((cents_off + 600.0) % 1200.0 - 600.0)


def _accuracy_score(detected_midi, target_midi):
    cents_off = _octave_folded_cents_off(detected_midi, target_midi)
    return max(0.0, 100.0 * (1.0 - cents_off / ACCURACY_CENTS_FOR_ZERO))


class RealtimeGrader:
    """Stateful scorer for one grading session (one WebSocket connection).

    Feed it raw float32 PCM via push_samples() as chunks arrive; it returns
    zero or more score updates (dicts), throttled to roughly HOP_SECONDS
    apart regardless of how the caller's chunk sizes line up.

    `melody` (optional, pre-sanitized via parse_melody) enables reference
    grading; the caller must then also feed playback-position syncs via
    set_position() so mic time can be mapped onto song time - without at
    least one sync, grading behaves exactly as in no-reference mode.
    """

    def __init__(self, sample_rate, melody=None):
        if sample_rate <= 0:
            raise ValueError(f"sample_rate must be positive, got {sample_rate}")
        self.sample_rate = sample_rate
        self.frame_size = max(256, int(sample_rate * FRAME_SECONDS))
        self.hop_size = max(1, int(sample_rate * HOP_SECONDS))
        self.melody = melody or None

        self._buffer = np.zeros(0, dtype=np.float32)
        self._samples_since_emit = 0
        self._samples_seen = 0
        self._pitch_history_cents = collections.deque(maxlen=STABILITY_WINDOW)
        self._score_ema = 0.0
        # song position minus mic-stream position, in ms; None until the
        # first set_position() call.
        self._position_offset_ms = None

    def set_position(self, pos_ms):
        """Sync: the backing track is at `pos_ms` right now. Maps the mic
        stream's sample clock onto song time for melody lookups; called
        repeatedly, so pause/seek/drift all self-correct at the next sync."""
        pos_ms = float(pos_ms)
        if not math.isfinite(pos_ms):
            return
        stream_ms = self._samples_seen / self.sample_rate * 1000.0
        self._position_offset_ms = pos_ms - stream_ms

    def _song_position_ms(self, stream_t_ms):
        if self._position_offset_ms is None:
            return None
        return stream_t_ms + self._position_offset_ms

    def push_samples(self, chunk):
        chunk = np.asarray(chunk, dtype=np.float32).reshape(-1)
        self._buffer = np.concatenate([self._buffer, chunk])[-self.frame_size :]
        self._samples_since_emit += len(chunk)
        self._samples_seen += len(chunk)

        updates = []
        while self._samples_since_emit >= self.hop_size:
            self._samples_since_emit -= self.hop_size
            updates.append(self._analyze())
        return updates

    def _target_note(self, stream_t_ms):
        if self.melody is None:
            return None
        song_pos = self._song_position_ms(stream_t_ms)
        if song_pos is None:
            return None
        return _note_at(self.melody, song_pos)

    def _analyze(self):
        frame = self._buffer
        rms = compute_rms(frame)
        stream_t_ms = self._samples_seen / self.sample_rate * 1000.0
        target_note = self._target_note(stream_t_ms)
        result = {
            "t_ms": int(stream_t_ms),
            "singing": False,
            "frequency_hz": None,
            "target_midi": target_note["midi"] if target_note else None,
            "score": 0,
        }

        if rms < SILENCE_RMS_THRESHOLD:
            self._pitch_history_cents.clear()
            self._score_ema *= 1 - SCORE_SMOOTHING
            result["score"] = int(round(self._score_ema))
            return result

        result["singing"] = True
        frequency = yin_pitch(frame, self.sample_rate)

        if frequency is None:
            self._pitch_history_cents.clear()
            target_score = NOISE_SCORE
        else:
            result["frequency_hz"] = round(frequency, 1)
            self._pitch_history_cents.append(_hz_to_cents(frequency))
            stability = _stability_score(self._pitch_history_cents)
            if target_note is not None:
                accuracy = _accuracy_score(_hz_to_midi(frequency), target_note["midi"])
                target_score = ACCURACY_WEIGHT * accuracy + (1.0 - ACCURACY_WEIGHT) * stability
            else:
                target_score = stability

        self._score_ema += (target_score - self._score_ema) * SCORE_SMOOTHING
        result["score"] = int(round(max(0.0, min(100.0, self._score_ema))))
        return result
