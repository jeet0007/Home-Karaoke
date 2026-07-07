"""On-disk store for reusable per-song artifacts.

The processing pipeline produces several expensive intermediate files that
are worth keeping rather than discarding in a tempdir (the old
vocal_transcribe behavior): the decoded mix, the separated vocal stem, the
lyrics, and the MIDI melody. Persisting them means each pipeline stage is
independently re-runnable - re-transcribe from a kept vocal stem without
re-running Demucs, regenerate the MIDI without re-separating, etc. - and the
MIDI/vocals become downloadable products in their own right.

Layout: one directory per song id under the store root (KARAOKE_DATA_DIR),
with a fixed filename per artifact kind. This module owns the FILES only;
library.py records each artifact's path in the DB so the queue/UI can find
and serve them. Kept deliberately dumb (no DB, no network) so it imports
cheaply and tests without either.
"""

import hashlib
import json
import os
import shutil

# Artifact kinds. The value is the on-disk filename within the song dir.
KIND_MIX = "mix"          # decoded full-mix audio (the "music" going in)
KIND_VOCALS = "vocals"    # Demucs-separated vocal stem (the "singer audio")
KIND_INSTRUMENTAL = "instrumental"  # mix minus vocals - the playable backing track
KIND_LYRICS = "lyrics"    # synced lyrics
KIND_MIDI = "midi"        # the melody as a Standard MIDI File
KIND_MELODY = "melody"    # melody note segments (what the player/grader read)

# All three audio artifacts are WAV, not a compressed format like FLAC,
# despite the disk cost - deliberately. vocals/instrumental are re-seeked
# up to 60x/second by the player's singer-assist resync loop (main.js's
# per-frame syncLyrics -> singer-assist.js's syncTime(), which sets
# <audio>.currentTime directly whenever the two independent audio elements
# drift). WAV's time->byte mapping is exact and instant; a compressed
# format needs the browser to decode toward a seek point, which is slower
# and less precise - fine for occasional seeks, but this player's whole
# purpose is tight sync, so repeated re-seeks on a compressed stream
# reintroduced audible drift (regression caught 2026-07-07, one release
# after trying FLAC here). Not worth revisiting without a fundamentally
# different sync mechanism (e.g. Web Audio API buffer-based mixing instead
# of two <audio> elements).
FILENAMES = {
    KIND_MIX: "mix.wav",
    KIND_VOCALS: "vocals.wav",
    KIND_INSTRUMENTAL: "instrumental.wav",
    KIND_LYRICS: "lyrics.json",
    KIND_MIDI: "melody.mid",
    KIND_MELODY: "melody.json",
}

# Repo root (this module lives in core/), so artifacts default to a `data/`
# dir at the project root rather than inside the package. Override with
# KARAOKE_DATA_DIR.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_ROOT = os.path.join(_REPO_ROOT, "data")


class ArtifactStore:
    def __init__(self, root=DEFAULT_ROOT):
        self.root = root

    def song_dir(self, song_id):
        path = os.path.join(self.root, str(song_id))
        os.makedirs(path, exist_ok=True)
        return path

    def path(self, song_id, kind):
        """Absolute path an artifact of `kind` would live at (whether or not
        it exists yet). Ensures the song dir exists so callers can write."""
        if kind not in FILENAMES:
            raise ValueError(f"unknown artifact kind: {kind!r}")
        return os.path.join(self.song_dir(song_id), FILENAMES[kind])

    def exists(self, song_id, kind):
        return os.path.isfile(self._peek_path(song_id, kind))

    def _peek_path(self, song_id, kind):
        # Like path() but without creating the dir - for existence checks.
        if kind not in FILENAMES:
            raise ValueError(f"unknown artifact kind: {kind!r}")
        return os.path.join(self.root, str(song_id), FILENAMES[kind])

    def size(self, song_id, kind):
        peek = self._peek_path(song_id, kind)
        return os.path.getsize(peek) if os.path.isfile(peek) else 0

    def content_hash(self, song_id, kind, chunk_size=1 << 20):
        """sha256 of the artifact's current on-disk bytes, or None if it
        doesn't exist. Used for stage_runs lineage (library.py) so a run's
        recorded input/output can be verified against what's actually on
        disk - not a caching/change-detection mechanism, so a plain
        streaming hash is enough."""
        peek = self._peek_path(song_id, kind)
        if not os.path.isfile(peek):
            return None
        digest = hashlib.sha256()
        with open(peek, "rb") as handle:
            for chunk in iter(lambda: handle.read(chunk_size), b""):
                digest.update(chunk)
        return digest.hexdigest()

    def write_bytes(self, song_id, kind, data):
        target = self.path(song_id, kind)
        with open(target, "wb") as handle:
            handle.write(data)
        return target

    def write_json(self, song_id, kind, obj):
        target = self.path(song_id, kind)
        with open(target, "w", encoding="utf-8") as handle:
            json.dump(obj, handle)
        return target

    def read_json(self, song_id, kind):
        with open(self._peek_path(song_id, kind), encoding="utf-8") as handle:
            return json.load(handle)

    def remove_song(self, song_id):
        """Delete every artifact for a song (its whole dir). Safe to call
        when nothing was ever written."""
        shutil.rmtree(os.path.join(self.root, str(song_id)), ignore_errors=True)
