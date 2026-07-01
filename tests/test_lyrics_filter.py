import os
import sys
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import lyrica_client  # noqa: E402
from lyrics_filter import filter_candidates_by_lyrics  # noqa: E402


def _song_identity(song):
    return (song["artist"], song["title"])


class FilterCandidatesByLyricsTestCase(unittest.TestCase):
    def _songs(self, n):
        return [{"artist": f"Artist {i}", "title": f"Song {i}"} for i in range(n)]

    @patch("lyrics_filter.lyrica_client.check_lyrics_available")
    def test_keeps_has_lyrics_drops_no_lyrics(self, mock_check):
        songs = self._songs(3)
        # song 0 has lyrics, song 1 doesn't, song 2 has lyrics
        mock_check.side_effect = lambda artist, title, timeout=None: title != "Song 1"

        kept, degraded = filter_candidates_by_lyrics(songs, _song_identity)

        self.assertEqual([s["title"] for s in kept], ["Song 0", "Song 2"])
        self.assertFalse(degraded)

    @patch("lyrics_filter.lyrica_client.check_lyrics_available")
    def test_fails_open_on_lyrica_error(self, mock_check):
        songs = self._songs(2)

        def side_effect(artist, title, timeout=None):
            if title == "Song 0":
                raise lyrica_client.LyricaUnavailableError("boom")
            return True

        mock_check.side_effect = side_effect

        kept, degraded = filter_candidates_by_lyrics(songs, _song_identity)

        # Song 0 errored but is kept (fail open), Song 1 confirmed has lyrics.
        self.assertEqual({s["title"] for s in kept}, {"Song 0", "Song 1"})
        self.assertFalse(degraded)

    @patch("lyrics_filter.lyrica_client.check_lyrics_available")
    def test_degraded_true_only_when_every_check_errors(self, mock_check):
        songs = self._songs(3)
        mock_check.side_effect = lyrica_client.LyricaUnavailableError("service down")

        kept, degraded = filter_candidates_by_lyrics(songs, _song_identity)

        self.assertEqual(len(kept), 3)  # fail-open keeps everything
        self.assertTrue(degraded)

    @patch("lyrics_filter.lyrica_client.check_lyrics_available")
    def test_caps_number_of_candidates_checked(self, mock_check):
        songs = self._songs(20)
        mock_check.return_value = True

        kept, _ = filter_candidates_by_lyrics(songs, _song_identity, cap=5)

        self.assertEqual(mock_check.call_count, 5)
        self.assertEqual(len(kept), 5)

    @patch("lyrics_filter.lyrica_client.check_lyrics_available")
    def test_tries_each_identity_guess_until_one_resolves(self, mock_check):
        video = {"title": "Let Her Go - Passenger (Karaoke Version)"}

        def identity_fn(_candidate):
            return [("Let Her Go", "Passenger"), ("Passenger", "Let Her Go")]

        def side_effect(artist, title, timeout=None):
            return (artist, title) == ("Passenger", "Let Her Go")

        mock_check.side_effect = side_effect

        kept, degraded = filter_candidates_by_lyrics([video], identity_fn)

        self.assertEqual(len(kept), 1)
        self.assertEqual(kept[0]["_resolved_identity"], ("Passenger", "Let Her Go"))
        self.assertFalse(degraded)

    @patch("lyrics_filter.lyrica_client.check_lyrics_available")
    def test_drops_candidate_when_no_identity_guess_has_lyrics(self, mock_check):
        video = {"title": "Untitled Karaoke Track"}

        def identity_fn(_candidate):
            return [("A", "B"), ("B", "A")]

        mock_check.return_value = False

        kept, degraded = filter_candidates_by_lyrics([video], identity_fn)

        self.assertEqual(kept, [])
        self.assertFalse(degraded)

    def test_empty_candidates_returns_empty_not_degraded(self):
        kept, degraded = filter_candidates_by_lyrics([], _song_identity)

        self.assertEqual(kept, [])
        self.assertFalse(degraded)

    @patch("lyrics_filter.lyrica_client.check_lyrics_available")
    def test_checks_run_concurrently_not_serialized(self, mock_check):
        # Each check "blocks" for 0.2s; 8 candidates run sequentially would take
        # ~1.6s. With the default thread pool they should overlap heavily.
        def slow_check(artist, title, timeout=None):
            time.sleep(0.2)
            return True

        mock_check.side_effect = slow_check
        songs = self._songs(8)

        started = time.monotonic()
        kept, _ = filter_candidates_by_lyrics(songs, _song_identity, max_workers=8)
        elapsed = time.monotonic() - started

        self.assertEqual(len(kept), 8)
        self.assertLess(elapsed, 0.6)  # well under the ~1.6s serial baseline

    @patch("lyrics_filter.lyrica_client.check_lyrics_available")
    def test_preserves_original_candidate_order(self, mock_check):
        # Earlier candidates take longer, so a naive as-completed collection
        # would reorder them - results must come back in input order regardless.
        delays = {"Song 0": 0.15, "Song 1": 0.05, "Song 2": 0.1}

        def side_effect(artist, title, timeout=None):
            time.sleep(delays.get(title, 0))
            return True

        mock_check.side_effect = side_effect
        songs = self._songs(3)

        kept, _ = filter_candidates_by_lyrics(songs, _song_identity)

        self.assertEqual([s["title"] for s in kept], ["Song 0", "Song 1", "Song 2"])


if __name__ == "__main__":
    unittest.main()
