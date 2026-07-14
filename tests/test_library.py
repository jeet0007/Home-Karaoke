import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import library  # noqa: E402

LYRICS = {"synced": [{"time_ms": 0, "text": "hi"}], "plain": "hi", "source": "lrclib"}
VIDEO = {"video_id": "vid123", "duration_seconds": 200}
MELODY = {"notes": [{"start_ms": 0, "end_ms": 500, "midi": 57}], "duration_s": 200.0}


def _temp_library(test_case):
    tmp = tempfile.TemporaryDirectory()
    test_case.addCleanup(tmp.cleanup)
    return library.SongLibrary(os.path.join(tmp.name, "library.db"))


class EnqueueTestCase(unittest.TestCase):
    def setUp(self):
        self.lib = _temp_library(self)

    def test_enqueue_creates_pending_song(self):
        song = self.lib.enqueue("Passenger", "Let Her Go", duration_seconds=253)
        self.assertEqual(song["status"], library.STATUS_PENDING)
        self.assertEqual(song["artist"], "Passenger")
        self.assertIsNone(song["error"])
        self.assertTrue(self.lib.work_available.is_set())

    def test_enqueue_requires_identity(self):
        with self.assertRaises(ValueError):
            self.lib.enqueue("", "Title")
        with self.assertRaises(ValueError):
            self.lib.enqueue("Artist", "   ")

    def test_duplicate_identity_returns_existing_row(self):
        first = self.lib.enqueue("Passenger", "Let Her Go")
        second = self.lib.enqueue("passenger", "let her go")  # case-insensitive
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(len(self.lib.list_songs()), 1)

    def test_failed_song_is_requeued_on_enqueue(self):
        song = self.lib.enqueue("Passenger", "Let Her Go")
        self.lib.mark_failed(song["id"], "no synced lyrics")
        self.assertEqual(self.lib.get(song["id"])["status"], library.STATUS_FAILED)

        requeued = self.lib.enqueue("Passenger", "Let Her Go")
        self.assertEqual(requeued["id"], song["id"])
        self.assertEqual(requeued["status"], library.STATUS_PENDING)
        self.assertIsNone(requeued["error"])

    def test_ready_song_is_not_requeued(self):
        song = self.lib.enqueue("Passenger", "Let Her Go")
        self.lib.mark_ready(song["id"], video_id="vid123", lyrics=LYRICS)
        again = self.lib.enqueue("Passenger", "Let Her Go")
        self.assertEqual(again["status"], library.STATUS_READY)

    def test_enqueue_stores_prefilled_lyrics_and_video(self):
        # A live /select-song pick that already resolved these passes them
        # through (see app.py's _enqueue_for_library_safe) so the pipeline's
        # lyrics/video stages can reuse them instead of re-fetching.
        song = self.lib.enqueue("Artist", "Title", lyrics=LYRICS, video_id="v123")
        full = self.lib.get_full(song["id"])
        self.assertEqual(full["lyrics"], LYRICS)
        self.assertEqual(full["video_id"], "v123")
        self.assertEqual(full["status"], library.STATUS_PENDING)  # still queued, not served as ready

    def test_enqueue_without_prefill_leaves_them_null(self):
        song = self.lib.enqueue("Artist", "Title")
        full = self.lib.get_full(song["id"])
        self.assertIsNone(full["lyrics"])
        self.assertIsNone(full["video_id"])

    def test_requeue_of_a_failed_song_stores_fresh_prefill(self):
        song = self.lib.enqueue("Artist", "Title")
        self.lib.mark_failed(song["id"], "no synced lyrics found in any source")
        requeued = self.lib.enqueue("Artist", "Title", lyrics=LYRICS, video_id="v123")
        self.assertEqual(requeued["id"], song["id"])
        full = self.lib.get_full(song["id"])
        self.assertEqual(full["lyrics"], LYRICS)
        self.assertEqual(full["video_id"], "v123")
        self.assertEqual(full["status"], library.STATUS_PENDING)


