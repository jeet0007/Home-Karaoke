//! Client-side port of `core/audio_grading.py`.
//!
//! This is a *port*, not a redesign: same constants, same algorithm shape
//! (YIN autocorrelation-style difference function -> cumulative mean
//! normalized difference -> absolute-threshold first-dip search ->
//! parabolic interpolation), same octave-folded melody accuracy scoring,
//! same stability scoring, same EMA blending. See the Python module's
//! docstring for the "why" of each constant; this file only documents
//! where the Rust port deliberately differs in *mechanics* (not
//! semantics) to survive the trip through f32 JS typed arrays / WASM.
//!
//! Two traps worth flagging for anyone touching this file:
//! - All DSP arithmetic is done in f64 (matching numpy's `dtype=np.float64`
//!   casts in the Python version) even though samples arrive as f32 from
//!   the Web Audio API - accumulating the YIN difference function in f32
//!   measurably drifts the detected tau on longer frames.
//! - Octave folding uses `rem_euclid`, NOT the `%` operator - Rust's `%`
//!   on floats keeps the sign of the dividend (`-700.0 % 1200.0 ==
//!   -700.0`), whereas Python's `%` is floor-modulo (`-700.0 % 1200.0 ==
//!   500.0`). Using plain `%` here would silently invert scoring for
//!   every case where the sung pitch is below the target note.

use serde::{Deserialize, Serialize};
use std::collections::VecDeque;
use wasm_bindgen::prelude::*;

pub const MIN_FREQUENCY_HZ: f64 = 70.0;
pub const MAX_FREQUENCY_HZ: f64 = 1000.0;
pub const YIN_THRESHOLD: f64 = 0.15;

pub const SILENCE_RMS_THRESHOLD: f64 = 0.01;

pub const FRAME_SECONDS: f64 = 0.09;
pub const HOP_SECONDS: f64 = 0.2;

pub const STABILITY_WINDOW: usize = 8;
pub const STABILITY_CENTS_FOR_ZERO: f64 = 150.0;

pub const NOISE_SCORE: f64 = 10.0;
pub const FIRST_PITCH_SCORE: f64 = 45.0;
pub const SCORE_SMOOTHING: f64 = 0.35;

pub const CENTS_REFERENCE_HZ: f64 = 440.0;

pub const ACCURACY_CENTS_FOR_ZERO: f64 = 250.0;
pub const ACCURACY_WEIGHT: f64 = 0.7;

pub const MELODY_MAX_NOTES: usize = 5000;

const A4_MIDI: f64 = 69.0;

// -- YIN pitch detection -------------------------------------------------

pub fn compute_rms(samples: &[f32]) -> f64 {
    if samples.is_empty() {
        return 0.0;
    }
    let sum_sq: f64 = samples.iter().map(|&s| (s as f64) * (s as f64)).sum();
    (sum_sq / samples.len() as f64).sqrt()
}

fn difference_function(samples: &[f64], tau_max: usize) -> Vec<f64> {
    let n = samples.len();
    let mut diff = vec![0.0f64; tau_max];
    for tau in 1..tau_max {
        let mut acc = 0.0f64;
        for i in 0..(n - tau) {
            let delta = samples[i] - samples[i + tau];
            acc += delta * delta;
        }
        diff[tau] = acc;
    }
    diff
}

fn cumulative_mean_normalized_difference(diff: &[f64]) -> Vec<f64> {
    let mut cmnd = vec![1.0f64; diff.len()];
    let mut running_sum = 0.0f64;
    for tau in 1..diff.len() {
        running_sum += diff[tau];
        cmnd[tau] = if running_sum > 0.0 {
            diff[tau] * tau as f64 / running_sum
        } else {
            1.0
        };
    }
    cmnd
}

