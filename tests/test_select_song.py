import os
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app as app_module  # noqa: E402
from core import library  # noqa: E402

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


def _block_lrclib_fallback(test_case):
    """/select-song's lyrics lookup now falls back to the real LRCLIB API
    when the (mocked) Lyrica client returns nothing - stub the fallback out
    so these tests never touch the network. Also swap the module-level song
    library for a throwaway one so /select-song's library fast-path and
    background enqueue never touch the real library.db."""
    patcher = patch("lyrics.lrclib_client.get_lyrics_full", return_value=None)
    patcher.start()
    test_case.addCleanup(patcher.stop)

    tmp = tempfile.TemporaryDirectory()
    test_case.addCleanup(tmp.cleanup)
    library_patcher = patch.object(
        app_module, "song_library", library.SongLibrary(os.path.join(tmp.name, "library.db"))
    )
    library_patcher.start()
    test_case.addCleanup(library_patcher.stop)

    # The live /select-song response cache is a plain module-level dict, not
    # scoped per-library like the line above - many tests here reuse the
    # same "Passenger"/"Let Her Go" identity with different mocked
    # responses, so a stale cache entry from an earlier test would otherwise
    # leak into a later one.
    app_module._live_select_cache.clear()
    test_case.addCleanup(app_module._live_select_cache.clear)


class SelectSongRouteTestCase(unittest.TestCase):
    def setUp(self):
        app_module.app.testing = True
        self.client = app_module.app.test_client()
        _block_lrclib_fallback(self)

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


class SelectSongRunsLookupsConcurrentlyTestCase(unittest.TestCase):
    """metadata, lyrics, and the karaoke video search are independent lookups
    - none consumes another's output - so /select-song must run them
    concurrently. Each mock sleeps for a distinguishable delay; if the route
    still ran them sequentially, total wall-clock would be close to the sum
    of all three delays instead of close to the slowest one."""

    def setUp(self):
        app_module.app.testing = True
        self.client = app_module.app.test_client()
        _block_lrclib_fallback(self)

    @patch("app.karaoke_search.search")
    @patch("app.lyrica_client.get_lyrics_full")
    @patch("app.lyrica_client.get_metadata")
    def test_metadata_lyrics_and_search_run_in_parallel(self, mock_metadata, mock_lyrics, mock_karaoke_search):
        delay = 0.3

        def slow_metadata(*_args, **_kwargs):
            time.sleep(delay)
            return dict(METADATA)

        def slow_lyrics(*_args, **_kwargs):
            time.sleep(delay)
            return dict(LYRICS_FULL)

        def slow_search(*_args, **_kwargs):
            time.sleep(delay)
            return [CANDIDATE_A]

        mock_metadata.side_effect = slow_metadata
        mock_lyrics.side_effect = slow_lyrics
        mock_karaoke_search.side_effect = slow_search

        started = time.monotonic()
        resp = self.client.get("/select-song?artist=Passenger&title=Let+Her+Go")
        elapsed = time.monotonic() - started

        self.assertEqual(resp.status_code, 200)
        # Sequential would take ~3x delay (~0.9s); concurrent should stay
        # close to 1x delay (~0.3s). 2x delay leaves headroom for thread/test
        # overhead while still failing if the calls run back-to-back.
        self.assertLess(elapsed, delay * 2)

    @patch("app.karaoke_search.search")
    @patch("app.lyrica_client.get_lyrics_full")
    @patch("app.lyrica_client.get_metadata")
    def test_a_failing_lookup_does_not_suppress_the_others(self, mock_metadata, mock_lyrics, mock_karaoke_search):
        # Metadata raising an unexpected exception must not take down the
        # lyrics/video results that succeeded concurrently alongside it.
        mock_metadata.side_effect = RuntimeError("boom")
        mock_lyrics.return_value = dict(LYRICS_FULL)
        mock_karaoke_search.return_value = [CANDIDATE_A]

        resp = self.client.get("/select-song?artist=Passenger&title=Let+Her+Go")

        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertEqual(data["cover_art"], "")
        self.assertEqual(data["lyrics"]["synced"], LYRICS_FULL["synced"])
        self.assertEqual(data["video_id"], "aaa")


class SelectSongPrewarmsStreamCacheTestCase(unittest.TestCase):
    """/select-song already knows the winning video_id before it returns;
    it should fire a best-effort background resolution of that video's
    stream URL so the frontend's near-certain following /stream-url call
    often finds a warm _STREAM_CACHE entry - without making /select-song
    itself wait on that resolution."""

    def setUp(self):
        app_module.app.testing = True
        self.client = app_module.app.test_client()
        _block_lrclib_fallback(self)
        with app_module._STREAM_CACHE_LOCK:
            app_module._STREAM_CACHE.clear()

    @patch("app.karaoke_search.search")
    @patch("app.lyrica_client.get_lyrics_full")
    @patch("app.lyrica_client.get_metadata")
    @patch("app._get_upstream_stream_url")
    def test_does_not_block_on_the_background_prewarm(
        self, mock_resolve, mock_metadata, mock_lyrics, mock_karaoke_search
    ):
        mock_metadata.return_value = dict(METADATA)
        mock_lyrics.return_value = dict(LYRICS_FULL)
        mock_karaoke_search.return_value = [CANDIDATE_A]

        started_resolve = threading.Event()
        release_resolve = threading.Event()

        def blocking_resolve(video_id, *args, **kwargs):
            started_resolve.set()
            release_resolve.wait(timeout=2)
            return f"https://example.com/{video_id}", None

        mock_resolve.side_effect = blocking_resolve

        resp = self.client.get("/select-song?artist=Passenger&title=Let+Her+Go")

        # The response already came back even though blocking_resolve may
        # still be parked on release_resolve.wait() - proving /select-song
        # did not wait on it synchronously.
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json()["video_id"], "aaa")

        self.assertTrue(started_resolve.wait(timeout=2), "background prewarm was never triggered")
        mock_resolve.assert_called_once_with("aaa")
        release_resolve.set()

    @patch("app.karaoke_search.search")
    @patch("app.lyrica_client.get_lyrics_full")
    @patch("app.lyrica_client.get_metadata")
    @patch("app._get_upstream_stream_url")
    def test_no_backing_track_found_does_not_trigger_a_prewarm(
        self, mock_resolve, mock_metadata, mock_lyrics, mock_karaoke_search
    ):
        mock_metadata.return_value = dict(METADATA)
        mock_lyrics.return_value = dict(LYRICS_FULL)
        mock_karaoke_search.return_value = []

        resp = self.client.get("/select-song?artist=Passenger&title=Let+Her+Go")

        self.assertEqual(resp.status_code, 200)
        self.assertIsNone(resp.get_json()["video_id"])
        time.sleep(0.1)
        mock_resolve.assert_not_called()


if __name__ == "__main__":
    unittest.main()