class QueueMechanicsTestCase(unittest.TestCase):
    def setUp(self):
        self.lib = _temp_library(self)

    def test_claim_walks_queue_in_fifo_order_and_empties(self):
        a = self.lib.enqueue("Artist", "First")
        b = self.lib.enqueue("Artist", "Second")

        first = self.lib.claim_next_pending()
        second = self.lib.claim_next_pending()
        self.assertEqual(first["id"], a["id"])
        self.assertEqual(second["id"], b["id"])
        self.assertIsNone(self.lib.claim_next_pending())

        self.assertEqual(self.lib.get(a["id"])["status"], library.STATUS_PROCESSING)

    def test_mark_ready_stores_full_payload(self):
        song = self.lib.enqueue("Passenger", "Let Her Go")
        claimed = self.lib.claim_next_pending()
        self.lib.mark_ready(
            claimed["id"], video_id="vid123", lyrics=LYRICS, melody=MELODY, duration_seconds=200
        )

        payload = self.lib.find_ready("passenger", "LET HER GO")
        self.assertIsNotNone(payload)
        self.assertEqual(payload["video_id"], "vid123")
        self.assertEqual(payload["lyrics"], LYRICS)
        self.assertEqual(payload["melody"], MELODY)
        self.assertEqual(payload["duration_seconds"], 200)
        self.assertTrue(payload["has_melody"])

    def test_find_ready_ignores_unready_songs(self):
        self.lib.enqueue("Passenger", "Let Her Go")
        self.assertIsNone(self.lib.find_ready("Passenger", "Let Her Go"))

    def test_stale_processing_song_is_rescued(self):
        song = self.lib.enqueue("Artist", "Stuck")
        claimed = self.lib.claim_next_pending()
        self.assertEqual(claimed["id"], song["id"])

        # claim_next_pending() stamped a fresh heartbeat_at at claim time;
        # simulate the worker dying by backdating it (and updated_at, for
        # realism) past the staleness horizon.
        with self.lib._db() as conn:
            conn.execute(
                "UPDATE songs SET updated_at = ?, heartbeat_at = ? WHERE id = ?",
                (
                    time.time() - library.STALE_PROCESSING_SECONDS - 1,
                    time.time() - library.STALE_PROCESSING_SECONDS - 1,
                    song["id"],
                ),
            )

        rescued = self.lib.claim_next_pending()
        self.assertIsNotNone(rescued)
        self.assertEqual(rescued["id"], song["id"])

    def test_pre_heartbeat_claim_falls_back_to_claim_age(self):
        # A row claimed by a pre-heartbeat version of this code (or one
        # whose heartbeat column was never populated) has heartbeat_at NULL
        # forever - COALESCE(heartbeat_at, updated_at) still lets it be
        # reclaimed, judged by claim age instead.
        song = self.lib.enqueue("Artist", "Stuck")
        self.lib.claim_next_pending()
        with self.lib._db() as conn:
            conn.execute(
                "UPDATE songs SET updated_at = ?, heartbeat_at = NULL WHERE id = ?",
                (time.time() - library.STALE_PROCESSING_SECONDS - 1, song["id"]),
            )
        rescued = self.lib.claim_next_pending()
        self.assertIsNotNone(rescued)
        self.assertEqual(rescued["id"], song["id"])

    def test_stale_reclaim_is_judged_by_heartbeat_not_claim_age(self):
        # A slow-but-alive stage (Demucs can run minutes) must NOT be
        # reclaimed just because the claim itself is old - only a stopped
        # heartbeat means the worker is actually gone.
        song = self.lib.enqueue("Artist", "SlowButAlive")
        claimed = self.lib.claim_next_pending()
        with self.lib._db() as conn:
            conn.execute(
                "UPDATE songs SET updated_at = ?, heartbeat_at = ? WHERE id = ?",
                (time.time() - library.STALE_PROCESSING_SECONDS - 1, time.time(), claimed["id"]),
            )
        self.assertIsNone(self.lib.claim_next_pending())  # fresh heartbeat -> not reclaimed

        self.lib.beat(claimed["id"])  # still alive
        self.assertIsNone(self.lib.claim_next_pending())

        with self.lib._db() as conn:
            conn.execute(
                "UPDATE songs SET heartbeat_at = ? WHERE id = ?",
                (time.time() - library.STALE_PROCESSING_SECONDS - 1, claimed["id"]),
            )
        rescued = self.lib.claim_next_pending()
        self.assertIsNotNone(rescued)
        self.assertEqual(rescued["id"], song["id"])

    def test_claim_prefers_higher_priority_over_fifo_order(self):
        a = self.lib.enqueue("Artist", "Backfill", priority=library.PRIORITY_BACKFILL)
        b = self.lib.enqueue("Artist", "UserPick", priority=library.PRIORITY_USER)
        # Enqueued in FIFO order a-then-b, but b (higher priority) claims first.
        self.assertEqual(self.lib.claim_next_pending()["id"], b["id"])
        self.assertEqual(self.lib.claim_next_pending()["id"], a["id"])

    def test_user_priority_enqueue_promotes_a_pending_backfill_song(self):
        song = self.lib.enqueue("Artist", "Backfill", priority=library.PRIORITY_BACKFILL)
        other = self.lib.enqueue("Artist", "Other", priority=library.PRIORITY_BACKFILL)
        # A user pick of the SAME identity while it's still queued bumps it
        # ahead of same-priority backfill work already in the queue.
        self.lib.enqueue("Artist", "Backfill", priority=library.PRIORITY_USER)
        self.assertEqual(self.lib.claim_next_pending()["id"], song["id"])
        self.assertEqual(self.lib.claim_next_pending()["id"], other["id"])

    def test_beat_and_set_current_stage(self):
        song = self.lib.enqueue("Artist", "One")
        claimed = self.lib.claim_next_pending()

        self.lib.set_current_stage(claimed["id"], "separate")
        row = self.lib.get(claimed["id"])
        self.assertEqual(row["current_stage"], "separate")

        self.lib.beat(claimed["id"])  # doesn't clobber the stage, just the heartbeat
        self.assertEqual(self.lib.get(claimed["id"])["current_stage"], "separate")

    def test_beat_is_a_no_op_on_a_settled_song(self):
        song = self.lib.enqueue("Artist", "One")
        self.lib.mark_ready(song["id"], video_id="v", lyrics=LYRICS)
        self.lib.beat(song["id"])  # must not resurrect bookkeeping on a ready row
        with self.lib._db() as conn:
            row = conn.execute("SELECT heartbeat_at FROM songs WHERE id = ?", (song["id"],)).fetchone()
        self.assertIsNone(row["heartbeat_at"])

    def test_list_songs_filters_by_status(self):
        a = self.lib.enqueue("Artist", "One")
        self.lib.enqueue("Artist", "Two")
        self.lib.mark_ready(a["id"], video_id="v", lyrics=LYRICS)

        self.assertEqual(len(self.lib.list_songs()), 2)
        ready = self.lib.list_songs(status=library.STATUS_READY)
        self.assertEqual([s["id"] for s in ready], [a["id"]])

    def test_summary_never_includes_blob_fields(self):
        song = self.lib.enqueue("Artist", "One")
        self.lib.mark_ready(song["id"], video_id="v", lyrics=LYRICS, melody=MELODY)
        summary = self.lib.get(song["id"])
        self.assertNotIn("lyrics", summary)
        self.assertNotIn("melody", summary)
        self.assertTrue(summary["has_melody"])


