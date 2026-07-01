import os
import sys
import time
import unittest
from unittest.mock import patch

import httpx

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import lyrica_client  # noqa: E402
import lyrics_filter  # noqa: E402
from lyrics_filter import filter_candidates_by_lyrics  # noqa: E402


def _song_identity(song):
    return (song["artist"], song["title"])


def _timeout_error(message="timed out"):
    """Build a LyricaUnavailableError whose __cause__ makes it look like the
    real client's timeout path, so the retry-on-timeout logic kicks in."""
    err = lyrica_client.LyricaUnavailableError(message)
    err.__cause__ = httpx.TimeoutException(message)
    return err


def _non_timeout_error(message="boom"):
    """A LyricaUnavailableError with no timeout cause (e.g. HTTP 500, bad
    JSON) - should NOT be retried."""
    return lyrica_client.LyricaUnavailableError(message)


class FilterCandidatesByLyricsTestCase(unittest.TestCase):
    def _songs(self, n):
        return [{"artist": f"Artist {i}", "title": f"Song {i}"} for i in range(n)]

    @patch("lyrics_filter.lyrica_client.check_lyrics_available")
    def test_keeps_has_lyrics_drops_no_lyrics(self, mock_check):
        songs = self._songs(3)
        # song 0 has lyrics, song 1 doesn't, song 2 has lyrics
        mock_check.side_effect = lambda artist, title, timeout=None, **kwargs: title != "Song 1"

        kept, degraded = filter_candidates_by_lyrics(songs, _song_identity)

        self.assertEqual([s["title"] for s in kept], ["Song 0", "Song 2"])
        self.assertFalse(degraded)

    @patch("lyrics_filter.lyrica_client.check_lyrics_available")
    def test_excludes_candidate_on_lyrica_error_instead_of_failing_open(self, mock_check):
        songs = self._songs(2)

        def side_effect(artist, title, timeout=None, **kwargs):
            if title == "Song 0":
                raise _non_timeout_error("boom")
            return True

        mock_check.side_effect = side_effect

        kept, degraded = filter_candidates_by_lyrics(songs, _song_identity)

        # Song 0 errored (unverified) and must be EXCLUDED, not kept - a
        # single candidate's check failing is not grounds to hand the user
        # something with no confirmed lyrics. Song 1 confirmed has lyrics.
        self.assertEqual({s["title"] for s in kept}, {"Song 1"})
        self.assertFalse(degraded)

    @patch("lyrics_filter.lyrica_client.check_lyrics_available")
    def test_single_candidate_error_amid_healthy_batch_is_not_degraded(self, mock_check):
        # 1 error out of 15 (~7%) is well under the systemic threshold -
        # this is "this one candidate had a hiccup", not "Lyrica is down".
        songs = self._songs(15)

        def side_effect(artist, title, timeout=None, **kwargs):
            if title == "Song 0":
                raise _non_timeout_error("boom")
            return True

        mock_check.side_effect = side_effect

        kept, degraded = filter_candidates_by_lyrics(songs, _song_identity, stagger=0)

        self.assertEqual(len(kept), 14)
        self.assertNotIn("Song 0", {s["title"] for s in kept})
        self.assertFalse(degraded)

    @patch("lyrics_filter.lyrica_client.check_lyrics_available")
    def test_degraded_true_when_error_ratio_crosses_threshold(self, mock_check):
        # 12/15 (80%) erroring is the "Lyrica itself is unreachable" shape:
        # unrelated songs failing together points at a shared failure point,
        # not 12 coincidentally-unavailable songs.
        songs = self._songs(15)

        def side_effect(artist, title, timeout=None, **kwargs):
            index = int(title.split()[-1])
            if index < 12:
                raise _non_timeout_error("service down")
            return True

        mock_check.side_effect = side_effect

        kept, degraded = filter_candidates_by_lyrics(songs, _song_identity, stagger=0)

        self.assertEqual({s["title"] for s in kept}, {"Song 12", "Song 13", "Song 14"})
        self.assertTrue(degraded)

    @patch("lyrics_filter.lyrica_client.check_lyrics_available")
    def test_degraded_true_when_every_check_errors(self, mock_check):
        songs = self._songs(3)
        mock_check.side_effect = _non_timeout_error("service down")

        kept, degraded = filter_candidates_by_lyrics(songs, _song_identity)

        self.assertEqual(kept, [])  # unverified candidates are excluded, not kept
        self.assertTrue(degraded)

    @patch("lyrics_filter.lyrica_client.check_lyrics_available")
    def test_retries_once_on_timeout_then_succeeds(self, mock_check):
        calls = {"Song 0": 0}

        def side_effect(artist, title, timeout=None, **kwargs):
            if title == "Song 0":
                calls["Song 0"] += 1
                if calls["Song 0"] == 1:
                    raise _timeout_error()
                return True
            return True

        mock_check.side_effect = side_effect
        songs = self._songs(2)

        kept, degraded = filter_candidates_by_lyrics(songs, _song_identity)

        self.assertEqual({s["title"] for s in kept}, {"Song 0", "Song 1"})
        self.assertEqual(calls["Song 0"], 2)  # one retry happened
        self.assertFalse(degraded)

    @patch("lyrics_filter.lyrica_client.check_lyrics_available")
    def test_timeout_retry_is_bounded_then_excludes(self, mock_check):
        # Every attempt times out - the retry budget (1) is spent and the
        # candidate is still excluded, not kept.
        mock_check.side_effect = lambda artist, title, timeout=None, **kwargs: (_ for _ in ()).throw(_timeout_error())
        songs = self._songs(1)

        kept, degraded = filter_candidates_by_lyrics(songs, _song_identity)

        self.assertEqual(kept, [])
        self.assertEqual(mock_check.call_count, 2)  # original attempt + 1 retry

    @patch("lyrics_filter.lyrica_client.check_lyrics_available")
    def test_non_timeout_error_is_not_retried(self, mock_check):
        mock_check.side_effect = lambda artist, title, timeout=None, **kwargs: (_ for _ in ()).throw(_non_timeout_error())
        songs = self._songs(1)

        kept, degraded = filter_candidates_by_lyrics(songs, _song_identity)

        self.assertEqual(kept, [])
        self.assertEqual(mock_check.call_count, 1)  # no retry for a non-timeout error

    @patch("lyrics_filter.lyrica_client.check_lyrics_available")
    def test_caps_number_of_candidates_checked(self, mock_check):
        songs = self._songs(20)
        mock_check.return_value = True

        kept, _ = filter_candidates_by_lyrics(songs, _song_identity, cap=5, stagger=0)

        self.assertEqual(mock_check.call_count, 5)
        self.assertEqual(len(kept), 5)

    @patch("lyrics_filter.lyrica_client.check_lyrics_available")
    def test_tries_each_identity_guess_until_one_resolves(self, mock_check):
        video = {"title": "Let Her Go - Passenger (Karaoke Version)"}

        def identity_fn(_candidate):
            return [("Let Her Go", "Passenger"), ("Passenger", "Let Her Go")]

        def side_effect(artist, title, timeout=None, **kwargs):
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
        def slow_check(artist, title, timeout=None, **kwargs):
            time.sleep(0.2)
            return True

        mock_check.side_effect = slow_check
        songs = self._songs(8)

        started = time.monotonic()
        kept, _ = filter_candidates_by_lyrics(songs, _song_identity, max_workers=8, stagger=0)
        elapsed = time.monotonic() - started

        self.assertEqual(len(kept), 8)
        self.assertLess(elapsed, 0.6)  # well under the ~1.6s serial baseline

    @patch("lyrics_filter.lyrica_client.check_lyrics_available")
    def test_preserves_original_candidate_order(self, mock_check):
        # Earlier candidates take longer, so a naive as-completed collection
        # would reorder them - results must come back in input order regardless.
        delays = {"Song 0": 0.15, "Song 1": 0.05, "Song 2": 0.1}

        def side_effect(artist, title, timeout=None, **kwargs):
            time.sleep(delays.get(title, 0))
            return True

        mock_check.side_effect = side_effect
        songs = self._songs(3)

        kept, _ = filter_candidates_by_lyrics(songs, _song_identity)

        self.assertEqual([s["title"] for s in kept], ["Song 0", "Song 1", "Song 2"])

    @patch("lyrics_filter.lyrica_client.check_lyrics_available")
    def test_uses_lrclib_only_check_by_default(self, mock_check):
        # Pre-selection candidate checks must ask Lyrica for its
        # pass=true&sequence=2 mode (LRCLIB only) rather than fast=true
        # (LRCLIB+YouTube) or the full 6-source sequential chain - see
        # LYRICS_FILTER_CHECK_MODE in lyrics_filter.py. LRCLIB-only skips
        # YouTube's slow 3-layer cascade entirely.
        mock_check.return_value = True
        songs = self._songs(1)

        filter_candidates_by_lyrics(songs, _song_identity)

        _args, kwargs = mock_check.call_args
        self.assertEqual(kwargs["sequence"], lyrics_filter.LRCLIB_FETCHER_ID)
        self.assertNotIn("fast", kwargs)

    @patch("lyrics_filter.lyrica_client.check_lyrics_available")
    def test_check_mode_can_be_switched_to_fast_via_env_var(self, mock_check):
        # LYRICS_FILTER_CHECK_MODE is read from the env var once at import
        # time; patch the resolved module attribute directly rather than
        # reloading the module (which would also re-bind lyrica_client under
        # the mock in ways unrelated to this test).
        mock_check.return_value = True
        songs = self._songs(1)

        with patch.object(lyrics_filter, "LYRICS_FILTER_CHECK_MODE", "fast"):
            lyrics_filter.filter_candidates_by_lyrics(songs, _song_identity)

        _args, kwargs = mock_check.call_args
        self.assertTrue(kwargs["fast"])
        self.assertNotIn("sequence", kwargs)

    @patch("lyrics_filter.lyrica_client.check_lyrics_available")
    def test_default_max_workers_is_gentler_than_previous_default(self, mock_check):
        # Lyrica's backend can only truly process 1-2 requests at once (2
        # sync gunicorn workers in prod, a single-threaded dev server
        # locally) - see DEFAULT_MAX_WORKERS in lyrics_filter.py. The old
        # default of 8 concurrent checks mostly just queued behind each
        # other and helped trip Lyrica's own rate limiter.
        self.assertEqual(lyrics_filter.DEFAULT_MAX_WORKERS, 4)

    @patch("lyrics_filter.lyrica_client.check_lyrics_available")
    def test_submissions_are_staggered_to_avoid_bursting_lyrica(self, mock_check):
        # Checks themselves resolve instantly here, so any measured elapsed
        # time comes purely from the stagger between *submitting* each one -
        # this is what keeps a batch of candidates from opening every
        # connection to Lyrica in the same instant.
        mock_check.return_value = True
        songs = self._songs(4)

        started = time.monotonic()
        kept, _ = filter_candidates_by_lyrics(songs, _song_identity, stagger=0.05)
        elapsed = time.monotonic() - started

        self.assertEqual(len(kept), 4)
        self.assertGreaterEqual(elapsed, 3 * 0.05)


if __name__ == "__main__":
    unittest.main()
