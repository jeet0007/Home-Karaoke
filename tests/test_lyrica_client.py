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


if __name__ == "__main__":
    unittest.main()
