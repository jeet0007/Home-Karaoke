import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app as app_module  # noqa: E402
from search.song_search import SongSearchError  # noqa: E402

CLEAN_SONG = {
    "artist": "Passenger",
    "title": "Let Her Go",
    "album": "All The Little Lights",
    "duration_seconds": 253,
    "ytmusic_video_id": "6bGmUTAfh-A",
    "cover_art": "https://example.com/song-cover.jpg",
}

VIDEO_RESULT = {
    "video_id": "abc12345678",
    "title": "Let Her Go - Passenger (Karaoke Version)",
    "url": "https://www.youtube.com/watch?v=abc12345678",
    "duration": "4:13",
    "duration_seconds": 253,
    "thumbnail": "https://example.com/thumb.jpg",
    "uploader": "KaraFun",
    "view_count": 1000,
    "score": 30,
}


class UnifiedSearchRouteTestCase(unittest.TestCase):
    def setUp(self):
        app_module.app.testing = True
        self.client = app_module.app.test_client()

    def test_missing_query(self):
        resp = self.client.get("/unified-search")
        self.assertEqual(resp.status_code, 400)

    # -- primary path: song identity found and has lyrics -----------------

    @patch("app.filter_candidates_by_lyrics")
    @patch("app.song_search.search")
    def test_returns_identity_source_when_song_has_lyrics(self, mock_song_search, mock_filter):
        mock_song_search.return_value = [CLEAN_SONG]
        mock_filter.return_value = ([CLEAN_SONG], False)

        resp = self.client.get("/unified-search?q=let+her+go+passenger")

        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertEqual(data["source"], "identity")
        self.assertEqual(data["results"], [CLEAN_SONG])
        self.assertNotIn("warning", data)

    @patch("app.filter_candidates_by_lyrics")
    @patch("app.song_search.search")
    def test_identity_source_surfaces_degraded_warning(self, mock_song_search, mock_filter):
        mock_song_search.return_value = [CLEAN_SONG]
        mock_filter.return_value = ([CLEAN_SONG], True)

        resp = self.client.get("/unified-search?q=anything")

        data = resp.get_json()
        self.assertEqual(data["source"], "identity")
        self.assertIn("temporarily unavailable", data["warning"])

    # -- fallback trigger: no song match at all ----------------------------

    @patch("app.karaoke_search.search")
    @patch("app.filter_candidates_by_lyrics")
    @patch("app.song_search.search")
    def test_falls_back_to_videos_when_song_search_returns_nothing(
        self, mock_song_search, mock_filter, mock_karaoke_search
    ):
        mock_song_search.return_value = []
        mock_karaoke_search.return_value = [VIDEO_RESULT]
        mock_filter.return_value = ([dict(VIDEO_RESULT)], False)

        resp = self.client.get("/unified-search?q=some+obscure+query")

        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertEqual(data["source"], "fallback")
        self.assertEqual(len(data["results"]), 1)
        self.assertIn("No song match found", data["warning"])
        mock_karaoke_search.assert_called_once()

    # -- fallback trigger: song search errors ------------------------------

    @patch("app.karaoke_search.search")
    @patch("app.filter_candidates_by_lyrics")
    @patch("app.song_search.search")
    def test_falls_back_to_videos_when_song_search_errors(self, mock_song_search, mock_filter, mock_karaoke_search):
        mock_song_search.side_effect = SongSearchError("ytmusicapi search failed: boom", status_code=502)
        mock_karaoke_search.return_value = [VIDEO_RESULT]
        mock_filter.return_value = ([dict(VIDEO_RESULT)], False)

        resp = self.client.get("/unified-search?q=anything")

        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertEqual(data["source"], "fallback")
        self.assertIn("Song search unavailable", data["warning"])

    # -- fallback trigger: songs found but none have lyrics ----------------

    @patch("app.karaoke_search.search")
    @patch("app.filter_candidates_by_lyrics")
    @patch("app.song_search.search")
    def test_falls_back_to_videos_when_no_song_has_lyrics(self, mock_song_search, mock_filter, mock_karaoke_search):
        mock_song_search.return_value = [CLEAN_SONG]
        mock_karaoke_search.return_value = [VIDEO_RESULT]
        # First call (song identities) filters everything out; second call
        # (fallback videos) keeps the one video result.
        mock_filter.side_effect = [([], False), ([dict(VIDEO_RESULT)], False)]

        resp = self.client.get("/unified-search?q=anything")

        data = resp.get_json()
        self.assertEqual(data["source"], "fallback")
        self.assertIn("none have lyrics available", data["warning"])

    # -- fallback results are reshaped into song results, never raw videos --

    @patch("app.karaoke_search.search")
    @patch("app.song_search.search")
    def test_fallback_result_is_song_shaped_not_a_video_candidate(self, mock_song_search, mock_karaoke_search):
        mock_song_search.return_value = []
        mock_karaoke_search.return_value = [dict(VIDEO_RESULT)]

        with patch("lyrics.lyrica_client.check_lyrics_available") as mock_check:
            mock_check.side_effect = lambda artist, title, timeout=None, **kwargs: (artist, title) == ("Passenger", "Let Her Go")
            resp = self.client.get("/unified-search?q=anything")

        data = resp.get_json()
        self.assertEqual(data["source"], "fallback")
        self.assertEqual(len(data["results"]), 1)

        result = data["results"][0]
        self.assertEqual(
            result,
            {
                "artist": "Passenger",
                "title": "Let Her Go",
                "album": None,
                "duration_seconds": 253,
                "cover_art": "https://example.com/thumb.jpg",
            },
        )
        # Never expose video-picking fields to the frontend.
        for video_only_field in ("video_id", "url", "score", "uploader", "view_count"):
            self.assertNotIn(video_only_field, result)

    # -- both paths fail ----------------------------------------------------

    @patch("app.karaoke_search.search")
    @patch("app.song_search.search")
    def test_returns_error_when_both_paths_fail(self, mock_song_search, mock_karaoke_search):
        mock_song_search.side_effect = SongSearchError("ytmusicapi down", status_code=502)
        mock_karaoke_search.side_effect = RuntimeError("yt-dlp failed: no results")

        resp = self.client.get("/unified-search?q=anything")

        self.assertEqual(resp.status_code, 502)
        self.assertIn("error", resp.get_json())

    @patch("app.karaoke_search.search")
    @patch("app.song_search.search")
    def test_returns_video_search_error_when_only_fallback_fails(self, mock_song_search, mock_karaoke_search):
        mock_song_search.return_value = []
        mock_karaoke_search.side_effect = RuntimeError("yt-dlp failed: no results")

        resp = self.client.get("/unified-search?q=anything")

        self.assertEqual(resp.status_code, 502)

    def test_home_page_renders(self):
        resp = self.client.get("/")
        self.assertEqual(resp.status_code, 200)
        self.assertIn(b"/unified-search", resp.data)


if __name__ == "__main__":
    unittest.main()
