import os
import struct
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import waveform  # noqa: E402


def _pcm(samples):
    return struct.pack(f"<{len(samples)}f", *samples)


class ComputePeaksTestCase(unittest.TestCase):
    def test_empty_input_returns_no_peaks(self):
        self.assertEqual(waveform.compute_peaks(b""), [])

    def test_bucket_count_matches_request(self):
        samples = [0.1] * 1000
        peaks = waveform.compute_peaks(_pcm(samples), num_buckets=10)

        self.assertEqual(len(peaks), 10)

    def test_bucket_count_caps_at_sample_count(self):
        samples = [0.1] * 5
        peaks = waveform.compute_peaks(_pcm(samples), num_buckets=600)

        self.assertEqual(len(peaks), 5)

    def test_picks_max_absolute_amplitude_per_bucket(self):
        # Two buckets: first has a large negative spike, second is quiet.
        samples = [0.1, -0.9, 0.2, 0.05, 0.01, 0.02]
        peaks = waveform.compute_peaks(_pcm(samples), num_buckets=2)

        self.assertAlmostEqual(peaks[0], 0.9, places=5)
        self.assertAlmostEqual(peaks[1], 0.05, places=5)

    def test_clamps_out_of_range_amplitude_to_one(self):
        samples = [1.5, 0.1]
        peaks = waveform.compute_peaks(_pcm(samples), num_buckets=1)

        self.assertEqual(peaks[0], 1.0)

    def test_all_values_within_unit_range(self):
        samples = [(-1) ** i * (i / 100.0) for i in range(300)]
        peaks = waveform.compute_peaks(_pcm(samples), num_buckets=17)

        for peak in peaks:
            self.assertGreaterEqual(peak, 0.0)
            self.assertLessEqual(peak, 1.0)


class PcmDurationTestCase(unittest.TestCase):
    def test_duration_derived_from_byte_length_and_sample_rate(self):
        pcm = _pcm([0.0] * waveform.PEAK_SAMPLE_RATE_HZ)

        self.assertAlmostEqual(waveform.pcm_duration_s(pcm), 1.0, places=5)

    def test_empty_pcm_has_zero_duration(self):
        self.assertEqual(waveform.pcm_duration_s(b""), 0.0)


class FfmpegAvailableTestCase(unittest.TestCase):
    @patch("waveform.shutil.which", return_value="/usr/bin/ffmpeg")
    def test_true_when_found_on_path(self, _mock_which):
        self.assertTrue(waveform.ffmpeg_available())

    @patch("waveform.shutil.which", return_value=None)
    def test_false_when_missing_from_path(self, _mock_which):
        self.assertFalse(waveform.ffmpeg_available())


class DecodePcmFromUrlTestCase(unittest.TestCase):
    @patch("waveform.shutil.which", return_value=None)
    def test_raises_when_ffmpeg_missing(self, _mock_which):
        with self.assertRaisesRegex(RuntimeError, "ffmpeg not found"):
            waveform.decode_pcm_from_url("https://example.com/audio")

    @patch("waveform.subprocess.run")
    @patch("waveform.shutil.which", return_value="/usr/bin/ffmpeg")
    def test_raises_with_stderr_on_nonzero_exit(self, _mock_which, mock_run):
        mock_run.return_value.returncode = 1
        mock_run.return_value.stderr = b"Invalid data found when processing input"

        with self.assertRaisesRegex(RuntimeError, "Invalid data found"):
            waveform.decode_pcm_from_url("https://example.com/audio")

    @patch("waveform.subprocess.run")
    @patch("waveform.shutil.which", return_value="/usr/bin/ffmpeg")
    def test_returns_stdout_bytes_on_success(self, _mock_which, mock_run):
        mock_run.return_value.returncode = 0
        mock_run.return_value.stdout = b"\x00\x01\x02\x03"

        result = waveform.decode_pcm_from_url("https://example.com/audio")

        self.assertEqual(result, b"\x00\x01\x02\x03")


if __name__ == "__main__":
    unittest.main()
