import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx  # noqa: E402

from lyrics import lrclib_client  # noqa: E402
from lyrics import lyrics_sources  # noqa: E402


class ParseLrcTestCase(unittest.TestCase):
    def test_basic_lines(self):
        lrc = "[00:12.50] Hello world\n[01:02.03] Second line"
        parsed = lrclib_client.parse_lrc(lrc)
        self.assertEqual(
            parsed,
            [
                {"time_ms": 12500, "text": "Hello world"},
                {"time_ms": 62030, "text": "Second line"},
            ],
        )

    def test_metadata_tags_and_blank_text_skipped(self):
        lrc = "[ar: Artist]\n[00:05.00]\n[00:10.00] Real line"
        parsed = lrclib_client.parse_lrc(lrc)
        self.assertEqual(parsed, [{"time_ms": 10000, "text": "Real line"}])

    def test_multiple_timestamps_on_one_line(self):
        lrc = "[00:10.00][00:50.00] Chorus"
        parsed = lrclib_client.parse_lrc(lrc)
        self.assertEqual([e["time_ms"] for e in parsed], [10000, 50000])
        self.assertTrue(all(e["text"] == "Chorus" for e in parsed))

    def test_two_digit_fraction_is_centiseconds(self):
        parsed = lrclib_client.parse_lrc("[00:01.5] x\n[00:02.25] y")
        self.assertEqual([e["time_ms"] for e in parsed], [1500, 2250])

    def test_empty_input(self):
        self.assertEqual(lrclib_client.parse_lrc(""), [])
        self.assertEqual(lrclib_client.parse_lrc(None), [])


def _response(status_code=200, json_payload=None):
    response = mock.Mock()
    response.status_code = status_code
    if json_payload is None:
        response.json.side_effect = ValueError("no json")
    else:
        response.json.return_value = json_payload
    return response


class LrclibGetLyricsTestCase(unittest.TestCase):
    def test_synced_result(self):
        payload = {
            "syncedLyrics": "[00:01.00] one\n[00:02.00] two",
            "plainLyrics": "one\ntwo",
        }
        with mock.patch.object(lrclib_client.httpx, "get", return_value=_response(200, payload)) as get:
            result = lrclib_client.get_lyrics_full("Artist", "Title", duration=200)

        self.assertEqual(len(result["synced"]), 2)
        self.assertEqual(result["plain"], "one\ntwo")
        self.assertEqual(result["source"], "lrclib-direct")
        params = get.call_args.kwargs["params"]
        self.assertEqual(params["artist_name"], "Artist")
        self.assertEqual(params["duration"], 200)

    def test_plain_only_result(self):
        payload = {"syncedLyrics": None, "plainLyrics": "just words"}
        with mock.patch.object(lrclib_client.httpx, "get", return_value=_response(200, payload)):
            result = lrclib_client.get_lyrics_full("Artist", "Title")
        self.assertEqual(result["synced"], [])
        self.assertEqual(result["plain"], "just words")

    def test_404_returns_none(self):
        with mock.patch.object(lrclib_client.httpx, "get", return_value=_response(404, {})):
            self.assertIsNone(lrclib_client.get_lyrics_full("A", "T"))

    def test_network_error_returns_none(self):
        with mock.patch.object(lrclib_client.httpx, "get", side_effect=httpx.ConnectError("boom")):
            self.assertIsNone(lrclib_client.get_lyrics_full("A", "T"))

    def test_empty_payload_returns_none(self):
        with mock.patch.object(lrclib_client.httpx, "get", return_value=_response(200, {})):
            self.assertIsNone(lrclib_client.get_lyrics_full("A", "T"))


SYNCED = [{"time_ms": 0, "text": "hi"}]


class LyricsSourcesTestCase(unittest.TestCase):
    def test_lyrica_synced_wins(self):
        lyrica = {"synced": SYNCED, "plain": "hi", "source": "lrclib"}
        with mock.patch.object(lyrics_sources.lyrica_client, "get_lyrics_full", return_value=lyrica), mock.patch.object(
            lyrics_sources.lrclib_client, "get_lyrics_full"
        ) as lrclib:
            result = lyrics_sources.get_lyrics_full("A", "T")
        self.assertEqual(result["source"], "lrclib")
        lrclib.assert_not_called()

    def test_falls_back_to_lrclib_when_lyrica_misses(self):
        lrclib = {"synced": SYNCED, "plain": "hi", "source": "lrclib-direct"}
        with mock.patch.object(lyrics_sources.lyrica_client, "get_lyrics_full", return_value=None), mock.patch.object(
            lyrics_sources.lrclib_client, "get_lyrics_full", return_value=lrclib
        ):
            result = lyrics_sources.get_lyrics_full("A", "T")
        self.assertEqual(result["source"], "lrclib-direct")

    def test_falls_back_when_lyrica_raises(self):
        lrclib = {"synced": SYNCED, "plain": "hi", "source": "lrclib-direct"}
        with mock.patch.object(
            lyrics_sources.lyrica_client, "get_lyrics_full", side_effect=RuntimeError("down")
        ), mock.patch.object(lyrics_sources.lrclib_client, "get_lyrics_full", return_value=lrclib):
            result = lyrics_sources.get_lyrics_full("A", "T")
        self.assertEqual(result["source"], "lrclib-direct")

    def test_lrclib_synced_beats_lyrica_plain_only(self):
        lyrica = {"synced": [], "plain": "plain words", "source": "netease"}
        lrclib = {"synced": SYNCED, "plain": "hi", "source": "lrclib-direct"}
        with mock.patch.object(lyrics_sources.lyrica_client, "get_lyrics_full", return_value=lyrica), mock.patch.object(
            lyrics_sources.lrclib_client, "get_lyrics_full", return_value=lrclib
        ):
            result = lyrics_sources.get_lyrics_full("A", "T")
        self.assertEqual(result["source"], "lrclib-direct")

    def test_lyrica_plain_only_used_when_lrclib_misses(self):
        lyrica = {"synced": [], "plain": "plain words", "source": "netease"}
        with mock.patch.object(lyrics_sources.lyrica_client, "get_lyrics_full", return_value=lyrica), mock.patch.object(
            lyrics_sources.lrclib_client, "get_lyrics_full", return_value=None
        ):
            result = lyrics_sources.get_lyrics_full("A", "T")
        self.assertEqual(result["source"], "netease")

    def test_both_missing_returns_none(self):
        with mock.patch.object(lyrics_sources.lyrica_client, "get_lyrics_full", return_value=None), mock.patch.object(
            lyrics_sources.lrclib_client, "get_lyrics_full", return_value=None
        ):
            self.assertIsNone(lyrics_sources.get_lyrics_full("A", "T"))


if __name__ == "__main__":
    unittest.main()