REPORT = {
    "lyrics": {"status": "ok", "detail": "42 synced lines from lrclib"},
    "video": {"status": "ok", "detail": "picked backing video vid123"},
    "melody": {"status": "skipped", "detail": "vocal-transcription add-on not installed"},
}


class ProcessingReportTestCase(unittest.TestCase):
    def setUp(self):
        self.lib = _temp_library(self)

    def test_ready_song_stores_and_exposes_report(self):
        song = self.lib.enqueue("Passenger", "Let Her Go")
        self.lib.mark_ready(song["id"], video_id="vid123", lyrics=LYRICS, report=REPORT)

        full = self.lib.get_full(song["id"])
        self.assertEqual(full["report"], REPORT)
        # The list/summary view exposes a compact status-only map.
        self.assertEqual(full["stages"], {"lyrics": "ok", "video": "ok", "melody": "skipped"})

    def test_failed_song_stores_partial_report(self):
        song = self.lib.enqueue("Obscure", "B-side")
        partial = {"lyrics": {"status": "failed", "detail": "no synced lyrics found in any source"}}
        self.lib.mark_failed(song["id"], "no synced lyrics found in any source", report=partial)

        full = self.lib.get_full(song["id"])
        self.assertEqual(full["report"], partial)
        self.assertEqual(full["stages"], {"lyrics": "failed"})

    def test_missing_report_is_empty_not_error(self):
        song = self.lib.enqueue("Artist", "One")
        self.lib.mark_ready(song["id"], video_id="v", lyrics=LYRICS)  # no report
        full = self.lib.get_full(song["id"])
        self.assertEqual(full["report"], {})
        self.assertEqual(full["stages"], {})

    def test_report_json_column_is_migrated_onto_an_older_db(self):
        # A pre-report-column DB has every original column EXCEPT report_json
        # (and possibly a leftover waveform_json). Opening the library must
        # migrate report_json in without disturbing the existing rows.
        import os
        import sqlite3
        import tempfile

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        db_path = os.path.join(tmp.name, "old.db")
        conn = sqlite3.connect(db_path)
        conn.execute(
            "CREATE TABLE songs (id INTEGER PRIMARY KEY AUTOINCREMENT, artist TEXT NOT NULL, title TEXT NOT NULL,"
            " album TEXT, duration_seconds INTEGER, cover_art TEXT NOT NULL DEFAULT '', ytmusic_video_id TEXT,"
            " status TEXT NOT NULL DEFAULT 'pending', error TEXT, video_id TEXT, lyrics_json TEXT, melody_json TEXT,"
            " waveform_json TEXT, created_at REAL NOT NULL, updated_at REAL NOT NULL, UNIQUE(artist, title))"
        )
        conn.execute(
            "INSERT INTO songs (artist, title, status, created_at, updated_at) VALUES ('A','B','ready',0,0)"
        )
        conn.commit()
        conn.close()

        # Opening the library migrates the column in; the old row still reads.
        lib = library.SongLibrary(db_path)
        with lib._db() as c:
            cols = {r["name"] for r in c.execute("PRAGMA table_info(songs)")}
        self.assertIn("report_json", cols)
        self.assertEqual(lib.get_full(1)["report"], {})