/// First tau past tau_min where cmnd dips below threshold and is a local
/// minimum (the classic YIN "first dip" search). None (no confident
/// pitch) rather than a low-confidence global-minimum fallback - for
/// grading, an unclear pitch should read as noise, not a guess.
fn absolute_threshold(cmnd: &[f64], tau_min: usize, tau_max: usize, threshold: f64) -> Option<usize> {
    let mut tau = tau_min;
    while tau < tau_max.saturating_sub(1) {
        if cmnd[tau] < threshold {
            while tau + 1 < tau_max && cmnd[tau + 1] < cmnd[tau] {
                tau += 1;
            }
            return Some(tau);
        }
        tau += 1;
    }
    None
}

fn parabolic_interpolation(cmnd: &[f64], tau: usize) -> f64 {
    if tau == 0 || tau + 1 >= cmnd.len() {
        return tau as f64;
    }
    let (s0, s1, s2) = (cmnd[tau - 1], cmnd[tau], cmnd[tau + 1]);
    let denom = s0 - 2.0 * s1 + s2;
    if denom == 0.0 {
        return tau as f64;
    }
    let offset = 0.5 * (s0 - s2) / denom;
    tau as f64 + offset
}

/// Estimate the fundamental frequency of `samples` in Hz, or `None` if no
/// confident periodic pitch is found (silence, noise, or below fmin/above
/// fmax). `samples` are upcast to f64 immediately (see module docs).
pub fn yin_pitch(
    samples: &[f32],
    sample_rate: f64,
    fmin: f64,
    fmax: f64,
    threshold: f64,
) -> Option<f64> {
    let tau_min = ((sample_rate / fmax) as usize).max(1);
    let tau_max = (samples.len() / 2).min((sample_rate / fmin) as usize);
    if tau_max <= tau_min + 1 {
        return None;
    }

    let samples_f64: Vec<f64> = samples.iter().map(|&s| s as f64).collect();
    let diff = difference_function(&samples_f64, tau_max);
    let cmnd = cumulative_mean_normalized_difference(&diff);
    let tau = absolute_threshold(&cmnd, tau_min, tau_max, threshold)?;

    let refined_tau = parabolic_interpolation(&cmnd, tau);
    if refined_tau <= 0.0 {
        return None;
    }

    let frequency = sample_rate / refined_tau;
    if frequency < fmin || frequency > fmax {
        return None;
    }
    Some(frequency)
}

fn hz_to_cents(frequency: f64, reference_hz: f64) -> f64 {
    1200.0 * (frequency / reference_hz).log2()
}

fn hz_to_midi(frequency_hz: f64) -> f64 {
    A4_MIDI + 12.0 * (frequency_hz / CENTS_REFERENCE_HZ).log2()
}

/// Population standard deviation (ddof=0, divide by N) - matching
/// numpy's default `np.std`, NOT a sample stddev (N-1).
fn population_std(values: &[f64]) -> f64 {
    let n = values.len() as f64;
    let mean = values.iter().sum::<f64>() / n;
    let variance = values.iter().map(|v| (v - mean).powi(2)).sum::<f64>() / n;
    variance.sqrt()
}

fn stability_score(cents_history: &VecDeque<f64>) -> f64 {
    if cents_history.len() < 2 {
        return FIRST_PITCH_SCORE;
    }
    let values: Vec<f64> = cents_history.iter().copied().collect();
    let std_cents = population_std(&values);
    (100.0 * (1.0 - std_cents / STABILITY_CENTS_FOR_ZERO)).max(0.0)
}

/// Distance (cents) from the target note, folded to the nearest octave:
/// singing the right note in a different octave counts as on-pitch.
/// Uses `rem_euclid` (floor-modulo), NOT `%` - see module docs.
fn octave_folded_cents_off(detected_midi: f64, target_midi: f64) -> f64 {
    let cents_off = (detected_midi - target_midi) * 100.0;
    ((cents_off + 600.0).rem_euclid(1200.0) - 600.0).abs()
}

fn accuracy_score(detected_midi: f64, target_midi: f64) -> f64 {
    let cents_off = octave_folded_cents_off(detected_midi, target_midi);
    (100.0 * (1.0 - cents_off / ACCURACY_CENTS_FOR_ZERO)).max(0.0)
}

