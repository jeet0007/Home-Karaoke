"""SQLite song library + background processing queue.

The library holds "good to go" songs: identities whose synced lyrics and
best karaoke backing video have already been resolved, plus the optional
extras (reference melody, MIDI) computed once and stored. A song
served from the library skips every network lookup /select-song would
otherwise do live — on a low-power NAS that's the difference between
instant playback and a 10+ second wait.

The processing queue IS the songs table: a song's `status` walks
pending → processing → ready (or failed), claimed atomically by the single
background worker thread. Using SQLite rows as the queue (rather than an
in-memory queue.Queue) means enqueued work survives restarts, failures
stay visible with their error message, and the web UI can show progress by
just listing rows — matching the project's small-footprint stance: one
extra thread, one .db file, no broker.

This module owns the queue, the DB, and artifact bookkeeping only. The
actual per-song work - the "music -> MIDI" core - lives in pipeline.py and
is injected into the worker as a `process_song` callable, so the queue
mechanics here stay testable without network/ffmpeg/torch. Reusable output
files (vocal stem, MIDI, etc.) are written by artifacts.ArtifactStore; their
paths are recorded in the `artifacts` table here so the UI can find and
serve them.
"""

import contextlib
import json
import os
import sqlite3
import threading
import time

STATUS_PENDING = "pending"
STATUS_PROCESSING = "processing"
STATUS_READY = "ready"
STATUS_FAILED = "failed"

# Repo root (this module lives in core/), so the DB defaults to the project
# root rather than inside the package directory. Override with LIBRARY_DB.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_DB_PATH = os.path.join(_REPO_ROOT, "library.db")

# How long the worker sleeps between queue polls when idle. Enqueues also
# wake it immediately via an Event, so this only bounds pickup latency for
# work enqueued by OTHER processes sharing the .db file.
WORKER_POLL_SECONDS = 5.0

# A song stuck in `processing` longer than this was orphaned by a crash /
# restart mid-pipeline (the worker is single-threaded, so nothing legitimate
# runs this long) - re-claim it as pending on the next poll.
STALE_PROCESSING_SECONDS = 15 * 60

_SCHEMA = """
CREATE TABLE IF NOT EXISTS songs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    artist TEXT NOT NULL,
    title TEXT NOT NULL,
    album TEXT,
    duration_seconds INTEGER,
    cover_art TEXT NOT NULL DEFAULT '',
    ytmusic_video_id TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    error TEXT,
    video_id TEXT,
    lyrics_json TEXT,
    melody_json TEXT,
    report_json TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    UNIQUE(artist COLLATE NOCASE, title COLLATE NOCASE)
);

CREATE TABLE IF NOT EXISTS artifacts (
    song_id INTEGER NOT NULL,
    kind TEXT NOT NULL,
    path TEXT NOT NULL,
    bytes INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL,
    PRIMARY KEY (song_id, kind),
    FOREIGN KEY (song_id) REFERENCES songs(id)
);
"""

_SUMMARY_FIELDS = (
    "id",
    "artist",
    "title",
    "album",
    "duration_seconds",
    "cover_art",
    "status",
    "error",
    "video_id",
    "created_at",
    "updated_at",
)


class ProcessingError(Exception):
    """A song can't become ready (no synced lyrics, no backing track, ...).
    The message is stored on the row for the UI; deliberately distinct from
    unexpected crashes, which are stored with a generic prefix instead.

    `report` carries the partial per-stage processing report built so far, so
    a failed song still shows which stages passed before the failing one."""

    def __init__(self, message, report=None):
        super().__init__(message)
        self.report = report


def _json_or_none(text):
    if not text:
        return None
    try:
        return json.loads(text)
    except ValueError:
        return None


