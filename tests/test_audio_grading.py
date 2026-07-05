import os
import sys
import unittest

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import audio_grading as ag  # noqa: E402

SAMPLE_RATE = 44100


def _sine(freq, seconds, sample_rate=SAMPLE_RATE, amp=0.5):
    t = np.arange(int(sample_rate * seconds)) / sample_rate
    return (amp * np.sin(2 * np.pi * freq * t)).astype(np.float32)


def _feed_in_chunks(grader, samples, chunk_ms=20, sample_rate=SAMPLE_RATE):
    chunk_n = max(1, int(sample_rate * chunk_ms / 1000))
    updates = []
    for start in range(0, len(samples), chunk_n):
        chunk = samples[start : start + chunk_n]
        updates.extend(grader.push_samples(chunk))
    return updates


class YinPitchTestCase(unittest.TestCase):
    def test_detects_known_frequency_within_tolerance(self):
        for freq in (110.0, 220.0, 440.0, 880.0):
            with self.subTest(freq=freq):
                samples = _sine(freq, seconds=0.1)
                detected = ag.yin_pitch(samples, SAMPLE_RATE)
                self.assertIsNotNone(detected)
                self.assertAlmostEqual(detected, freq, delta=freq * 0.02)

    def test_returns_none_for_silence(self):
        samples = np.zeros(4096, dtype=np.float32)
        self.assertIsNone(ag.yin_pitch(samples, SAMPLE_RATE))

    def test_returns_none_for_white_noise(self):
        rng = np.random.default_rng(0)
        samples = rng.normal(0, 0.3, 4096).astype(np.float32)
        self.assertIsNone(ag.yin_pitch(samples, SAMPLE_RATE))

    def test_returns_none_below_fmin(self):
        # 40 Hz is below MIN_FREQUENCY_HZ (70 Hz) - out of the vocal range
        # this scorer targets.
        samples = _sine(40.0, seconds=0.2)
        self.assertIsNone(ag.yin_pitch(samples, SAMPLE_RATE))


