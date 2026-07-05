import os
import sys
import unittest
from unittest.mock import patch

import httpx

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lyrics import lyrica_client  # noqa: E402


def _response(status_code=200, json_body=None):
    return httpx.Response(status_code, json=json_body if json_body is not None else {})


class CheckLyricsAvailableTestCase(unittest.TestCase):
    @patch("lyrics.lyrica_client.httpx.get")
    def test_returns_true_when_lyrics_present(self, mock_get):
        mock_get.return_value = _response(json_body={"status": "success", "data": {"plain_lyrics": "la la la"}})

        self.assertTrue(lyrica_client.check_lyrics_available("Passenger", "Let Her Go"))

    @patch("lyrics.lyrica_client.httpx.get")
    def test_returns_false_when_status_is_error(self, mock_get):
        mock_get.return_value = _response(json_body={"status": "error", "message": "not found"})

        self.assertFalse(lyrica_client.check_lyrics_available("Nobody", "Nothing"))

    @patch("lyrics.lyrica_client.httpx.get")
    def test_returns_false_when_success_but_empty_data(self, mock_get):
        mock_get.return_value = _response(json_body={"status": "success", "data": {}})

        self.assertFalse(lyrica_client.check_lyrics_available("Nobody", "Nothing"))

    @patch("lyrics.lyrica_client.httpx.get")
    def test_raises_unavailable_on_network_error(self, mock_get):
        mock_get.side_effect = httpx.ConnectError("connection refused")

        with self.assertRaises(lyrica_client.LyricaUnavailableError):
            lyrica_client.check_lyrics_available("Passenger", "Let Her Go")

    @patch("lyrics.lyrica_client.httpx.get")
    def test_raises_unavailable_on_timeout(self, mock_get):
        mock_get.side_effect = httpx.ReadTimeout("timed out")

        with self.assertRaises(lyrica_client.LyricaUnavailableError):
            lyrica_client.check_lyrics_available("Passenger", "Let Her Go")

    @patch("lyrics.lyrica_client.httpx.get")
    def test_raises_unavailable_on_non_200(self, mock_get):
        mock_get.return_value = _response(status_code=500)

        with self.assertRaises(lyrica_client.LyricaUnavailableError):
            lyrica_client.check_lyrics_available("Passenger", "Let Her Go")

    @patch("lyrics.lyrica_client.httpx.get")
    def test_raises_unavailable_on_unparsable_body(self, mock_get):
        bad_response = httpx.Response(200, content=b"not json")
        mock_get.return_value = bad_response

        with self.assertRaises(lyrica_client.LyricaUnavailableError):
            lyrica_client.check_lyrics_available("Passenger", "Let Her Go")

    @patch("lyrics.lyrica_client.httpx.get")
    def test_sends_fast_true_by_default(self, mock_get):
        # Pre-selection availability checks (this function's only caller is
        # lyrics_filter.py, over candidates the user hasn't picked yet) must
        # ask Lyrica for fast=true - LRCLIB+YouTube racing in parallel -
        # instead of falling through Lyrica's default sequential 6-source
        # chain (LRCLIB, YouTube, NetEase, Megalobiz, Musixmatch, SimpMusic).
        mock_get.return_value = _response(json_body={"status": "success", "data": {"plain_lyrics": "la la la"}})

        lyrica_client.check_lyrics_available("Passenger", "Let Her Go")

        _args, kwargs = mock_get.call_args
        self.assertEqual(kwargs["params"]["fast"], "true")

    @patch("lyrics.lyrica_client.httpx.get")
    def test_fast_false_opts_out_of_fast_mode(self, mock_get):
        mock_get.return_value = _response(json_body={"status": "success", "data": {"plain_lyrics": "la la la"}})

        lyrica_client.check_lyrics_available("Passenger", "Let Her Go", fast=False)

        _args, kwargs = mock_get.call_args
        self.assertEqual(kwargs["params"]["fast"], "false")

    @patch("lyrics.lyrica_client.httpx.get")
    def test_sequence_requests_lyrica_pass_mode(self, mock_get):
        # A `sequence` (e.g. "2" for LRCLIB - see fetch_controller.py
        # FETCHER_MAP) requests Lyrica's pass=true&sequence=... mode,
        # restricting the check to just that fetcher.
        mock_get.return_value = _response(json_body={"status": "success", "data": {"plain_lyrics": "la la la"}})

        lyrica_client.check_lyrics_available("Passenger", "Let Her Go", sequence="2")

        _args, kwargs = mock_get.call_args
        self.assertEqual(kwargs["params"]["pass"], "true")
        self.assertEqual(kwargs["params"]["sequence"], "2")
        self.assertNotIn("fast", kwargs["params"])

    @patch("lyrics.lyrica_client.httpx.get")
    def test_sequence_takes_precedence_over_fast(self, mock_get):
        mock_get.return_value = _response(json_body={"status": "success", "data": {"plain_lyrics": "la la la"}})

        lyrica_client.check_lyrics_available("Passenger", "Let Her Go", fast=True, sequence="2")

        _args, kwargs = mock_get.call_args
        self.assertEqual(kwargs["params"]["sequence"], "2")
        self.assertNotIn("fast", kwargs["params"])


