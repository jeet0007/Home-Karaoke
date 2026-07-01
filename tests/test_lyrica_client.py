import os
import sys
import unittest
from unittest.mock import patch

import httpx

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import lyrica_client  # noqa: E402


def _response(status_code=200, json_body=None):
    return httpx.Response(status_code, json=json_body if json_body is not None else {})


class CheckLyricsAvailableTestCase(unittest.TestCase):
    @patch("lyrica_client.httpx.get")
    def test_returns_true_when_lyrics_present(self, mock_get):
        mock_get.return_value = _response(json_body={"status": "success", "data": {"plain_lyrics": "la la la"}})

        self.assertTrue(lyrica_client.check_lyrics_available("Passenger", "Let Her Go"))

    @patch("lyrica_client.httpx.get")
    def test_returns_false_when_status_is_error(self, mock_get):
        mock_get.return_value = _response(json_body={"status": "error", "message": "not found"})

        self.assertFalse(lyrica_client.check_lyrics_available("Nobody", "Nothing"))

    @patch("lyrica_client.httpx.get")
    def test_returns_false_when_success_but_empty_data(self, mock_get):
        mock_get.return_value = _response(json_body={"status": "success", "data": {}})

        self.assertFalse(lyrica_client.check_lyrics_available("Nobody", "Nothing"))

    @patch("lyrica_client.httpx.get")
    def test_raises_unavailable_on_network_error(self, mock_get):
        mock_get.side_effect = httpx.ConnectError("connection refused")

        with self.assertRaises(lyrica_client.LyricaUnavailableError):
            lyrica_client.check_lyrics_available("Passenger", "Let Her Go")

    @patch("lyrica_client.httpx.get")
    def test_raises_unavailable_on_timeout(self, mock_get):
        mock_get.side_effect = httpx.ReadTimeout("timed out")

        with self.assertRaises(lyrica_client.LyricaUnavailableError):
            lyrica_client.check_lyrics_available("Passenger", "Let Her Go")

    @patch("lyrica_client.httpx.get")
    def test_raises_unavailable_on_non_200(self, mock_get):
        mock_get.return_value = _response(status_code=500)

        with self.assertRaises(lyrica_client.LyricaUnavailableError):
            lyrica_client.check_lyrics_available("Passenger", "Let Her Go")

    @patch("lyrica_client.httpx.get")
    def test_raises_unavailable_on_unparsable_body(self, mock_get):
        bad_response = httpx.Response(200, content=b"not json")
        mock_get.return_value = bad_response

        with self.assertRaises(lyrica_client.LyricaUnavailableError):
            lyrica_client.check_lyrics_available("Passenger", "Let Her Go")

    @patch("lyrica_client.httpx.get")
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

    @patch("lyrica_client.httpx.get")
    def test_fast_false_opts_out_of_fast_mode(self, mock_get):
        mock_get.return_value = _response(json_body={"status": "success", "data": {"plain_lyrics": "la la la"}})

        lyrica_client.check_lyrics_available("Passenger", "Let Her Go", fast=False)

        _args, kwargs = mock_get.call_args
        self.assertEqual(kwargs["params"]["fast"], "false")


class FullLyricsFetchUsesFullChainTestCase(unittest.TestCase):
    """get_lyrics_full()/get_lyrics() are the post-selection fetch for the
    one song the user actually picked - they must NOT set Lyrica's fast=true
    and so keep walking its full multi-source chain for best quality/accuracy,
    unlike the pre-selection check above."""

    @patch("lyrica_client.httpx.get")
    def test_get_lyrics_full_does_not_request_fast_mode(self, mock_get):
        mock_get.return_value = _response(json_body={
            "status": "success",
            "data": {"plain_lyrics": "la la la", "source": "lrclib"},
        })

        lyrica_client.get_lyrics_full("Passenger", "Let Her Go")

        _args, kwargs = mock_get.call_args
        self.assertNotIn("fast", kwargs["params"])

    @patch("lyrica_client.httpx.get")
    def test_get_lyrics_does_not_request_fast_mode(self, mock_get):
        mock_get.return_value = _response(json_body={
            "status": "success",
            "data": {"timed_lyrics": [{"start_time": 0, "text": "la"}]},
        })

        lyrica_client.get_lyrics("Passenger", "Let Her Go")

        _args, kwargs = mock_get.call_args
        self.assertNotIn("fast", kwargs["params"])


if __name__ == "__main__":
    unittest.main()
