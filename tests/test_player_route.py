import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app as app_module  # noqa: E402


class PlayerRouteTestCase(unittest.TestCase):
    def setUp(self):
        app_module.app.testing = True
        self.client = app_module.app.test_client()

    def test_player_embeds_clean_song_identity_for_select_song_fetch(self):
        resp = self.client.get("/player?title=Let+Her+Go&artist=Passenger&duration=253")

        self.assertEqual(resp.status_code, 200)
        html = resp.get_data(as_text=True)
        # static/player/main.js's loadSong() fetches /select-song using
        # window.PLAYER_CONFIG's exact fields, so whatever identity /player
        # is called with is what /select-song ends up queried with.
        self.assertIn('title: "Let Her Go"', html)
        self.assertIn('artist: "Passenger"', html)
        self.assertIn('duration: "253"', html)

    def test_player_forwards_ytmusic_video_id_for_melody_extraction(self):
        resp = self.client.get("/player?title=X&artist=Y&ytm=abc12345678")
        html = resp.get_data(as_text=True)
        self.assertIn('ytmusicVideoId: "abc12345678"', html)


if __name__ == "__main__":
    unittest.main()