ARTIFACTS = [
    {"kind": "lyrics", "path": "/data/1/lyrics.json", "bytes": 40},
    {"kind": "midi", "path": "/data/1/melody.mid", "bytes": 128},
]


class ArtifactRecordingTestCase(unittest.TestCase):
    def setUp(self):
        self.lib = _temp_library(self)

    def test_mark_ready_records_artifacts(self):
        song = self.lib.enqueue("Passenger", "Let Her Go")
        self.lib.mark_ready(song["id"], video_id="vid", lyrics=LYRICS, artifacts=ARTIFACTS)

        recorded = self.lib.list_artifacts(song["id"])
        self.assertEqual({a["kind"] for a in recorded}, {"lyrics", "midi"})
        midi = self.lib.get_artifact(song["id"], "midi")
        self.assertEqual(midi["path"], "/data/1/melody.mid")
        self.assertEqual(midi["bytes"], 128)

    def test_get_full_includes_artifacts(self):
        song = self.lib.enqueue("Passenger", "Let Her Go")
        self.lib.mark_ready(song["id"], video_id="vid", lyrics=LYRICS, artifacts=ARTIFACTS)
        payload = self.lib.get_full(song["id"])
        self.assertEqual(len(payload["artifacts"]), 2)

    def test_reprocessing_replaces_artifact_rows(self):
        song = self.lib.enqueue("Passenger", "Let Her Go")
        self.lib.mark_ready(song["id"], video_id="vid", lyrics=LYRICS, artifacts=ARTIFACTS)
        # A second processing pass with an updated MIDI upserts, not dupes.
        self.lib.mark_ready(
            song["id"],
            video_id="vid",
            lyrics=LYRICS,
            artifacts=[{"kind": "midi", "path": "/data/1/melody.mid", "bytes": 256}],
        )
        recorded = self.lib.list_artifacts(song["id"])
        self.assertEqual(len(recorded), 2)  # lyrics + midi, midi replaced not duplicated
        self.assertEqual(self.lib.get_artifact(song["id"], "midi")["bytes"], 256)

    def test_missing_artifact_returns_none(self):
        song = self.lib.enqueue("Passenger", "Let Her Go")
        self.assertIsNone(self.lib.get_artifact(song["id"], "midi"))


