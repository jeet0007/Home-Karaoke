import os
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app as app_module  # noqa: E402
from core import artifacts  # noqa: E402
from core import library  # noqa: E402
from core import midi  # noqa: E402
from search.song_search import SongSearch, SongSearchError  # noqa: E402

LYRICS = {"synced": [{"time_ms": 0, "text": "hi"}], "plain": "hi", "source": "lrclib"}
MELODY = {"notes": [{"start_ms": 0, "end_ms": 500, "midi": 57}], "duration_s": 200.0}


class LibraryRouteTestCaseBase(unittest.TestCase):
    def setUp(self):
        app_module.app.testing = True
        self.client = app_module.app.test_client()

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.library = library.SongLibrary(os.path.join(tmp.name, "library.db"))
        patcher = patch.object(app_module, "song_library", self.library)
        patcher.start()
        self.addCleanup(patcher.stop)

        # The live /select-song cache is a plain module-level dict, not
        # scoped to song_library - clear it so a cached response from one
        # test's identity can't leak into another's.
        app_module._live_select_cache.clear()
        self.addCleanup(app_module._live_select_cache.clear)


class LibraryAddRouteTestCase(LibraryRouteTestCaseBase):
    def test_add_enqueues_pending_song(self):
        resp = self.client.post(
            "/library/add",
            json={"artist": "Passenger", "title": "Let Her Go", "duration_seconds": 253, "ytmusic_video_id": "ytm1"},
        )
        self.assertEqual(resp.status_code, 202)
        song = resp.get_json()["song"]
        self.assertEqual(song["status"], "pending")
        self.assertEqual(song["artist"], "Passenger")

    def test_add_requires_artist_and_title(self):
        self.assertEqual(self.client.post("/library/add", json={"artist": "X"}).status_code, 400)
        self.assertEqual(self.client.post("/library/add", json={"title": "Y"}).status_code, 400)
        self.assertEqual(self.client.post("/library/add", json={}).status_code, 400)

    def test_add_tolerates_bad_duration(self):
        resp = self.client.post(
            "/library/add", json={"artist": "A", "title": "T", "duration_seconds": "not-a-number"}
        )
        self.assertEqual(resp.status_code, 202)
        self.assertIsNone(resp.get_json()["song"]["duration_seconds"])


class LibraryListRouteTestCase(LibraryRouteTestCaseBase):
    def test_lists_songs_with_status_filter(self):
        a = self.library.enqueue("Artist", "One")
        self.library.enqueue("Artist", "Two")
        self.library.mark_ready(a["id"], video_id="vid", lyrics=LYRICS)

        resp = self.client.get("/library")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json()["count"], 2)

        resp = self.client.get("/library?status=ready")
        data = resp.get_json()
        self.assertEqual(data["count"], 1)
        self.assertEqual(data["songs"][0]["id"], a["id"])

    def test_invalid_status_is_rejected(self):
        self.assertEqual(self.client.get("/library?status=bogus").status_code, 400)

    def test_song_detail_and_404(self):
        song = self.library.enqueue("Artist", "One")
        self.library.mark_ready(song["id"], video_id="vid", lyrics=LYRICS, melody=MELODY)

        resp = self.client.get(f"/library/song/{song['id']}")
        self.assertEqual(resp.status_code, 200)
        payload = resp.get_json()
        self.assertEqual(payload["lyrics"], LYRICS)
        self.assertEqual(payload["melody"], MELODY)

        self.assertEqual(self.client.get("/library/song/99999").status_code, 404)


class SongMelodyPollRouteTestCase(LibraryRouteTestCaseBase):
    def test_requires_artist_and_title(self):
        self.assertEqual(self.client.get("/song-melody?artist=X").status_code, 400)
        self.assertEqual(self.client.get("/song-melody?title=Y").status_code, 400)

    def test_not_ready_song_returns_null_melody(self):
        self.library.enqueue("Passenger", "Let Her Go")  # pending, not ready
        resp = self.client.get("/song-melody?artist=Passenger&title=Let+Her+Go")
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertFalse(data["ready"])
        self.assertIsNone(data["melody"])

    def test_ready_song_returns_melody_notes(self):
        song = self.library.enqueue("Passenger", "Let Her Go")
        self.library.mark_ready(song["id"], video_id="vid", lyrics=LYRICS, melody=MELODY)
        resp = self.client.get("/song-melody?artist=passenger&title=LET+HER+GO")  # case-insensitive
        data = resp.get_json()
        self.assertTrue(data["ready"])
        self.assertEqual(data["melody"], MELODY["notes"])

    def test_ready_song_without_melody_returns_null(self):
        song = self.library.enqueue("Passenger", "Let Her Go")
        self.library.mark_ready(song["id"], video_id="vid", lyrics=LYRICS)  # no melody
        resp = self.client.get("/song-melody?artist=Passenger&title=Let+Her+Go")
        data = resp.get_json()
        self.assertTrue(data["ready"])
        self.assertIsNone(data["melody"])


