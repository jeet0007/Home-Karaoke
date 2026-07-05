import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import vocal_transcribe as vt  # noqa: E402


# Basic Pitch note events are (start_s, end_s, pitch_midi, amplitude, pitch_bends).
def _event(start_s, end_s, pitch, amplitude=0.8):
    return (start_s, end_s, pitch, amplitude, [])


class NoteEventsToSegmentsTestCase(unittest.TestCase):
    def test_converts_seconds_to_ms_segments(self):
        events = [_event(0.5, 1.25, 60), _event(1.30, 2.00, 62)]
        segments = vt.note_events_to_segments(events)
        self.assertEqual(
            segments,
            [
                {"start_ms": 500, "end_ms": 1250, "midi": 60},
                {"start_ms": 1300, "end_ms": 2000, "midi": 62},
            ],
        )

    def test_amplitude_is_dropped_from_output(self):
        segments = vt.note_events_to_segments([_event(0, 0.5, 60, amplitude=0.9)])
        self.assertNotIn("amplitude", segments[0])

    def test_degenerate_and_short_events_dropped(self):
        events = [
            _event(1.0, 1.0, 60),  # zero-length
            _event(2.0, 1.5, 61),  # inverted
            _event(3.0, 3.05, 62),  # 50ms - below MIN_NOTE_MS
            _event(4.0, 4.5, 63),  # keeper
        ]
        segments = vt.note_events_to_segments(events)
        self.assertEqual([s["midi"] for s in segments], [63])

    def test_pitch_and_time_are_rounded(self):
        segments = vt.note_events_to_segments([_event(0.5004, 1.4996, 60.4)])
        self.assertEqual(segments[0], {"start_ms": 500, "end_ms": 1500, "midi": 60})

    def test_empty_input(self):
        self.assertEqual(vt.note_events_to_segments([]), [])


class ReduceToMonophonicTestCase(unittest.TestCase):
    def _seg(self, start_ms, end_ms, midi, amplitude):
        return {"start_ms": start_ms, "end_ms": end_ms, "midi": midi, "amplitude": amplitude}

    def test_non_overlapping_notes_pass_through(self):
        segs = [self._seg(0, 500, 60, 0.8), self._seg(600, 1000, 62, 0.7)]
        result = vt.reduce_to_monophonic(segs)
        self.assertEqual([s["midi"] for s in result], [60, 62])

    def test_louder_note_wins_overlap_and_trims_earlier(self):
        # A quiet long note overlapped by a louder note starting mid-way:
        # the quiet one is shortened to where the loud one begins.
        segs = [self._seg(0, 1000, 60, 0.4), self._seg(400, 1200, 67, 0.9)]
        result = vt.reduce_to_monophonic(segs)
        result.sort(key=lambda s: s["start_ms"])
        self.assertEqual(len(result), 2)
        self.assertEqual(result[0]["midi"], 60)
        self.assertEqual(result[0]["end_ms"], 400)  # trimmed to the loud note's start
        self.assertEqual(result[1]["midi"], 67)

    def test_quieter_overlapping_note_is_dropped_when_fully_covered(self):
        segs = [self._seg(0, 1000, 67, 0.9), self._seg(200, 800, 60, 0.3)]
        result = vt.reduce_to_monophonic(segs)
        self.assertEqual([s["midi"] for s in result], [67])

    def test_reduction_drops_sub_min_note_fragments(self):
        # The quieter note only pokes out 50ms past the louder one - the
        # trimmed remainder is below MIN_NOTE_MS and dropped.
        segs = [self._seg(0, 1000, 67, 0.9), self._seg(950, 1050, 60, 0.3)]
        result = vt.reduce_to_monophonic(segs)
        self.assertEqual([s["midi"] for s in result], [67])

    def test_end_to_end_conversion_is_monophonic(self):
        events = [_event(0.0, 1.0, 67, 0.9), _event(0.2, 0.8, 60, 0.3)]  # harmony under a lead note
        segments = vt.note_events_to_segments(events)
        self.assertEqual([s["midi"] for s in segments], [67])


class AvailabilityTestCase(unittest.TestCase):
    def setUp(self):
        vt._availability = None
        self.addCleanup(setattr, vt, "_availability", None)

    def test_unavailable_when_deps_missing(self):
        with mock.patch.object(vt, "_probe_dependencies", return_value=False):
            self.assertFalse(vt.available())

    def test_unavailable_when_ffmpeg_missing(self):
        with mock.patch.object(vt, "_probe_dependencies", return_value=True), mock.patch.object(
            vt.shutil, "which", return_value=None
        ):
            self.assertFalse(vt.available())

    def test_available_when_deps_and_ffmpeg_present(self):
        with mock.patch.object(vt, "_probe_dependencies", return_value=True), mock.patch.object(
            vt.shutil, "which", return_value="/usr/bin/ffmpeg"
        ):
            self.assertTrue(vt.available())

    def test_result_is_cached(self):
        with mock.patch.object(vt, "_probe_dependencies", return_value=True) as probe, mock.patch.object(
            vt.shutil, "which", return_value="/usr/bin/ffmpeg"
        ):
            vt.available()
            vt.available()
            probe.assert_called_once()


if __name__ == "__main__":
    unittest.main()
