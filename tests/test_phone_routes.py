import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app as app_module  # noqa: E402


class PhoneHomeRouteTestCase(unittest.TestCase):
    def setUp(self):
        app_module.app.testing = True
        self.client = app_module.app.test_client()

    def test_known_code_renders_phone_home(self):
        code = self.client.post("/room/create").get_json()["code"]

        resp = self.client.get(f"/room/{code}")

        self.assertEqual(resp.status_code, 200)
        html = resp.get_data(as_text=True)
        self.assertIn(f'data-code="{code}"', html)
        self.assertIn("/static/phone/home.js", html)

    def test_unknown_code_falls_back_to_join_error_state(self):
        resp = self.client.get("/room/ZZZZ")

        self.assertEqual(resp.status_code, 200)
        html = resp.get_data(as_text=True)
        self.assertIn('data-room-exists="false"', html)

    def test_lowercase_code_is_normalized(self):
        code = self.client.post("/room/create").get_json()["code"]

        resp = self.client.get(f"/room/{code.lower()}")

        html = resp.get_data(as_text=True)
        self.assertIn(f'data-code="{code}"', html)


class PhoneNowSingingRouteTestCase(unittest.TestCase):
    def setUp(self):
        app_module.app.testing = True
        self.client = app_module.app.test_client()

    def test_known_code_renders_now_singing(self):
        code = self.client.post("/room/create").get_json()["code"]

        resp = self.client.get(f"/room/{code}/now-singing")

        self.assertEqual(resp.status_code, 200)
        html = resp.get_data(as_text=True)
        self.assertIn(f'data-code="{code}"', html)
        self.assertIn("/static/phone/now-singing.js", html)

    def test_unknown_code_falls_back_to_join_error_state(self):
        resp = self.client.get("/room/ZZZZ/now-singing")

        self.assertEqual(resp.status_code, 200)
        html = resp.get_data(as_text=True)
        self.assertIn('data-room-exists="false"', html)

    def test_lowercase_code_is_normalized(self):
        code = self.client.post("/room/create").get_json()["code"]

        resp = self.client.get(f"/room/{code.lower()}/now-singing")

        html = resp.get_data(as_text=True)
        self.assertIn(f'data-code="{code}"', html)


if __name__ == "__main__":
    unittest.main()