class FullLyricsFetchUsesFullChainTestCase(unittest.TestCase):
    """get_lyrics_full()/get_lyrics() are the post-selection fetch for the
    one song the user actually picked - they must NOT set Lyrica's fast=true
    and so keep walking its full multi-source chain for best quality/accuracy,
    unlike the pre-selection check above."""

    @patch("lyrics.lyrica_client.httpx.get")
    def test_get_lyrics_full_does_not_request_fast_mode(self, mock_get):
        mock_get.return_value = _response(json_body={
            "status": "success",
            "data": {"plain_lyrics": "la la la", "source": "lrclib"},
        })

        lyrica_client.get_lyrics_full("Passenger", "Let Her Go")

        _args, kwargs = mock_get.call_args
        self.assertNotIn("fast", kwargs["params"])

class FullFetchRequestsParallelRacingModeTestCase(unittest.TestCase):
    """The bug: get_lyrics_full()/get_lyrics() used to send a plain GET (no
    fast/pass param), which makes Lyrica walk its full synced-lyrics source
    list SEQUENTIALLY with NO per-fetcher timeout at all - LRCLIB alone can
    genuinely take 15-20s+ on its own flaky free server (confirmed via a real
    repro against a live Lyrica instance - see this PR's description), well
    past the old 10s client-side TIMEOUT. That combination made /select-song
    give up and report empty lyrics for songs that actually have them.

    The fix asks Lyrica for pass=true&sequence=... instead, which forces its
    PARALLEL/racing path, and raises the client timeout well above what
    Lyrica itself guarantees server-side."""

    @patch("lyrics.lyrica_client.httpx.get")
    def test_get_lyrics_full_requests_parallel_mode_excluding_musixmatch(self, mock_get):
        mock_get.return_value = _response(json_body={
            "status": "success",
            "data": {"plain_lyrics": "la la la", "source": "lrclib"},
        })

        lyrica_client.get_lyrics_full("Passenger", "Let Her Go")

        _args, kwargs = mock_get.call_args
        self.assertEqual(kwargs["params"]["pass"], "true")
        sequence_ids = {int(x) for x in kwargs["params"]["sequence"].split(",")}
        # LRCLIB, YouTube, NetEase, Megalobiz, SimpMusic - Musixmatch (id 6)
        # deliberately excluded (unauthenticated/unreliable - see
        # lyrica_client.py module docstring for the full justification).
        self.assertEqual(sequence_ids, {2, 3, 4, 5, 7})

    @patch("lyrics.lyrica_client.httpx.get")
    def test_client_timeout_comfortably_exceeds_lyricas_server_side_bound(self, mock_get):
        mock_get.return_value = _response(json_body={
            "status": "success",
            "data": {"plain_lyrics": "la la la", "source": "lrclib"},
        })

        lyrica_client.get_lyrics_full("Passenger", "Let Her Go")

        _args, kwargs = mock_get.call_args
        # Lyrica's outer run_async(fetch_lyrics_controller(...), timeout=60)
        # (router.py) is the real, always-enforced bound - unlike the
        # per-fetcher 12s figure, which real-repro testing showed does NOT
        # reliably bound a slow synchronous fetcher (LRCLIB) that blocks
        # Lyrica's asyncio event loop. Our client timeout must clear the
        # authoritative 60s bound, not just the aspirational 12s one.
        self.assertGreater(kwargs["timeout"], 60)

    @patch("lyrics.lyrica_client.httpx.get")
    def test_slow_lyrics_fetch_returns_real_lyrics_instead_of_a_premature_timeout(self, mock_get):
        """Stands in for the real repro (see PR description): LRCLIB taking
        36s to respond with real lyrics for a legitimately slow lookup. The
        old TIMEOUT=10.0 would have raised httpx.ReadTimeout well before this
        response ever arrived, and get_lyrics_full would have silently
        returned None - indistinguishable from a confirmed "no lyrics"
        result. The fixed client timeout must be large enough to let a
        response this slow through instead of giving up early."""

        def slow_but_real_response(*_args, **kwargs):
            if kwargs.get("timeout", 0) < 36:
                raise httpx.ReadTimeout("simulated slow LRCLIB response")
            return _response(json_body={
                "status": "success",
                "data": {"plain_lyrics": "real lyrics found late", "source": "lrclib"},
            })

        mock_get.side_effect = slow_but_real_response

        result = lyrica_client.get_lyrics_full("Olivia Rodrigo", "happier")

        self.assertIsNotNone(result)
        self.assertEqual(result["plain"], "real lyrics found late")

    @patch("lyrics.lyrica_client.httpx.get")
    def test_timeout_that_still_exceeds_the_client_bound_reports_no_lyrics_not_a_crash(self, mock_get):
        # The genuine-timeout residual case: even the raised client timeout
        # eventually gets exceeded (e.g. Lyrica itself is down/overloaded).
        # get_lyrics_full must fail closed to None (the existing "no lyrics"
        # UI state), not raise, so /select-song still returns a normal
        # response instead of a 500.
        mock_get.side_effect = httpx.ReadTimeout("still too slow")

        result = lyrica_client.get_lyrics_full("Olivia Rodrigo", "happier")

        self.assertIsNone(result)


if __name__ == "__main__":
    unittest.main()
