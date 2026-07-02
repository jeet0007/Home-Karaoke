"""Coarse waveform peak generation for the player UI's visual waveform.

Scope: visualization only - a bucketed max-amplitude envelope for a canvas
progress bar, not audio analysis (see audio_grading.py for the project's
existing "no heavy audio processing, use existing tools" stance, which this
follows: numpy for the math, the `ffmpeg` binary - already a common host
dependency - for decoding, no ML/audio-analysis libraries).

/stream-proxy plays a progressive video+audio format chosen for browser
compatibility, which is several times larger than an audio-only stream (a
sample video: ~11MB progressive vs. ~3MB bestaudio-only) and mostly wasted
bytes for a waveform. So this resolves its own audio-only stream URL via
yt-dlp (see app.py's _resolve_stream_urls) and decodes just that.
"""

import shutil
import subprocess

import numpy as np

FFMPEG_BINARY = "ffmpeg"
PEAK_SAMPLE_RATE_HZ = 8000
DEFAULT_PEAK_COUNT = 600
DECODE_TIMEOUT_S = 45


def ffmpeg_available():
    return shutil.which(FFMPEG_BINARY) is not None


def decode_pcm_from_url(audio_url, timeout=DECODE_TIMEOUT_S):
    """Decode `audio_url` to raw little-endian float32 mono PCM via ffmpeg,
    returning the raw bytes. ffmpeg reads the audio-only stream directly over
    HTTP - no video bytes are ever pulled, no file touches disk."""
    ffmpeg_path = shutil.which(FFMPEG_BINARY)
    if not ffmpeg_path:
        raise RuntimeError("ffmpeg not found on PATH")

    cmd = [
        ffmpeg_path,
        "-v",
        "error",
        "-i",
        audio_url,
        "-vn",
        "-ac",
        "1",
        "-ar",
        str(PEAK_SAMPLE_RATE_HZ),
        "-f",
        "f32le",
        "-",
    ]
    proc = subprocess.run(cmd, capture_output=True, timeout=timeout)
    if proc.returncode != 0:
        stderr = proc.stderr.decode("utf-8", "replace").strip() or "unknown ffmpeg error"
        raise RuntimeError(f"ffmpeg failed: {stderr}")

    return proc.stdout


def pcm_duration_s(pcm_bytes, sample_rate=PEAK_SAMPLE_RATE_HZ):
    return (len(pcm_bytes) / 4) / sample_rate


def compute_peaks(pcm_bytes, num_buckets=DEFAULT_PEAK_COUNT):
    """Downsample raw little-endian float32 mono PCM into `num_buckets` peak
    values (max absolute amplitude per bucket, each clamped to [0, 1])."""
    samples = np.frombuffer(pcm_bytes, dtype="<f4")
    if samples.size == 0:
        return []

    num_buckets = min(num_buckets, samples.size)
    bucket_edges = np.linspace(0, samples.size, num_buckets + 1).astype(np.int64)
    abs_samples = np.abs(samples)

    peaks = []
    for i in range(num_buckets):
        start, end = bucket_edges[i], bucket_edges[i + 1]
        peaks.append(float(min(abs_samples[start:end].max(), 1.0)))

    return peaks
