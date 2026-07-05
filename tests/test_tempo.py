import os
import sys
import types
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import tempo  # noqa: E402


class AvailabilityTestCase(unittest.TestCase):
    def setUp(self):
        tempo._availability = None
        self.addCleanup(setattr, tempo, "_availability", None)

    def test_unavailable_when_probe_fails(self):
        with mock.patch.object(tempo, "_probe_dependencies", return_value=False):
            self.assertFalse(tempo.available())

    def test_available_and_cached(self):
        with mock.patch.object(tempo, "_probe_dependencies", return_value=True) as probe:
            self.assertTrue(tempo.available())
            self.assertTrue(tempo.available())
            probe.assert_called_once()


def _fake_librosa(tempo_value):
    """A stand-in librosa module: load() returns dummy audio, beat_track
    returns (tempo_value, beat_frames). tempo_value may be a scalar or an
    array (numpy import happens for real, only librosa is faked)."""
    lib = types.ModuleType("librosa")
    lib.load = lambda path, mono=True: ([0.0, 0.1, 0.2], 22050)
    beat = types.ModuleType("librosa.beat")
    beat.beat_track = lambda y, sr: (tempo_value, [0, 1, 2])
    lib.beat = beat
    return lib


class EstimateBpmTestCase(unittest.TestCase):
    def _run(self, tempo_value):
        import numpy as np

        with mock.patch.dict(sys.modules, {"librosa": _fake_librosa(tempo_value), "librosa.beat": None}):
            # numpy is imported for real inside estimate_bpm; ensure it's present.
            assert np is not None
            return tempo.estimate_bpm("/tmp/whatever.wav")

    def test_scalar_tempo(self):
        self.assertEqual(self._run(128.4), 128.4)

    def test_array_tempo_takes_first(self):
        import numpy as np

        self.assertEqual(self._run(np.array([120.0])), 120.0)

    def test_rounds_to_one_decimal(self):
        self.assertEqual(self._run(119.96), 120.0)

    def test_non_positive_returns_none(self):
        self.assertIsNone(self._run(0.0))

    def test_non_finite_returns_none(self):
        import numpy as np

        self.assertIsNone(self._run(np.array([float("nan")])))

    def test_empty_array_returns_none(self):
        import numpy as np

        self.assertIsNone(self._run(np.array([])))


if __name__ == "__main__":
    unittest.main()
