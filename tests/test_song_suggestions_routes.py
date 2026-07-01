import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app as app_module  # noqa: E402
from song_search import SongSearchError  # noqa: E402

CLEAN_SONG = {
    "artist": "Passenger",
    "title": "Let Her Go",
    "album": "All The Little Lights",
    "duration_seconds": 253,
    "ytmusic_video_id": "6bGmUTAfh-A",
    "cover_art": "https://example.com/song-cover.jpg",
}


class SongSuggestionsRouteTestCase(unittest.TestCase):
    def setUp(self):
        app_module.app.testing = True
        self.client = app_module.app.test_client()

    @patch("app.song_search.search")
    def test_returns_clean_results_without_lyrics_filtering(self, mock_search):
        mock_search.return_value = [CLEAN_SONG]

        resp = self.client.get("/song-suggestions?q=let+her+go")

        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertEqual(data["results"], [CLEAN_SONG])
        self.assertEqual(data["count"], 1)
        mock_search.assert_called_once_with("let her go", limit=8)

    @patch("app.song_search.search")
    def test_respects_custom_limit_capped_at_max(self, mock_search):
        mock_search.return_value = []

        self.client.get("/song-suggestions?q=let+her+go&limit=50")

        mock_search.assert_called_once_with("let her go", limit=app_module.SUGGESTIONS_MAX_LIMIT)

    @patch("app.song_search.search")
    def test_missing_query_skips_search_entirely(self, mock_search):
        resp = self.client.get("/song-suggestions")

        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertEqual(data["results"], [])
        self.assertEqual(data["count"], 0)
        mock_search.assert_not_called()

    @patch("app.song_search.search")
    def test_short_query_below_minimum_length_skips_search(self, mock_search):
        resp = self.client.get("/song-suggestions?q=a")

        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertEqual(data["results"], [])
        self.assertEqual(data["count"], 0)
        mock_search.assert_not_called()

    @patch("app.song_search.search")
    def test_no_results_returns_empty_list(self, mock_search):
        mock_search.return_value = []

        resp = self.client.get("/song-suggestions?q=asdkjfhaskdjfh")

        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertEqual(data["results"], [])
        self.assertEqual(data["count"], 0)

    @patch("app.song_search.search")
    def test_surfaces_network_error(self, mock_search):
        mock_search.side_effect = SongSearchError("ytmusicapi search failed: boom", status_code=502)

        resp = self.client.get("/song-suggestions?q=anything")

        self.assertEqual(resp.status_code, 502)
        self.assertIn("boom", resp.get_json()["error"])

    @patch("app.song_search.search")
    def test_surfaces_timeout_as_504(self, mock_search):
        mock_search.side_effect = SongSearchError("ytmusicapi search failed: timed out", status_code=504)

        resp = self.client.get("/song-suggestions?q=anything")

        self.assertEqual(resp.status_code, 504)


if __name__ == "__main__":
    unittest.main()