class RealtimeGraderTestCase(unittest.TestCase):
    def test_rejects_non_positive_sample_rate(self):
        with self.assertRaises(ValueError):
            ag.RealtimeGrader(0)

    def test_steady_tone_scores_high_and_reports_frequency(self):
        grader = ag.RealtimeGrader(SAMPLE_RATE)
        samples = _sine(220.0, seconds=2.0)
        updates = _feed_in_chunks(grader, samples)

        self.assertTrue(updates)
        for update in updates:
            self.assertTrue(update["singing"])

        detected_freqs = [u["frequency_hz"] for u in updates if u["frequency_hz"]]
        self.assertTrue(detected_freqs)
        for freq in detected_freqs:
            self.assertAlmostEqual(freq, 220.0, delta=5.0)

        # Score should climb as pitch-history accumulates and settle high
        # for a rock-steady tone.
        self.assertGreaterEqual(updates[-1]["score"], 85)

    def test_silence_scores_zero_and_reports_not_singing(self):
        grader = ag.RealtimeGrader(SAMPLE_RATE)
        rng = np.random.default_rng(1)
        # A tiny mic self-noise floor, not true digital zero - realistic
        # "silence" still has some noise floor.
        samples = rng.normal(0, 0.0005, int(SAMPLE_RATE * 1.5)).astype(np.float32)
        updates = _feed_in_chunks(grader, samples)

        self.assertTrue(updates)
        for update in updates:
            self.assertFalse(update["singing"])
            self.assertIsNone(update["frequency_hz"])
            self.assertEqual(update["score"], 0)

    def test_white_noise_scores_low_but_above_silence(self):
        grader = ag.RealtimeGrader(SAMPLE_RATE)
        rng = np.random.default_rng(2)
        samples = rng.normal(0, 0.2, int(SAMPLE_RATE * 2.0)).astype(np.float32)
        updates = _feed_in_chunks(grader, samples)

        self.assertTrue(updates)
        for update in updates:
            self.assertTrue(update["singing"])

        self.assertLess(updates[-1]["score"], 20)
        self.assertGreater(updates[-1]["score"], 0)

    def test_steady_tone_outscores_drifting_pitch(self):
        """A held note should end up scored noticeably higher than one that
        slides around, even though both have clear energy and are
        periodic/pitched moment-to-moment."""
        steady_grader = ag.RealtimeGrader(SAMPLE_RATE)
        steady_updates = _feed_in_chunks(steady_grader, _sine(220.0, seconds=2.5))

        drift_grader = ag.RealtimeGrader(SAMPLE_RATE)
        t = np.arange(int(SAMPLE_RATE * 2.5)) / SAMPLE_RATE
        freq = 220 + 20 * np.sin(2 * np.pi * 0.4 * t)
        phase = 2 * np.pi * np.cumsum(freq) / SAMPLE_RATE
        drifting_samples = (0.5 * np.sin(phase)).astype(np.float32)
        drift_updates = _feed_in_chunks(drift_grader, drifting_samples)

        self.assertGreater(steady_updates[-1]["score"], drift_updates[-1]["score"] + 20)

    def test_drifting_pitch_outscores_pure_noise(self):
        t = np.arange(int(SAMPLE_RATE * 2.5)) / SAMPLE_RATE
        freq = 220 + 20 * np.sin(2 * np.pi * 0.4 * t)
        phase = 2 * np.pi * np.cumsum(freq) / SAMPLE_RATE
        drifting_samples = (0.5 * np.sin(phase)).astype(np.float32)
        drift_grader = ag.RealtimeGrader(SAMPLE_RATE)
        drift_updates = _feed_in_chunks(drift_grader, drifting_samples)

        rng = np.random.default_rng(3)
        noise_samples = rng.normal(0, 0.2, int(SAMPLE_RATE * 2.5)).astype(np.float32)
        noise_grader = ag.RealtimeGrader(SAMPLE_RATE)
        noise_updates = _feed_in_chunks(noise_grader, noise_samples)

        self.assertGreater(drift_updates[-1]["score"], noise_updates[-1]["score"])

    def test_updates_are_throttled_to_roughly_hop_seconds(self):
        grader = ag.RealtimeGrader(SAMPLE_RATE)
        samples = _sine(220.0, seconds=1.0)
        updates = _feed_in_chunks(grader, samples)

        expected_count = int(1.0 / ag.HOP_SECONDS)
        self.assertIn(len(updates), (expected_count - 1, expected_count, expected_count + 1))

    def test_transition_from_singing_to_silence_decays_score_toward_zero(self):
        grader = ag.RealtimeGrader(SAMPLE_RATE)
        _feed_in_chunks(grader, _sine(220.0, seconds=2.0))

        rng = np.random.default_rng(4)
        silence = rng.normal(0, 0.0005, int(SAMPLE_RATE * 2.0)).astype(np.float32)
        silence_updates = _feed_in_chunks(grader, silence)

        self.assertFalse(silence_updates[-1]["singing"])
        # EMA decay is asymptotic, not instant - after 2s of silence it
        # should be at or near zero, not exactly reset.
        self.assertLessEqual(silence_updates[-1]["score"], 1)
        # Should decay monotonically toward zero, not snap instantly.
        scores = [u["score"] for u in silence_updates]
        self.assertEqual(scores, sorted(scores, reverse=True))


class ParseMelodyTestCase(unittest.TestCase):
    def test_valid_notes_sorted(self):
        raw = [
            {"start_ms": 1000, "end_ms": 2000, "midi": 60},
            {"start_ms": 0, "end_ms": 900, "midi": 57},
        ]
        notes = ag.parse_melody(raw)
        self.assertEqual([n["midi"] for n in notes], [57.0, 60.0])

    def test_garbage_entries_dropped(self):
        raw = [
            "not a dict",
            {"start_ms": "x", "end_ms": 100, "midi": 60},
            {"start_ms": 500, "end_ms": 100, "midi": 60},  # inverted
            {"start_ms": -5, "end_ms": 100, "midi": 60},  # negative start
            {"start_ms": 0, "end_ms": 100, "midi": float("nan")},
            {"start_ms": 0, "end_ms": 100, "midi": 60},  # the one keeper
        ]
        notes = ag.parse_melody(raw)
        self.assertEqual(len(notes), 1)

    def test_non_list_or_empty_returns_none(self):
        self.assertIsNone(ag.parse_melody(None))
        self.assertIsNone(ag.parse_melody({"start_ms": 0}))
        self.assertIsNone(ag.parse_melody([]))
        self.assertIsNone(ag.parse_melody(["junk"]))