// -- Melody (reference notes) --------------------------------------------

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Note {
    pub start_ms: f64,
    pub end_ms: f64,
    pub midi: f64,
}

/// Sanitize a client-supplied melody into a sorted list of notes, or
/// `None` when nothing usable was provided - mirrors
/// `core/audio_grading.parse_melody`. Input here is already JSON (the
/// player's own `getMelodyNotes()` shape), not untrusted network input,
/// but the same defensive filtering is kept for parity/robustness.
pub fn parse_melody(raw: &str) -> Option<Vec<Note>> {
    let value: serde_json::Value = serde_json::from_str(raw).ok()?;
    let array = value.as_array()?;

    let mut notes: Vec<Note> = Vec::new();
    for entry in array.iter().take(MELODY_MAX_NOTES) {
        let obj = match entry.as_object() {
            Some(o) => o,
            None => continue,
        };
        let start_ms = obj.get("start_ms").and_then(|v| v.as_f64());
        let end_ms = obj.get("end_ms").and_then(|v| v.as_f64());
        let midi = obj.get("midi").and_then(|v| v.as_f64());
        let (start_ms, end_ms, midi) = match (start_ms, end_ms, midi) {
            (Some(a), Some(b), Some(c)) => (a, b, c),
            _ => continue,
        };
        if !(start_ms.is_finite() && end_ms.is_finite() && midi.is_finite()) {
            continue;
        }
        if end_ms <= start_ms || start_ms < 0.0 {
            continue;
        }
        notes.push(Note { start_ms, end_ms, midi });
    }

    if notes.is_empty() {
        return None;
    }
    notes.sort_by(|a, b| a.start_ms.partial_cmp(&b.start_ms).unwrap());
    Some(notes)
}

/// The melody note active at `pos_ms`, or `None` (instrumental gap).
/// Binary search on start_ms; `notes` must be sorted (see parse_melody).
fn note_at(notes: &[Note], pos_ms: f64) -> Option<&Note> {
    if notes.is_empty() {
        return None;
    }
    let (mut low, mut high) = (0i64, notes.len() as i64 - 1);
    let mut candidate: Option<&Note> = None;
    while low <= high {
        let mid = ((low + high) / 2) as usize;
        if notes[mid].start_ms <= pos_ms {
            candidate = Some(&notes[mid]);
            low = mid as i64 + 1;
        } else {
            high = mid as i64 - 1;
        }
    }
    match candidate {
        Some(note) if pos_ms < note.end_ms => Some(note),
        _ => None,
    }
}

// -- Score update shape (matches RealtimeGrader._analyze()'s dict) ------

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ScoreUpdate {
    pub t_ms: i64,
    pub singing: bool,
    pub frequency_hz: Option<f64>,
    pub target_midi: Option<f64>,
    pub score: i32,
}

// -- Stateful scorer for one grading session -----------------------------

/// Direct port of `core/audio_grading.RealtimeGrader`. Feed it PCM via
/// `push_samples()` as chunks arrive (any chunk size - a AudioWorklet
/// quantum of 128 samples works fine, this buffers internally exactly
/// like the Python version); it returns zero or more score updates,
/// throttled to roughly `HOP_SECONDS` apart regardless of caller chunk
/// size.
#[wasm_bindgen]
pub struct Grader {
    sample_rate: f64,
    frame_size: usize,
    hop_size: usize,
    melody: Option<Vec<Note>>,

    buffer: VecDeque<f32>,
    samples_since_emit: usize,
    samples_seen: u64,
    pitch_history_cents: VecDeque<f64>,
    score_ema: f64,
    position_offset_ms: Option<f64>,
}