class MidiDownloadRouteTestCase(LibraryRouteTestCaseBase):
    def test_downloads_stored_midi(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        midi_path = os.path.join(tmp.name, "melody.mid")
        midi.write_midi([{"start_ms": 0, "end_ms": 500, "midi": 60}], midi_path)

        song = self.library.enqueue("Passenger", "Let Her Go")
        self.library.mark_ready(
            song["id"],
            video_id="vid",
            lyrics=LYRICS,
            artifacts=[{"kind": artifacts.KIND_MIDI, "path": midi_path, "bytes": os.path.getsize(midi_path)}],
        )

        resp = self.client.get(f"/library/song/{song['id']}/midi")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.mimetype, "audio/midi")
        self.assertEqual(resp.data[:4], b"MThd")

    def test_404_when_no_midi_recorded(self):
        song = self.library.enqueue("Passenger", "Let Her Go")
        self.assertEqual(self.client.get(f"/library/song/{song['id']}/midi").status_code, 404)

    def test_404_when_file_missing_on_disk(self):
        song = self.library.enqueue("Passenger", "Let Her Go")
        self.library.mark_ready(
            song["id"],
            video_id="vid",
            lyrics=LYRICS,
            artifacts=[{"kind": artifacts.KIND_MIDI, "path": "/nonexistent/melody.mid", "bytes": 10}],
        )
        self.assertEqual(self.client.get(f"/library/song/{song['id']}/midi").status_code, 404)


class InstrumentalRouteTestCase(LibraryRouteTestCaseBase):
    def test_serves_stored_instrumental(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        wav_path = os.path.join(tmp.name, "instrumental.wav")
        with open(wav_path, "wb") as handle:
            handle.write(b"RIFFfake-wav-bytes")

        song = self.library.enqueue("Passenger", "Let Her Go")
        self.library.mark_ready(
            song["id"],
            video_id="vid",
            lyrics=LYRICS,
            artifacts=[{"kind": artifacts.KIND_INSTRUMENTAL, "path": wav_path, "bytes": os.path.getsize(wav_path)}],
        )

        resp = self.client.get(f"/library/song/{song['id']}/instrumental")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.mimetype, "audio/wav")
        self.assertEqual(resp.data, b"RIFFfake-wav-bytes")

    def test_404_when_no_instrumental_recorded(self):
        song = self.library.enqueue("Passenger", "Let Her Go")
        self.assertEqual(self.client.get(f"/library/song/{song['id']}/instrumental").status_code, 404)

    def test_404_when_file_missing_on_disk(self):
        song = self.library.enqueue("Passenger", "Let Her Go")
        self.library.mark_ready(
            song["id"],
            video_id="vid",
            lyrics=LYRICS,
            artifacts=[{"kind": artifacts.KIND_INSTRUMENTAL, "path": "/nonexistent/instrumental.wav", "bytes": 10}],
        )
        self.assertEqual(self.client.get(f"/library/song/{song['id']}/instrumental").status_code, 404)


class SeedChartsRouteTestCase(LibraryRouteTestCaseBase):
    def test_seeds_chart_songs_into_queue(self):
        chart_songs = [
            {"artist": "A", "title": "One", "album": None, "duration_seconds": 200, "ytmusic_video_id": "y1", "cover_art": ""},
            {"artist": "B", "title": "Two", "album": None, "duration_seconds": 180, "ytmusic_video_id": "y2", "cover_art": ""},
        ]
        with patch.object(app_module.song_search, "charts", return_value=chart_songs):
            resp = self.client.post("/library/seed-charts", json={"limit": 10})

        self.assertEqual(resp.status_code, 202)
        self.assertEqual(resp.get_json()["count"], 2)
        self.assertEqual(len(self.library.list_songs(status="pending")), 2)

    def test_charts_failure_is_surfaced(self):
        with patch.object(app_module.song_search, "charts", side_effect=SongSearchError("charts down", 502)):
            resp = self.client.post("/library/seed-charts", json={})
        self.assertEqual(resp.status_code, 502)

    def test_reseeding_is_idempotent(self):
        chart_songs = [
            {"artist": "A", "title": "One", "album": None, "duration_seconds": 200, "ytmusic_video_id": "y1", "cover_art": ""}
        ]
        with patch.object(app_module.song_search, "charts", return_value=chart_songs):
            self.client.post("/library/seed-charts", json={})
            self.client.post("/library/seed-charts", json={})
        self.assertEqual(len(self.library.list_songs()), 1)


