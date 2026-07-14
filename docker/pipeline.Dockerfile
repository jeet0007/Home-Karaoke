# Heavy pipeline container: runs ONLY worker.py (the background processing
# queue - Demucs vocal separation, Basic Pitch transcription, tempo
# estimation). No web server, no exposed port. This is the container to
# `docker compose stop pipeline` when you need the NAS's CPU back - queued
# songs just wait as `pending` until it's running again (see worker.py's
# docstring and CLAUDE.md's "Docker deployment" section).
#
# python:3.11-slim (not 3.10) is fine here: the Python-3.10-only constraint
# in requirements-ml.txt's comments is specifically an Apple Silicon/macOS
# Basic Pitch limitation. On Linux (what a NAS runs) Basic Pitch uses the
# TensorFlow Lite runtime instead, with no such version pin.

FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1

WORKDIR /app

# ffmpeg: vocal_transcribe.py's decode step. git: requirements-ml.txt
# installs demucs straight from GitHub (PyPI's release predates the
# demucs.api module this app needs). libsndfile1: soundfile's runtime dep
# (used by librosa and Basic Pitch's audio loading). build-essential: a
# fallback in case any wheel isn't available for this image's architecture
# (notably on ARM64 NAS hardware).
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg \
        git \
        libsndfile1 \
        build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt requirements-ml.txt /app/
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt \
    && pip install --no-cache-dir -r requirements-ml.txt

COPY . /app

# DEMUCS_DEVICE defaults to "auto" (core/vocal_transcribe.py picks CUDA,
# then MPS, then CPU) - a NAS has neither CUDA nor MPS, so this resolves to
# "cpu" automatically. KARAOKE_TORCH_THREADS caps CPU thread use (default:
# all cores minus one) so the worker doesn't starve anything else running on
# the NAS; override either via docker-compose.yml's environment if needed.
CMD ["python", "worker.py"]