#[wasm_bindgen]
impl Grader {
    /// `melody_json`, if provided, is a JSON array of `{start_ms, end_ms,
    /// midi}` objects (the shape `note-guide.js`'s `getMelodyNotes()`
    /// already produces) - pass `None`/`undefined` for no-reference
    /// (stability-only) grading.
    #[wasm_bindgen(constructor)]
    pub fn new(sample_rate: f64, melody_json: Option<String>) -> Grader {
        let frame_size = ((sample_rate * FRAME_SECONDS) as usize).max(256);
        let hop_size = ((sample_rate * HOP_SECONDS) as usize).max(1);
        let melody = melody_json.and_then(|s| parse_melody(&s));

        Grader {
            sample_rate,
            frame_size,
            hop_size,
            melody,
            buffer: VecDeque::with_capacity(frame_size),
            samples_since_emit: 0,
            samples_seen: 0,
            pitch_history_cents: VecDeque::with_capacity(STABILITY_WINDOW),
            score_ema: 0.0,
            position_offset_ms: None,
        }
    }

    /// Sync: the backing track is at `pos_ms` right now. Maps the mic
    /// stream's sample clock onto song time for melody lookups; call
    /// repeatedly (the player does so every ~1s), so pause/seek/drift
    /// self-correct at the next sync.
    pub fn set_position(&mut self, pos_ms: f64) {
        if !pos_ms.is_finite() {
            return;
        }
        let stream_ms = self.samples_seen as f64 / self.sample_rate * 1000.0;
        self.position_offset_ms = Some(pos_ms - stream_ms);
    }

    /// Push a chunk of mono float32 PCM samples. Returns a JSON string
    /// encoding an array of zero or more score-update objects (each
    /// shaped like `{t_ms, singing, frequency_hz, target_midi, score}`,
    /// matching `RealtimeGrader.push_samples()`'s Python dicts).
    pub fn push_samples(&mut self, chunk: &[f32]) -> String {
        for &s in chunk {
            self.buffer.push_back(s);
        }
        while self.buffer.len() > self.frame_size {
            self.buffer.pop_front();
        }
        self.samples_since_emit += chunk.len();
        self.samples_seen += chunk.len() as u64;

        let mut updates = Vec::new();
        while self.samples_since_emit >= self.hop_size {
            self.samples_since_emit -= self.hop_size;
            updates.push(self.analyze());
        }
        serde_json::to_string(&updates).unwrap_or_else(|_| "[]".to_string())
    }

    fn analyze(&mut self) -> ScoreUpdate {
        let frame: Vec<f32> = self.buffer.iter().copied().collect();
        let rms = compute_rms(&frame);
        let stream_t_ms = self.samples_seen as f64 / self.sample_rate * 1000.0;
        let target_note = self.target_note(stream_t_ms).cloned();

        let mut result = ScoreUpdate {
            t_ms: stream_t_ms as i64,
            singing: false,
            frequency_hz: None,
            target_midi: target_note.as_ref().map(|n| n.midi),
            score: 0,
        };

        if rms < SILENCE_RMS_THRESHOLD {
            self.pitch_history_cents.clear();
            self.score_ema *= 1.0 - SCORE_SMOOTHING;
            result.score = self.score_ema.round() as i32;
            return result;
        }

        result.singing = true;
        let frequency = yin_pitch(&frame, self.sample_rate, MIN_FREQUENCY_HZ, MAX_FREQUENCY_HZ, YIN_THRESHOLD);

        let target_score = match frequency {
            None => {
                self.pitch_history_cents.clear();
                NOISE_SCORE
            }
            Some(freq) => {
                result.frequency_hz = Some((freq * 10.0).round() / 10.0);
                self.pitch_history_cents.push_back(hz_to_cents(freq, CENTS_REFERENCE_HZ));
                while self.pitch_history_cents.len() > STABILITY_WINDOW {
                    self.pitch_history_cents.pop_front();
                }
                let stability = stability_score(&self.pitch_history_cents);
                match &target_note {
                    Some(note) => {
                        let accuracy = accuracy_score(hz_to_midi(freq), note.midi);
                        ACCURACY_WEIGHT * accuracy + (1.0 - ACCURACY_WEIGHT) * stability
                    }
                    None => stability,
                }
            }
        };

        self.score_ema += (target_score - self.score_ema) * SCORE_SMOOTHING;
        result.score = self.score_ema.clamp(0.0, 100.0).round() as i32;
        result
    }