class SelectSongLibraryFastPathTestCase(LibraryRouteTestCaseBase):
    def _make_ready_song(self):
        song = self.library.enqueue("Passenger", "Let Her Go", duration_seconds=253, cover_art="http://art")
        self.library.mark_ready(
            song["id"], video_id="vid123", lyrics=LYRICS, melody=MELODY, duration_seconds=253
        )
        return song

    @patch("app.karaoke_search.search")
    def test_ready_song_served_from_library_without_live_lookups(self, mock_karaoke_search):
        self._make_ready_song()

        with patch.object(app_module, "_prewarm_stream_cache"):
            resp = self.client.get("/select-song?artist=Passenger&title=Let+Her+Go")

        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertEqual(data["source"], "library")
        self.assertEqual(data["video_id"], "vid123")
        self.assertEqual(data["lyrics"]["synced"], LYRICS["synced"])
        self.assertEqual(data["melody"], MELODY["notes"])
        self.assertEqual(data["cover_art"], "http://art")
        self.assertFalse(data["has_instrumental"])  # no stored stem: video fallback
        mock_karaoke_search.assert_not_called()

    @patch("app.karaoke_search.search")
    def test_instrumental_song_flags_single_source_and_skips_prewarm(self, mock_karaoke_search):
        song = self.library.enqueue("Passenger", "Let Her Go", duration_seconds=253)
        self.library.mark_ready(
            song["id"],
            video_id="vid123",
            lyrics=LYRICS,
            melody=MELODY,
            artifacts=[{"kind": artifacts.KIND_INSTRUMENTAL, "path": "/data/1/instrumental.wav", "bytes": 4}],
        )

        with patch.object(app_module, "_prewarm_stream_cache") as prewarm:
            resp = self.client.get("/select-song?artist=Passenger&title=Let+Her+Go")

        data = resp.get_json()
        self.assertTrue(data["has_instrumental"])
        # Single-source playback never touches the YouTube stream, so no
        # yt-dlp cache warm should be spent on it.
        prewarm.assert_not_called()
        mock_karaoke_search.assert_not_called()

    @patch("app.karaoke_search.search")
    @patch("app.lyrica_client.get_lyrics_full")
    @patch("app.lyrica_client.get_metadata")
    def test_live_path_enqueues_song_for_background_processing(
        self, mock_metadata, mock_lyrics, mock_karaoke_search
    ):
        mock_metadata.return_value = {"cover_art": "http://art", "duration_s": 253}
        mock_lyrics.return_value = dict(LYRICS)
        mock_karaoke_search.return_value = [{"video_id": "aaa", "score": 20, "duration_seconds": 253}]

        with patch("lyrics.lrclib_client.get_lyrics_full", return_value=None):
            resp = self.client.get(
                "/select-song?artist=Passenger&title=Let+Her+Go&ytmusic_video_id=ytm42"
            )

        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertEqual(data["source"], "live")
        self.assertIsNone(data["melody"])

        rows = self.library.list_songs(status="pending")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["artist"], "Passenger")
        # ytmusic_video_id rides along for the worker's melody extraction.
        full = self.library.get_full(rows[0]["id"])
        self.assertEqual(full["status"], "pending")


class SongSearchChartsTestCase(unittest.TestCase):
    def test_charts_cleans_and_caps_results(self):
        class FakeClient:
            def get_charts(self, country):
                assert country == "ZZ"
                return {
                    "songs": {
                        "items": [
                            {"videoId": "v1", "title": "One", "artists": [{"name": "A"}], "thumbnails": []},
                            {"videoId": None, "title": "skipped", "artists": []},
                            {"videoId": "v2", "title": "Two", "artists": [{"name": "B"}], "thumbnails": []},
                        ]
                    }
                }

        search = SongSearch(client_factory=FakeClient)
        results = search.charts(limit=1)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["title"], "One")
        self.assertEqual(results[0]["ytmusic_video_id"], "v1")

    def test_charts_error_raises_song_search_error(self):
        class BrokenClient:
            def get_charts(self, country):
                raise RuntimeError("nope")

        search = SongSearch(client_factory=BrokenClient)
        with self.assertRaises(SongSearchError):
            search.charts()


if __name__ == "__main__":
    unittest.main()