class SongLibrary:
    """All access to the library database. Connections are per-call (SQLite
    connections aren't shareable across threads by default, and both Flask
    request threads and the worker thread call in here); WAL mode keeps the
    worker's writes from blocking reads."""

    def __init__(self, db_path=DEFAULT_DB_PATH):
        self.db_path = db_path
        # Set when work is enqueued so the worker can skip its poll sleep.
        self.work_available = threading.Event()
        with self._db() as conn:
            conn.executescript(_SCHEMA)
            # Migration for DBs created before a column existed (CREATE TABLE
            # IF NOT EXISTS never alters an existing table). Keeps an older
            # library.db usable across upgrades instead of silently missing
            # the new column.
            self._ensure_column(conn, "songs", "report_json", "TEXT")

    @staticmethod
    def _ensure_column(conn, table, column, decl):
        existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
        if column not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")

    @contextlib.contextmanager
    def _db(self):
        """A per-call connection: commit on success, roll back on error,
        always close (sqlite3's own `with conn:` commits but never closes)."""
        conn = sqlite3.connect(self.db_path, timeout=10.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    # -- enqueue / lookup ------------------------------------------------

    def enqueue(self, artist, title, album=None, duration_seconds=None, cover_art="", ytmusic_video_id=None):
        """Add a song to the processing queue, or return the existing row
        for this identity. A previously-failed song is reset to pending
        (sources change: lyrics get uploaded, videos appear), but ready and
        in-flight songs are returned as-is rather than reprocessed."""
        artist = (artist or "").strip()
        title = (title or "").strip()
        if not artist or not title:
            raise ValueError("artist and title are required")

        now = time.time()
        # The post-write re-read (self.get) opens its own connection, so it
        # must happen after the transaction commits - not inside the block.
        with self._db() as conn:
            existing = conn.execute(
                "SELECT * FROM songs WHERE artist = ? COLLATE NOCASE AND title = ? COLLATE NOCASE",
                (artist, title),
            ).fetchone()
            if existing is not None and existing["status"] != STATUS_FAILED:
                return self.summary(existing)

            if existing is not None:
                conn.execute(
                    "UPDATE songs SET status = ?, error = NULL, updated_at = ? WHERE id = ?",
                    (STATUS_PENDING, now, existing["id"]),
                )
                song_id = existing["id"]
            else:
                cursor = conn.execute(
                    "INSERT INTO songs (artist, title, album, duration_seconds, cover_art, ytmusic_video_id,"
                    " status, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (artist, title, album, duration_seconds, cover_art or "", ytmusic_video_id, STATUS_PENDING, now, now),
                )
                song_id = cursor.lastrowid

        self.work_available.set()
        return self.get(song_id)

    def get(self, song_id):
        with self._db() as conn:
            row = conn.execute("SELECT * FROM songs WHERE id = ?", (song_id,)).fetchone()
        return self.summary(row) if row is not None else None

    def find_ready(self, artist, title):
        """The full stored payload for a ready song with this identity, or
        None - the /select-song fast path."""
        with self._db() as conn:
            row = conn.execute(
                "SELECT * FROM songs WHERE artist = ? COLLATE NOCASE AND title = ? COLLATE NOCASE AND status = ?",
                (artist, title, STATUS_READY),
            ).fetchone()
        return self.full_payload(row) if row is not None else None

    def get_full(self, song_id):
        with self._db() as conn:
            row = conn.execute("SELECT * FROM songs WHERE id = ?", (song_id,)).fetchone()
        if row is None:
            return None
        payload = self.full_payload(row)
        payload["artifacts"] = self.list_artifacts(song_id)
        return payload

    def list_songs(self, status=None, limit=200):
        query = "SELECT * FROM songs"
        params = []
        if status:
            query += " WHERE status = ?"
            params.append(status)
        query += " ORDER BY updated_at DESC LIMIT ?"
        params.append(limit)
        with self._db() as conn:
            rows = conn.execute(query, params).fetchall()
        return [self.summary(row) for row in rows]

    # -- queue mechanics ---------------------------------------------------

    def claim_next_pending(self):
        """Atomically claim the oldest pending song for processing and
        return its row as a dict, or None when the queue is empty. Also
        rescues songs orphaned in `processing` by a crash (see
        STALE_PROCESSING_SECONDS)."""
        now = time.time()
        with self._db() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "UPDATE songs SET status = ?, updated_at = ? WHERE status = ? AND updated_at < ?",
                (STATUS_PENDING, now, STATUS_PROCESSING, now - STALE_PROCESSING_SECONDS),
            )
            row = conn.execute(
                "SELECT * FROM songs WHERE status = ? ORDER BY id LIMIT 1", (STATUS_PENDING,)
            ).fetchone()
            if row is None:
                conn.execute("COMMIT")
                return None
            conn.execute(
                "UPDATE songs SET status = ?, updated_at = ? WHERE id = ?",
                (STATUS_PROCESSING, now, row["id"]),
            )
            conn.execute("COMMIT")
        return dict(row)

    def mark_ready(
        self,
        song_id,
        video_id,
        lyrics,
        melody=None,
        duration_seconds=None,
        cover_art=None,
        artifacts=None,
        report=None,
    ):
        sets = [
            "status = ?",
            "error = NULL",
            "video_id = ?",
            "lyrics_json = ?",
            "melody_json = ?",
            "report_json = ?",
            "updated_at = ?",
        ]
        params = [
            STATUS_READY,
            video_id,
            json.dumps(lyrics),
            json.dumps(melody) if melody is not None else None,
            json.dumps(report) if report else None,
            time.time(),
        ]
        if duration_seconds is not None:
            sets.append("duration_seconds = ?")
            params.append(duration_seconds)
        if cover_art:
            sets.append("cover_art = ?")
            params.append(cover_art)
        params.append(song_id)
        now = time.time()
        with self._db() as conn:
            conn.execute(f"UPDATE songs SET {', '.join(sets)} WHERE id = ?", params)
            for artifact in artifacts or []:
                conn.execute(
                    "INSERT OR REPLACE INTO artifacts (song_id, kind, path, bytes, created_at)"
                    " VALUES (?, ?, ?, ?, ?)",
                    (song_id, artifact["kind"], artifact["path"], artifact.get("bytes", 0), now),
                )

    def list_artifacts(self, song_id):
        with self._db() as conn:
            rows = conn.execute(
                "SELECT kind, path, bytes, created_at FROM artifacts WHERE song_id = ? ORDER BY kind",
                (song_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_artifact(self, song_id, kind):
        """The recorded artifact of `kind` for a song (path + size), or None.
        Used by the presentation layer to serve/download a stored file."""
        with self._db() as conn:
            row = conn.execute(
                "SELECT kind, path, bytes, created_at FROM artifacts WHERE song_id = ? AND kind = ?",
                (song_id, kind),
            ).fetchone()
        return dict(row) if row is not None else None

    def mark_failed(self, song_id, error, report=None):
        with self._db() as conn:
            conn.execute(
                "UPDATE songs SET status = ?, error = ?, report_json = ?, updated_at = ? WHERE id = ?",
                (STATUS_FAILED, str(error)[:500], json.dumps(report) if report else None, time.time(), song_id),
            )

    # -- row shaping -------------------------------------------------------

    @staticmethod
    def summary(row):
        """The lightweight shape for lists/status polling: no lyrics/melody
        blobs, just identity + queue state + a compact per-stage status map
        (stage -> "ok"/"reused"/"skipped"/"failed") so a list view can show
        at a glance what passed and what didn't."""
        summary = {field: row[field] for field in _SUMMARY_FIELDS}
        summary["has_melody"] = bool(row["melody_json"])
        report = _json_or_none(row["report_json"]) or {}
        summary["stages"] = {stage: entry.get("status") for stage, entry in report.items()}
        return summary

    @staticmethod
    def full_payload(row):
        payload = SongLibrary.summary(row)
        payload["lyrics"] = _json_or_none(row["lyrics_json"])
        payload["melody"] = _json_or_none(row["melody_json"])
        # The full per-stage report: each stage's status plus a human-readable
        # "detail" explaining what passed, was skipped, or failed and why.
        payload["report"] = _json_or_none(row["report_json"]) or {}
        return payload


class LibraryWorker(threading.Thread):
    """Single background thread draining the pending queue. One worker on
    purpose: each song's pipeline already fans out yt-dlp/ffmpeg
    subprocesses and network calls, and the target host is a small NAS -
    parallel songs would compete for the same scarce CPU/bandwidth."""

    def __init__(self, library, process_song, poll_seconds=WORKER_POLL_SECONDS):
        super().__init__(name="library-worker", daemon=True)
        self.library = library
        self.process_song = process_song
        self.poll_seconds = poll_seconds
        self._stop_requested = threading.Event()

    def stop(self):
        self._stop_requested.set()
        self.library.work_available.set()

    def run(self):
        while not self._stop_requested.is_set():
            song = self.library.claim_next_pending()
            if song is None:
                self.library.work_available.wait(timeout=self.poll_seconds)
                self.library.work_available.clear()
                continue
            self._process_one(song)

    def _process_one(self, song):
        try:
            result = self.process_song(song)
        except ProcessingError as exc:
            self.library.mark_failed(song["id"], str(exc), report=getattr(exc, "report", None))
            return
        except Exception as exc:  # pipeline bug/infra failure - keep the worker alive
            self.library.mark_failed(song["id"], f"unexpected error: {exc}")
            return

        self.library.mark_ready(
            song["id"],
            video_id=result["video_id"],
            lyrics=result["lyrics"],
            melody=result.get("melody"),
            duration_seconds=result.get("duration_seconds"),
            cover_art=result.get("cover_art"),
            artifacts=result.get("artifacts"),
            report=result.get("report"),
        )
