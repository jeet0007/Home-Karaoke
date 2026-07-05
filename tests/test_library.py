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

        # Backdate the processing claim past the staleness horizon.
        with self.lib._db() as conn:
            conn.execute(
                "UPDATE songs SET updated_at = ? WHERE id = ?",
                (time.time() - library.STALE_PROCESSING_SECONDS - 1, song["id"]),
            )

        rescued = self.lib.claim_next_pending()
        self.assertIsNotNone(rescued)
        self.assertEqual(rescued["id"], song["id"])

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

        def process(_song):
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

        def process(_song):
            raise library.ProcessingError("no synced lyrics found in any source")

        self._run_worker_until(
            process, lambda: self.lib.get(song["id"])["status"] == library.STATUS_FAILED
        )
        self.assertEqual(self.lib.get(song["id"])["error"], "no synced lyrics found in any source")

    def test_worker_survives_unexpected_crash_and_continues(self):
        bad = self.lib.enqueue("Artist", "Crashy")
        good = self.lib.enqueue("Artist", "Fine")

        def process(song):
            if song["title"] == "Crashy":
                raise RuntimeError("boom")
            return {"video_id": "vid123", "lyrics": LYRICS, "melody": None}

        self._run_worker_until(
            process, lambda: self.lib.get(good["id"])["status"] == library.STATUS_READY
        )
        failed = self.lib.get(bad["id"])
        self.assertEqual(failed["status"], library.STATUS_FAILED)
        self.assertIn("unexpected error", failed["error"])


if __name__ == "__main__":
    unittest.main()
