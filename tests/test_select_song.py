import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app as app_module  # noqa: E402

METADATA = {
    "cover_art": "https://example.com/cover.jpg",
    "duration_s": 253,
    "genre": "Folk",
}

LYRICS_FULL = {
    "synced": [{"time_ms": 0, "text": "line one"}, {"time_ms": 1000, "text": "line two"}],
    "plain": "line one\nline two",
    "source": "lrclib",
}

CANDIDATE_A = {"video_id": "aaa", "score": 20, "duration_seconds": 253}
CANDIDATE_B = {"video_id": "bbb", "score": 30, "duration_seconds": 800}


class SelectSongRouteTestCase(unittest.TestCase):
    def setUp(self):
        app_module.app.testing = True
        self.client = app_module.app.test_client()

    def test_requires_artist_and_title(self):
        resp = self.client.get("/select-song?artist=Passenger")
        self.assertEqual(resp.status_code, 400)

        resp = self.client.get("/select-song?title=Let+Her+Go")
        self.assertEqual(resp.status_code, 400)

    @patch("app.karaoke_search.search")
    @patch("app.lyrica_client.get_lyrics_full")
    @patch("app.lyrica_client.get_metadata")
    def test_success_picks_the_duration_closest_candidate(self, mock_metadata, mock_lyrics, mock_karaoke_search):
        mock_metadata.return_value = dict(METADATA)
        mock_lyrics.return_value = dict(LYRICS_FULL)
        # B ranks higher on raw karaoke score alone, but its duration is
        # wildly off Lyrica's known 253s - A should be auto-picked instead.
        mock_karaoke_search.return_value = [CANDIDATE_B, CANDIDATE_A]

        resp = self.client.get("/select-song?artist=Passenger&title=Let+Her+Go")

        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertEqual(data["video_id"], "aaa")
        self.assertEqual(data["artist"], "Passenger")
        self.assertEqual(data["title"], "Let Her Go")
        self.assertEqual(data["duration_seconds"], 253)
        self.assertEqual(data["cover_art"], "https://example.com/cover.jpg")
        self.assertEqual(data["lyrics"]["synced"], LYRICS_FULL["synced"])
        self.assertEqual(data["lyrics"]["source"], "lrclib")
        self.assertNotIn("message", data)

        # Never expose the runner-up candidate to the frontend.
        self.assertNotIn("candidates", data)
        self.assertNotIn("results", data)

    @patch("app.karaoke_search.search")
    @patch("app.lyrica_client.get_lyrics_full")
    @patch("app.lyrica_client.get_metadata")
    def test_uses_query_duration_hint_when_lyrica_metadata_has_none(
        self, mock_metadata, mock_lyrics, mock_karaoke_search
    ):
        mock_metadata.return_value = {}
        mock_lyrics.return_value = {}
        mock_karaoke_search.return_value = [CANDIDATE_A, CANDIDATE_B]

        resp = self.client.get("/select-song?artist=Passenger&title=Let+Her+Go&duration=253")

        data = resp.get_json()
        self.assertEqual(data["duration_seconds"], 253)
        self.assertEqual(data["video_id"], "aaa")

    @patch("app.karaoke_search.search")
    @patch("app.lyrica_client.get_lyrics_full")
    @patch("app.lyrica_client.get_metadata")
    def test_no_backing_track_found_signals_null_video_id_not_an_error(
        self, mock_metadata, mock_lyrics, mock_karaoke_search
    ):
        mock_metadata.return_value = dict(METADATA)
        mock_lyrics.return_value = dict(LYRICS_FULL)
        mock_karaoke_search.return_value = []

        resp = self.client.get("/select-song?artist=Passenger&title=Let+Her+Go")

        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertIsNone(data["video_id"])
        self.assertIn("message", data)
        # Lyrics/cover art should still come through even with no video.
        self.assertEqual(data["cover_art"], "https://example.com/cover.jpg")
        self.assertEqual(data["lyrics"]["synced"], LYRICS_FULL["synced"])

    @patch("app.karaoke_search.search")
    @patch("app.lyrica_client.get_lyrics_full")
    @patch("app.lyrica_client.get_metadata")
    def test_missing_lyrica_data_does_not_error(self, mock_metadata, mock_lyrics, mock_karaoke_search):
        mock_metadata.return_value = None
        mock_lyrics.return_value = None
        mock_karaoke_search.return_value = [CANDIDATE_A]

        resp = self.client.get("/select-song?artist=Passenger&title=Let+Her+Go")

        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertEqual(data["cover_art"], "")
        self.assertEqual(data["lyrics"]["synced"], [])
        self.assertEqual(data["video_id"], "aaa")

    @patch("app.karaoke_search.search")
    @patch("app.lyrica_client.get_lyrics_full")
    @patch("app.lyrica_client.get_metadata")
    def test_surfaces_karaoke_search_failures(self, mock_metadata, mock_lyrics, mock_karaoke_search):
        mock_metadata.return_value = dict(METADATA)
        mock_lyrics.return_value = dict(LYRICS_FULL)
        mock_karaoke_search.side_effect = RuntimeError("yt-dlp failed: no results")

        resp = self.client.get("/select-song?artist=Passenger&title=Let+Her+Go")

        self.assertEqual(resp.status_code, 502)


if __name__ == "__main__":
    unittest.main()