STAGE_RUNS = [
    {
        "stage": "lyrics",
        "status": "ok",
        "started_at": 1000.0,
        "finished_at": 1000.2,
        "duration_ms": 200,
        "input_hashes": None,
        "output_path": "/data/1/lyrics.json",
        "output_hash": "abc123",
        "error": None,
        "detail": "1 synced lines from lrclib",
    },
    {
        "stage": "tempo",
        "status": "failed",
        "started_at": 1000.2,
        "finished_at": 1000.3,
        "duration_ms": 100,
        "input_hashes": ["deadbeef"],
        "output_path": None,
        "output_hash": None,
        "error": "tempo estimation failed: boom",
        "detail": "tempo estimation failed: boom",
    },
]


class StageRunsTestCase(unittest.TestCase):
    def setUp(self):
        self.lib = _temp_library(self)

    def test_round_trips_stage_runs(self):
        song = self.lib.enqueue("Passenger", "Let Her Go")
        self.lib.record_stage_runs(song["id"], "run-1", STAGE_RUNS)

        rows = self.lib.list_stage_runs(song["id"])
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["stage"], "lyrics")
        self.assertEqual(rows[0]["output_hash"], "abc123")
        self.assertIsNone(rows[0]["input_hashes"])
        # input_hashes round-trips through the input_hashes_json column.
        self.assertEqual(rows[1]["input_hashes"], ["deadbeef"])
        self.assertEqual(rows[1]["status"], "failed")

    def test_list_stage_runs_for_run_filters_by_run_id(self):
        song = self.lib.enqueue("Passenger", "Let Her Go")
        self.lib.record_stage_runs(song["id"], "run-1", STAGE_RUNS[:1])
        self.lib.record_stage_runs(song["id"], "run-2", STAGE_RUNS[1:])

        self.assertEqual(len(self.lib.list_stage_runs_for_run("run-1")), 1)
        self.assertEqual(len(self.lib.list_stage_runs_for_run("run-2")), 1)
        # Both runs' rows accumulate against the same song (append-only -
        # (song_id, stage) legitimately repeats across reprocessing runs).
        self.assertEqual(len(self.lib.list_stage_runs(song["id"])), 2)

    def test_empty_list_is_a_no_op(self):
        song = self.lib.enqueue("Passenger", "Let Her Go")
        self.lib.record_stage_runs(song["id"], "run-1", [])
        self.assertEqual(self.lib.list_stage_runs(song["id"]), [])

    def test_stage_runs_table_is_created_onto_an_older_db(self):
        # Mirrors test_report_json_column_is_migrated_onto_an_older_db: a
        # pre-stage_runs DB gains the table on open, with no backfill (an
        # existing ready song has no stage_runs rows retroactively created).
        import sqlite3

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        db_path = os.path.join(tmp.name, "old.db")
        conn = sqlite3.connect(db_path)
        conn.execute(
            "CREATE TABLE songs (id INTEGER PRIMARY KEY AUTOINCREMENT, artist TEXT NOT NULL, title TEXT NOT NULL,"
            " album TEXT, duration_seconds INTEGER, cover_art TEXT NOT NULL DEFAULT '', ytmusic_video_id TEXT,"
            " status TEXT NOT NULL DEFAULT 'pending', error TEXT, video_id TEXT, lyrics_json TEXT, melody_json TEXT,"
            " created_at REAL NOT NULL, updated_at REAL NOT NULL, UNIQUE(artist, title))"
        )
        conn.execute(
            "INSERT INTO songs (artist, title, status, created_at, updated_at) VALUES ('A','B','ready',0,0)"
        )
        conn.commit()
        conn.close()

        lib = library.SongLibrary(db_path)
        with lib._db() as c:
            tables = {r["name"] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertIn("stage_runs", tables)
        self.assertEqual(lib.list_stage_runs(1), [])


class StageStatsTestCase(unittest.TestCase):
    def setUp(self):
        self.lib = _temp_library(self)

    def test_aggregates_stage_timing_and_song_census(self):
        song = self.lib.enqueue("Passenger", "Let Her Go")
        self.lib.record_stage_runs(
            song["id"],
            "run-1",
            [
                {
                    "stage": "lyrics", "status": "ok", "started_at": 0, "finished_at": 0.2,
                    "duration_ms": 200, "input_hashes": None, "output_path": None,
                    "output_hash": None, "error": None, "detail": "",
                },
                {
                    "stage": "lyrics", "status": "ok", "started_at": 0, "finished_at": 0.4,
                    "duration_ms": 400, "input_hashes": None, "output_path": None,
                    "output_hash": None, "error": None, "detail": "",
                },
            ],
        )
        self.lib.mark_ready(song["id"], video_id="v", lyrics=LYRICS)
        other = self.lib.enqueue("Artist", "Two")
        self.lib.mark_failed(other["id"], "boom")

        stats = self.lib.stage_stats()
        self.assertEqual(stats["songs"], {"ready": 1, "failed": 1})
        lyrics_row = next(r for r in stats["stages"] if r["stage"] == "lyrics" and r["status"] == "ok")
        self.assertEqual(lyrics_row["runs"], 2)
        self.assertEqual(lyrics_row["avg_ms"], 300)
        self.assertEqual(lyrics_row["max_ms"], 400)

    def test_empty_library_yields_empty_stats(self):
        stats = self.lib.stage_stats()
        self.assertEqual(stats, {"songs": {}, "stages": []})


class LibraryWorkerTestCase(unittest.TestCase):
    def setUp(self):
        self.lib = _temp_library(self)

    def _run_worker_until(self, process, predicate, timeout=5.0):
        worker = library.LibraryWorker(self.lib, process, poll_seconds=0.05)
        worker.start()
        self.addCleanup(worker.join, 2.0)
        self.addCleanup(worker.stop)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.02)
        self.fail("worker did not reach the expected state in time")

    def test_worker_processes_queue_to_ready(self):
        song = self.lib.enqueue("Passenger", "Let Her Go")

        def process(_song, run_id=None, observer=None):
            return {"video_id": "vid123", "lyrics": LYRICS, "melody": None, "artifacts": ARTIFACTS}

        self._run_worker_until(
            process, lambda: self.lib.get(song["id"])["status"] == library.STATUS_READY
        )
        payload = self.lib.get_full(song["id"])
        self.assertEqual(payload["video_id"], "vid123")
        # Artifacts produced by the pipeline are recorded on the row.
        self.assertEqual(len(payload["artifacts"]), 2)

    def test_worker_marks_processing_error_as_failed_with_message(self):
        song = self.lib.enqueue("Obscure", "B-side")

        def process(_song, run_id=None, observer=None):
            raise library.ProcessingError("no synced lyrics found in any source")

        self._run_worker_until(
            process, lambda: self.lib.get(song["id"])["status"] == library.STATUS_FAILED
        )
        self.assertEqual(self.lib.get(song["id"])["error"], "no synced lyrics found in any source")

    def test_worker_survives_unexpected_crash_and_continues(self):
        bad = self.lib.enqueue("Artist", "Crashy")
        good = self.lib.enqueue("Artist", "Fine")

        def process(song, run_id=None, observer=None):
            if song["title"] == "Crashy":
                raise RuntimeError("boom")
            return {"video_id": "vid123", "lyrics": LYRICS, "melody": None}

        self._run_worker_until(
            process, lambda: self.lib.get(good["id"])["status"] == library.STATUS_READY
        )
        failed = self.lib.get(bad["id"])
        self.assertEqual(failed["status"], library.STATUS_FAILED)
        self.assertIn("unexpected error", failed["error"])

    def test_worker_generates_and_threads_a_run_id(self):
        song = self.lib.enqueue("Passenger", "Let Her Go")
        seen_run_ids = []

        def process(_song, run_id=None, observer=None):
            seen_run_ids.append(run_id)
            return {"video_id": "vid123", "lyrics": LYRICS, "melody": None}

        self._run_worker_until(
            process, lambda: self.lib.get(song["id"])["status"] == library.STATUS_READY
        )
        self.assertEqual(len(seen_run_ids), 1)
        self.assertTrue(seen_run_ids[0])  # non-empty - LibraryWorker generated one

    def test_worker_persists_stage_runs_via_observer_as_they_land(self):
        # The real pipeline notifies the observer per-stage (see
        # RunContext.notify in pipeline.py) rather than handing the worker a
        # batch at the end - that's what makes lineage crash-proof. This
        # stub plays that role: it calls observer("stage_end", ...) itself,
        # the same way _stage() does inside pipeline.py.
        song = self.lib.enqueue("Passenger", "Let Her Go")
        captured = {}

        def process(_song, run_id=None, observer=None):
            captured["run_id"] = run_id
            observer("stage_end", dict(STAGE_RUNS[0]))
            return {"video_id": "vid123", "lyrics": LYRICS, "melody": None, "artifacts": ARTIFACTS}

        self._run_worker_until(
            process, lambda: self.lib.get(song["id"])["status"] == library.STATUS_READY
        )
        rows = self.lib.list_stage_runs(song["id"])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["stage"], "lyrics")
        self.assertEqual(self.lib.list_stage_runs_for_run(captured["run_id"]), rows)

    def test_worker_persists_stage_runs_on_processing_error(self):
        song = self.lib.enqueue("Obscure", "B-side")

        def process(_song, run_id=None, observer=None):
            observer("stage_end", dict(STAGE_RUNS[1]))
            raise library.ProcessingError("no synced lyrics found in any source")

        self._run_worker_until(
            process, lambda: self.lib.get(song["id"])["status"] == library.STATUS_FAILED
        )
        rows = self.lib.list_stage_runs(song["id"])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["stage"], "tempo")
        self.assertEqual(rows[0]["status"], "failed")

    def test_stage_begin_sets_current_stage_while_processing(self):
        song = self.lib.enqueue("Passenger", "Let Her Go")
        seen = {}

        def process(_song, run_id=None, observer=None):
            observer("stage_begin", "melody")
            seen["current_stage"] = self.lib.get(_song["id"])["current_stage"]
            return {"video_id": "vid123", "lyrics": LYRICS, "melody": None}

        self._run_worker_until(
            process, lambda: self.lib.get(song["id"])["status"] == library.STATUS_READY
        )
        self.assertEqual(seen["current_stage"], "melody")
        # Settled songs don't carry a stale in-flight stage.
        self.assertIsNone(self.lib.get(song["id"])["current_stage"])

    def test_observer_exceptions_never_fail_the_song(self):
        # A malformed stage_end payload (missing required keys) would raise
        # inside record_stage_run - telemetry plumbing must never take the
        # song down with it.
        song = self.lib.enqueue("Passenger", "Let Her Go")

        def process(_song, run_id=None, observer=None):
            observer("stage_end", {"status": "ok"})  # missing "stage" -> KeyError, swallowed
            observer("bogus_event", "whatever")  # unknown events are also a no-op
            return {"video_id": "vid123", "lyrics": LYRICS, "melody": None}

        self._run_worker_until(
            process, lambda: self.lib.get(song["id"])["status"] == library.STATUS_READY
        )

    def test_worker_records_no_stage_runs_on_bare_crash(self):
        song = self.lib.enqueue("Artist", "Crashy")

        def process(_song, run_id=None, observer=None):
            raise RuntimeError("boom")

        self._run_worker_until(
            process, lambda: self.lib.get(song["id"])["status"] == library.STATUS_FAILED
        )
        self.assertEqual(self.lib.list_stage_runs(song["id"]), [])


if __name__ == "__main__":
    unittest.main()
