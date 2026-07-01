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
}

VIDEO_RESULT = {
    "video_id": "abc12345678",
    "title": "Let Her Go - Passenger (Karaoke Version)",
    "url": "https://www.youtube.com/watch?v=abc12345678",
    "duration": "4:13",
    "thumbnail": "https://example.com/thumb.jpg",
    "uploader": "KaraFun",
    "view_count": 1000,
    "score": 30,
}


class SongSearchRouteTestCase(unittest.TestCase):
    def setUp(self):
        app_module.app.testing = True
        self.client = app_module.app.test_client()

    # -- /song-search --------------------------------------------------

    def test_song_search_missing_query(self):
        resp = self.client.get("/song-search")
        self.assertEqual(resp.status_code, 400)

    @patch("app.song_search.search")
    def test_song_search_returns_clean_results(self, mock_search):
        mock_search.return_value = [CLEAN_SONG]

        resp = self.client.get("/song-search?q=let+her+go+passenger&limit=5")

        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertEqual(data["results"], [CLEAN_SONG])
        mock_search.assert_called_once_with("let her go passenger", limit=5)

    @patch("app.song_search.search")
    def test_song_search_surfaces_network_error(self, mock_search):
        mock_search.side_effect = SongSearchError("ytmusicapi search failed: boom", status_code=502)

        resp = self.client.get("/song-search?q=anything")

        self.assertEqual(resp.status_code, 502)
        self.assertIn("boom", resp.get_json()["error"])

    @patch("app.song_search.search")
    def test_song_search_surfaces_timeout_as_504(self, mock_search):
        mock_search.side_effect = SongSearchError("ytmusicapi search failed: timed out", status_code=504)

        resp = self.client.get("/song-search?q=anything")

        self.assertEqual(resp.status_code, 504)

    # -- /video-search --------------------------------------------------

    def test_video_search_requires_artist_and_title(self):
        resp = self.client.get("/video-search?artist=Passenger")
        self.assertEqual(resp.status_code, 400)

    @patch("app.karaoke_search.search")
    def test_video_search_queries_karaoke_search_with_clean_identity(self, mock_search):
        mock_search.return_value = [VIDEO_RESULT]

        resp = self.client.get("/video-search?artist=Passenger&title=Let+Her+Go&limit=5")

        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertEqual(data["results"], [VIDEO_RESULT])
        # KaraokeSearch.search() appends " karaoke" itself, so the query we
        # pass in should just be "<title> <artist>".
        mock_search.assert_called_once_with("Let Her Go Passenger", max_results=5)

    @patch("app.karaoke_search.search")
    def test_video_search_surfaces_ytdlp_failures(self, mock_search):
        mock_search.side_effect = RuntimeError("yt-dlp failed: no results")

        resp = self.client.get("/video-search?artist=Passenger&title=Let+Her+Go")

        self.assertEqual(resp.status_code, 502)

    # -- /player passes clean artist/title through for /select-song ---

    def test_player_embeds_clean_song_identity_for_select_song_fetch(self):
        resp = self.client.get("/player?title=Let+Her+Go&artist=Passenger&duration=253")

        self.assertEqual(resp.status_code, 200)
        html = resp.get_data(as_text=True)
        # player.html's loadSong() fetches /select-song using these exact
        # template vars, so whatever identity /player is called with is what
        # /select-song ends up queried with.
        self.assertIn('const songTitle = "Let Her Go"', html)
        self.assertIn('const artist = "Passenger"', html)
        self.assertIn('const durationHint = "253"', html)

    def test_songs_page_redirects_to_home(self):
        resp = self.client.get("/songs")
        self.assertEqual(resp.status_code, 301)
        self.assertTrue(resp.headers["Location"].endswith("/"))


if __name__ == "__main__":
    unittest.main()