class OctaveFoldingTestCase(unittest.TestCase):
    def test_exact_and_octave_matches_are_zero(self):
        self.assertAlmostEqual(ag._octave_folded_cents_off(69, 69), 0.0)
        self.assertAlmostEqual(ag._octave_folded_cents_off(57, 69), 0.0)
        self.assertAlmostEqual(ag._octave_folded_cents_off(81, 69), 0.0)

    def test_semitone_off_is_100_cents_either_direction(self):
        self.assertAlmostEqual(ag._octave_folded_cents_off(70, 69), 100.0)
        self.assertAlmostEqual(ag._octave_folded_cents_off(68, 69), 100.0)
        # ...even across an octave.
        self.assertAlmostEqual(ag._octave_folded_cents_off(58, 69), 100.0)

    def test_tritone_is_the_worst_case(self):
        self.assertAlmostEqual(ag._octave_folded_cents_off(63, 69), 600.0)


MELODY_A3 = [{"start_ms": 0.0, "end_ms": 60000.0, "midi": 57.0}]


def _reference_grader(melody=MELODY_A3):
    grader = ag.RealtimeGrader(SAMPLE_RATE, melody=melody)
    grader.set_position(0)
    return grader


class ReferenceMelodyGradingTestCase(unittest.TestCase):
    def test_on_pitch_singing_scores_high(self):
        grader = _reference_grader()
        updates = _feed_in_chunks(grader, _sine(220.0, seconds=2.5))  # A3, dead-on
        self.assertGreaterEqual(updates[-1]["score"], 85)
        self.assertEqual(updates[-1]["target_midi"], 57.0)

    def test_octave_up_counts_as_on_pitch(self):
        grader = _reference_grader()
        updates = _feed_in_chunks(grader, _sine(440.0, seconds=2.5))  # A4 vs A3 target
        self.assertGreaterEqual(updates[-1]["score"], 85)

    def test_wrong_note_scores_much_lower_than_right_note(self):
        right = _feed_in_chunks(_reference_grader(), _sine(220.0, seconds=2.5))
        # E4 (330 Hz) is a tritone-ish 7 semitones up from A3 -> folded 500c off.
        wrong = _feed_in_chunks(_reference_grader(), _sine(311.1, seconds=2.5))  # D#4: folded 600c
        self.assertGreater(right[-1]["score"], wrong[-1]["score"] + 40)

    def test_no_position_sync_falls_back_to_stability_grading(self):
        grader = ag.RealtimeGrader(SAMPLE_RATE, melody=MELODY_A3)  # no set_position
        updates = _feed_in_chunks(grader, _sine(311.1, seconds=2.5))  # "wrong" note
        # Without a sync there is no song position, so the wrong-but-steady
        # note grades on stability alone and still scores well.
        self.assertGreaterEqual(updates[-1]["score"], 85)
        self.assertIsNone(updates[-1]["target_midi"])

    def test_instrumental_gap_falls_back_to_stability(self):
        melody = [{"start_ms": 30000.0, "end_ms": 60000.0, "midi": 57.0}]
        grader = _reference_grader(melody)  # position 0: no active note
        updates = _feed_in_chunks(grader, _sine(311.1, seconds=2.5))
        self.assertGreaterEqual(updates[-1]["score"], 85)
        self.assertIsNone(updates[-1]["target_midi"])

    def test_set_position_maps_song_time(self):
        melody = [{"start_ms": 30000.0, "end_ms": 60000.0, "midi": 57.0}]
        grader = ag.RealtimeGrader(SAMPLE_RATE, melody=melody)
        grader.set_position(30500)  # jump into the note despite mic t=0
        updates = _feed_in_chunks(grader, _sine(220.0, seconds=2.0))
        self.assertEqual(updates[-1]["target_midi"], 57.0)
        self.assertGreaterEqual(updates[-1]["score"], 85)

    def test_non_finite_position_is_ignored(self):
        grader = ag.RealtimeGrader(SAMPLE_RATE, melody=MELODY_A3)
        grader.set_position(float("nan"))
        updates = _feed_in_chunks(grader, _sine(220.0, seconds=1.0))
        self.assertIsNone(updates[-1]["target_midi"])


if __name__ == "__main__":
    unittest.main()
