import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app as app_module  # noqa: E402


class RoomHttpRoutesTestCase(unittest.TestCase):
    def setUp(self):
        app_module.app.testing = True
        self.client = app_module.app.test_client()

    def test_tv_lobby_renders(self):
        resp = self.client.get("/tv")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("/static/tv/lobby.js", resp.get_data(as_text=True))

    def test_create_room_returns_a_code(self):
        resp = self.client.post("/room/create")
        self.assertEqual(resp.status_code, 200)
        code = resp.get_json()["code"]
        self.assertEqual(len(code), 4)

    def test_room_state_unknown_code_is_404(self):
        resp = self.client.get("/room/ZZZZ/state")
        self.assertEqual(resp.status_code, 404)

    def test_room_state_known_code_returns_snapshot(self):
        code = self.client.post("/room/create").get_json()["code"]
        resp = self.client.get(f"/room/{code}/state")
        self.assertEqual(resp.status_code, 200)
        body = resp.get_json()
        self.assertEqual(body["code"], code)
        self.assertEqual(body["queue"], [])
        self.assertIsNone(body["now_playing"])

    def test_room_qr_unknown_code_is_404(self):
        resp = self.client.get("/room/ZZZZ/qr.svg")
        self.assertEqual(resp.status_code, 404)

    def test_room_qr_known_code_returns_svg_embedding_join_url(self):
        code = self.client.post("/room/create").get_json()["code"]
        with patch.object(app_module, "_detect_lan_ip", return_value="192.168.1.50"):
            resp = self.client.get(f"/room/{code}/qr.svg")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.mimetype, "image/svg+xml")
        svg = resp.get_data(as_text=True)
        self.assertTrue(svg.startswith("<svg"))
        # The literal join URL isn't IN the SVG (it's QR-encoded as pixels),
        # so assert indirectly: the same data through qr_svg() renders
        # identically only if it encoded the URL we expect. Read the port
        # back from _resolve_host_port() rather than assuming the default -
        # this environment may override APP_PORT.
        from core.qrcode_svg import qr_svg

        _, app_port = app_module._resolve_host_port()
        expected = qr_svg(f"http://192.168.1.50:{app_port}/join/{code}")
        self.assertEqual(svg, expected)

    def test_join_room_known_code_renders_join_page(self):
        code = self.client.post("/room/create").get_json()["code"]
        resp = self.client.get(f"/join/{code}")
        self.assertEqual(resp.status_code, 200)
        html = resp.get_data(as_text=True)
        self.assertIn('data-room-exists="true"', html)
        self.assertIn(code, html)

    def test_join_room_unknown_code_still_renders_error_state(self):
        resp = self.client.get("/join/ZZZZ")
        self.assertEqual(resp.status_code, 200)
        html = resp.get_data(as_text=True)
        self.assertIn('data-room-exists="false"', html)

    def test_join_room_lowercases_are_normalized_uppercase(self):
        code = self.client.post("/room/create").get_json()["code"]
        resp = self.client.get(f"/join/{code.lower()}")
        html = resp.get_data(as_text=True)
        self.assertIn('data-room-exists="true"', html)


class DetectLanIpTestCase(unittest.TestCase):
    def test_env_override_takes_precedence(self):
        ip = app_module._detect_lan_ip(env={"APP_LAN_HOST": "10.0.0.5"})
        self.assertEqual(ip, "10.0.0.5")

    def test_falls_back_to_udp_route_lookup(self):
        ip = app_module._detect_lan_ip(env={})
        # Can't assert a specific address portably, but it must look like
        # an IPv4 dotted quad and not be the "nothing routes here" address.
        parts = ip.split(".")
        self.assertEqual(len(parts), 4)
        self.assertTrue(all(part.isdigit() for part in parts))


if __name__ == "__main__":
    unittest.main()
