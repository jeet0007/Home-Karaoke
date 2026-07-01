import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from song_search import SongSearch, SongSearchError  # noqa: E402

RAW_RESULT = {
    "title": "Let Her Go",
    "videoId": "6bGmUTAfh-A",
    "album": {"name": "All The Little Lights"},
    "duration_seconds": 253,
    "artists": [{"name": "Passenger"}],
    "thumbnails": [
        {"url": "https://example.com/small.jpg"},
        {"url": "https://example.com/large.jpg"},
    ],
}


class FakeClient:
    def __init__(self, results=None, error=None):
        self._results = results if results is not None else []
        self._error = error
        self.calls = []

    def search(self, query, filter=None, limit=10):  # noqa: A002 - mirrors ytmusicapi's signature
        self.calls.append({"query": query, "filter": filter, "limit": limit})
        if self._error:
            raise self._error
        return self._results


class SongSearchTestCase(unittest.TestCase):
    def test_search_returns_clean_shape(self):
        client = FakeClient(results=[RAW_RESULT])
        song_search = SongSearch(client_factory=lambda: client)

        results = song_search.search("let her go passenger", limit=5)

        self.assertEqual(
            results,
            [
                {
                    "artist": "Passenger",
                    "title": "Let Her Go",
                    "album": "All The Little Lights",
                    "duration_seconds": 253,
                    "ytmusic_video_id": "6bGmUTAfh-A",
                    "cover_art": "https://example.com/large.jpg",
                }
            ],
        )
        self.assertEqual(client.calls[0]["filter"], "songs")

    def test_search_handles_missing_thumbnails_gracefully(self):
        entry = dict(RAW_RESULT)
        del entry["thumbnails"]
        client = FakeClient(results=[entry])
        song_search = SongSearch(client_factory=lambda: client)

        results = song_search.search("query")

        self.assertEqual(results[0]["cover_art"], "")

    def test_search_joins_multiple_artists(self):
        entry = dict(RAW_RESULT, artists=[{"name": "Artist A"}, {"name": "Artist B"}])
        client = FakeClient(results=[entry])
        song_search = SongSearch(client_factory=lambda: client)

        results = song_search.search("query")

        self.assertEqual(results[0]["artist"], "Artist A, Artist B")

    def test_search_handles_missing_album_gracefully(self):
        entry = dict(RAW_RESULT)
        del entry["album"]
        client = FakeClient(results=[entry])
        song_search = SongSearch(client_factory=lambda: client)

        results = song_search.search("query")

        self.assertIsNone(results[0]["album"])

    def test_search_skips_entries_without_video_id(self):
        entry_without_id = dict(RAW_RESULT)
        del entry_without_id["videoId"]
        client = FakeClient(results=[entry_without_id, RAW_RESULT])
        song_search = SongSearch(client_factory=lambda: client)

        results = song_search.search("query")

        self.assertEqual(len(results), 1)

    def test_search_wraps_client_errors(self):
        client = FakeClient(error=RuntimeError("boom"))
        song_search = SongSearch(client_factory=lambda: client)

        with self.assertRaises(SongSearchError) as ctx:
            song_search.search("query")

        self.assertEqual(ctx.exception.status_code, 502)
        self.assertIn("boom", str(ctx.exception))

    def test_search_maps_timeout_errors_to_504(self):
        client = FakeClient(error=TimeoutError("request timed out"))
        song_search = SongSearch(client_factory=lambda: client)

        with self.assertRaises(SongSearchError) as ctx:
            song_search.search("query")

        self.assertEqual(ctx.exception.status_code, 504)


if __name__ == "__main__":
    unittest.main()
