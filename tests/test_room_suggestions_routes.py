import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import library  # noqa: E402

import app as app_module  # noqa: E402
from search.song_search import SongSearchError  # noqa: E402

CHART_SONG = {
    "artist": "Journey",
    "title": "Don't Stop Believin'",
    "album": "Escape",
    "duration_seconds": 251,
    "ytmusic_video_id": "1k8craCGpgs",
    "cover_art": "https://example.com/journey.jpg",
}

READY_SONG_ROW = {
    "id": 7,
    "artist": "Bonnie Tyler",
    "title": "Total Eclipse of the Heart",
    "album": "Faster Than the Speed of Night",
    "duration_seconds": 336,
    "cover_art": "https://example.com/bonnie.jpg",
}


class RoomSuggestionsRouteTestCase(unittest.TestCase):
    def setUp(self):
        app_module.app.testing = True
        self.client = app_module.app.test_client()
        # The trending cache is a module-level singleton - reset it so one
        # test's mock response can't leak into another's assertions.
        app_module._room_trending_cache = None

    @patch("app.song_library.list_songs")
    @patch("app.song_search.charts")
    def test_returns_trending_and_group_picks(self, mock_charts, mock_list_songs):
        mock_charts.return_value = [CHART_SONG]
        mock_list_songs.return_value = [READY_SONG_ROW]

        resp = self.client.get("/room-suggestions")

        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertEqual(data["trending"], [CHART_SONG])
        self.assertEqual(len(data["group_picks"]), 1)
        self.assertEqual(data["group_picks"][0]["artist"], "Bonnie Tyler")
        self.assertEqual(data["group_picks"][0]["title"], "Total Eclipse of the Heart")
        mock_list_songs.assert_called_once_with(status=library.STATUS_READY, limit=app_module.ROOM_GROUP_PICKS_LIMIT)

    @patch("app.song_library.list_songs")
    @patch("app.song_search.charts")
    def test_trending_is_cached_across_requests(self, mock_charts, mock_list_songs):
        mock_charts.return_value = [CHART_SONG]
        mock_list_songs.return_value = []

        self.client.get("/room-suggestions")
        self.client.get("/room-suggestions")

        mock_charts.assert_called_once()

    @patch("app.song_library.list_songs")
    @patch("app.song_search.charts")
    def test_chart_failure_degrades_to_empty_trending_not_an_error(self, mock_charts, mock_list_songs):
        mock_charts.side_effect = SongSearchError("ytmusicapi charts failed: boom", status_code=502)
        mock_list_songs.return_value = []

        resp = self.client.get("/room-suggestions")

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json()["trending"], [])


if __name__ == "__main__":
    unittest.main()
