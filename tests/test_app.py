import os
import sys
import time
import unittest
from unittest.mock import MagicMock, patch

import httpx

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app as app_module  # noqa: E402

VIDEO_ID = "dQw4w9WgXcQ"


class AppConfigTestCase(unittest.TestCase):
    def test_resolve_host_port_defaults(self):
        self.assertEqual(app_module._resolve_host_port({}), ("127.0.0.1", 3000))

    def test_resolve_host_port_uses_env_values(self):
        env = {"APP_HOST": "0.0.0.0", "APP_PORT": "5050"}

        self.assertEqual(app_module._resolve_host_port(env), ("0.0.0.0", 5050))

    def test_resolve_host_port_rejects_non_numeric_port(self):
        with self.assertRaisesRegex(ValueError, "Invalid APP_PORT='abc'"):
            app_module._resolve_host_port({"APP_PORT": "abc"})

    def test_resolve_host_port_rejects_out_of_range_port(self):
        with self.assertRaisesRegex(ValueError, "Invalid APP_PORT=70000"):
            app_module._resolve_host_port({"APP_PORT": "70000"})


class StreamProxyTestCase(unittest.TestCase):
    def setUp(self):
        app_module.app.testing = True
        self.client = app_module.app.test_client()
        app_module._STREAM_CACHE.clear()
        self.binary_patch = patch("app.os.path.isfile", return_value=True)
        self.binary_patch.start()
        self.addCleanup(self.binary_patch.stop)
        self.addCleanup(app_module._STREAM_CACHE.clear)

    def _fake_upstream_url(self, expire_offset=3600):
        return f"https://rr1---example.googlevideo.com/videoplayback?expire={int(time.time()) + expire_offset}&itag=18"

    # -- /stream-url --------------------------------------------------

    def test_stream_url_missing_video_id(self):
        resp = self.client.get("/stream-url")
        self.assertEqual(resp.status_code, 400)

    def test_stream_url_invalid_video_id(self):
        resp = self.client.get("/stream-url?video_id=not-valid")
        self.assertEqual(resp.status_code, 400)

    @patch("app._resolve_stream_urls")
    def test_stream_url_returns_same_origin_proxy_path(self, mock_resolve):
        mock_resolve.return_value = [self._fake_upstream_url()]

        resp = self.client.get(f"/stream-url?video_id={VIDEO_ID}")

        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertEqual(data["stream_url"], f"/stream-proxy/{VIDEO_ID}")
        self.assertNotIn("googlevideo.com", data["stream_url"])

    @patch("app._resolve_stream_urls")
    def test_stream_url_surfaces_ytdlp_failure(self, mock_resolve):
        mock_resolve.side_effect = RuntimeError("yt-dlp failed: video unavailable")

        resp = self.client.get(f"/stream-url?video_id={VIDEO_ID}")

        self.assertEqual(resp.status_code, 502)
        self.assertIn("video unavailable", resp.get_json()["error"])

    # -- /stream-proxy --------------------------------------------------

    def test_stream_proxy_invalid_video_id(self):
        resp = self.client.get("/stream-proxy/not-valid")
        self.assertEqual(resp.status_code, 400)

    @patch("app._http_client.send")
    @patch("app._resolve_stream_urls")
    def test_stream_proxy_streams_bytes_same_origin(self, mock_resolve, mock_send):
        mock_resolve.return_value = [self._fake_upstream_url()]
        mock_send.return_value = httpx.Response(
            200,
            headers={"Content-Type": "video/mp4", "Content-Length": "9", "Accept-Ranges": "bytes"},
            content=b"videobyte",
        )

        resp = self.client.get(f"/stream-proxy/{VIDEO_ID}")

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data, b"videobyte")
        self.assertEqual(resp.headers["Content-Type"], "video/mp4")
        self.assertEqual(resp.headers["Access-Control-Allow-Origin"], "*")

    @patch("app._http_client.send")
    @patch("app._resolve_stream_urls")
    def test_stream_proxy_forwards_range_header_and_206(self, mock_resolve, mock_send):
        mock_resolve.return_value = [self._fake_upstream_url()]
        mock_send.return_value = httpx.Response(
            206,
            headers={"Content-Type": "video/mp4", "Content-Range": "bytes 0-3/9", "Content-Length": "4"},
            content=b"vide",
        )

        resp = self.client.get(f"/stream-proxy/{VIDEO_ID}", headers={"Range": "bytes=0-3"})

        self.assertEqual(resp.status_code, 206)
        self.assertEqual(resp.data, b"vide")
        self.assertEqual(resp.headers["Content-Range"], "bytes 0-3/9")

        sent_request = mock_send.call_args[0][0]
        self.assertEqual(sent_request.headers["Range"], "bytes=0-3")

    @patch("app._http_client.send")
    @patch("app._resolve_stream_urls")
    def test_stream_proxy_retries_once_on_403(self, mock_resolve, mock_send):
        mock_resolve.side_effect = [
            [self._fake_upstream_url()],
            [self._fake_upstream_url()],
        ]
        mock_send.side_effect = [
            httpx.Response(403, content=b""),
            httpx.Response(200, headers={"Content-Type": "video/mp4"}, content=b"ok"),
        ]

        resp = self.client.get(f"/stream-proxy/{VIDEO_ID}")

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data, b"ok")
        self.assertEqual(mock_resolve.call_count, 2)

    @patch("app._http_client.send")
    @patch("app._resolve_stream_urls")
    def test_stream_proxy_propagates_persistent_upstream_error(self, mock_resolve, mock_send):
        mock_resolve.return_value = [self._fake_upstream_url()]
        mock_send.return_value = httpx.Response(500, content=b"")

        resp = self.client.get(f"/stream-proxy/{VIDEO_ID}")

        self.assertEqual(resp.status_code, 502)

    # -- caching --------------------------------------------------

    @patch("app._resolve_stream_urls")
    def test_upstream_url_is_cached_between_calls(self, mock_resolve):
        mock_resolve.return_value = [self._fake_upstream_url()]

        url1, _ = app_module._get_upstream_stream_url(VIDEO_ID)
        url2, _ = app_module._get_upstream_stream_url(VIDEO_ID)

        self.assertEqual(url1, url2)
        mock_resolve.assert_called_once()

    @patch("app._resolve_stream_urls")
    def test_upstream_url_refreshed_when_near_expiry(self, mock_resolve):
        mock_resolve.side_effect = [
            [self._fake_upstream_url(expire_offset=5)],
            [self._fake_upstream_url(expire_offset=3600)],
        ]

        app_module._get_upstream_stream_url(VIDEO_ID)
        app_module._get_upstream_stream_url(VIDEO_ID)

        self.assertEqual(mock_resolve.call_count, 2)


if __name__ == "__main__":
    unittest.main()