    fn target_note(&self, stream_t_ms: f64) -> Option<&Note> {
        let melody = self.melody.as_ref()?;
        let offset = self.position_offset_ms?;
        let song_pos = stream_t_ms + offset;
        note_at(melody, song_pos)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::f64::consts::PI;

    const SAMPLE_RATE: f64 = 44100.0;

    fn sine(freq: f64, seconds: f64, amp: f32) -> Vec<f32> {
        let n = (SAMPLE_RATE * seconds) as usize;
        (0..n)
            .map(|i| {
                let t = i as f64 / SAMPLE_RATE;
                (amp as f64 * (2.0 * PI * freq * t).sin()) as f32
            })
            .collect()
    }

    #[test]
    fn detects_known_frequency_within_tolerance() {
        for freq in [110.0, 220.0, 440.0, 880.0] {
            let samples = sine(freq, 0.1, 0.5);
            let detected = yin_pitch(&samples, SAMPLE_RATE, MIN_FREQUENCY_HZ, MAX_FREQUENCY_HZ, YIN_THRESHOLD);
            assert!(detected.is_some(), "expected a pitch for {freq}Hz");
            let detected = detected.unwrap();
            assert!(
                (detected - freq).abs() < freq * 0.02,
                "freq={freq} detected={detected}"
            );
        }
    }

    #[test]
    fn returns_none_for_silence() {
        let samples = vec![0.0f32; 4096];
        assert_eq!(yin_pitch(&samples, SAMPLE_RATE, MIN_FREQUENCY_HZ, MAX_FREQUENCY_HZ, YIN_THRESHOLD), None);
    }

    #[test]
    fn octave_folding_matches_floor_modulo_semantics() {
        // detected below target by more than half an octave: Python's
        // floor-modulo `%` and Rust's `rem_euclid` must agree here, unlike
        // plain `%` which would give a different (wrong) sign/magnitude.
        let cents = octave_folded_cents_off(60.0, 67.0); // 7 semitones below
        assert!((cents - 500.0).abs() < 1e-9, "cents={cents}");
    }

    #[test]
    fn population_std_matches_numpy_ddof0() {
        // np.std([1,2,3,4]) == 1.118033988749895 (population, ddof=0)
        let values = [1.0, 2.0, 3.0, 4.0];
        let std = population_std(&values);
        assert!((std - 1.118033988749895).abs() < 1e-12, "std={std}");
    }

    #[test]
    fn grader_scores_on_pitch_singing_against_melody_highly() {
        let melody = r#"[{"start_ms": 0.0, "end_ms": 5000.0, "midi": 69.0}]"#; // A4 = 440Hz
        let mut grader = Grader::new(SAMPLE_RATE, Some(melody.to_string()));
        grader.set_position(0.0);
        let samples = sine(440.0, 1.0, 0.5);
        let mut last_score = 0;
        for chunk in samples.chunks((SAMPLE_RATE * 0.02) as usize) {
            let json = grader.push_samples(chunk);
            let updates: Vec<ScoreUpdate> = serde_json::from_str(&json).unwrap();
            if let Some(u) = updates.last() {
                last_score = u.score;
            }
        }
        assert!(last_score > 80, "expected high on-pitch score, got {last_score}");
    }

    #[test]
    fn grader_scores_silence_near_zero() {
        let mut grader = Grader::new(SAMPLE_RATE, None);
        let samples = vec![0.0f32; (SAMPLE_RATE * 1.0) as usize];
        let mut last_score = 100;
        for chunk in samples.chunks((SAMPLE_RATE * 0.02) as usize) {
            let json = grader.push_samples(chunk);
            let updates: Vec<ScoreUpdate> = serde_json::from_str(&json).unwrap();
            if let Some(u) = updates.last() {
                last_score = u.score;
            }
        }
        assert_eq!(last_score, 0);
    }
}
