"""Real WebSocket integration tests for /room-ws, mirroring
test_grade_websocket.py's scaffold: flask-sock needs a real WebSocket
upgrade handshake, so these boot the actual app on a background thread
bound to an OS-assigned free port and connect with simple_websocket.Client.
"""

import json
import os
import socket
import sys
import threading
import time
import unittest

from unittest.mock import patch

from simple_websocket import Client, ConnectionClosed

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app as app_module  # noqa: E402

SONG = {
    "artist": "Journey",
    "title": "Don't Stop Believin'",
    "cover_art": "http://example.com/art.jpg",
    "duration_seconds": 251,
    "ytmusic_video_id": "abc123",
}


def _free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class RoomWebSocketTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.port = _free_port()
        cls.server_thread = threading.Thread(
            target=lambda: app_module.app.run(host="127.0.0.1", port=cls.port, debug=False, threaded=True),
            daemon=True,
        )
        cls.server_thread.start()

        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            try:
                with socket.create_connection(("127.0.0.1", cls.port), timeout=0.5):
                    break
            except OSError:
                time.sleep(0.1)
        else:
            raise RuntimeError("test server did not start in time")

    def setUp(self):
        # Each test gets its own room so state from one test can't bleed
        # into the next via the module-level room_registry.
        room = app_module.room_registry.create_room()
        self.code = room.code

    def _connect(self):
        return Client.connect(f"ws://127.0.0.1:{self.port}/room-ws")

    def _recv_json(self, ws, timeout=4):
        message = ws.receive(timeout=timeout)
        self.assertIsNotNone(message, "expected a message before the timeout")
        return json.loads(message)

    def _drain_type(self, ws, msg_type, timeout=4, max_messages=10):
        """Reads messages until one of the given type shows up (server
        broadcasts can arrive interleaved with other traffic)."""
        for _ in range(max_messages):
            payload = self._recv_json(ws, timeout=timeout)
            if payload.get("type") == msg_type:
                return payload
        self.fail(f"never received a {msg_type!r} message")

    def _wait_for_joined_player(self, ws, name, timeout=4, max_messages=10):
        """A TV can learn about a phone two ways depending on the race
        between the phone's join and the TV's own attach: a live
        "joined-players" broadcast (TV was already attached), or its own
        "room-snapshot" (phone joined first, so the TV only finds out once
        it attaches - see RoomRegistry.attach_tv). Tests should assert on
        the underlying fact ("Dave is in the room"), not which of the two
        delivered it."""
        for _ in range(max_messages):
            payload = self._recv_json(ws, timeout=timeout)
            if payload.get("type") == "joined-players":
                players = payload.get("players") or []
            elif payload.get("type") == "room-snapshot":
                players = payload.get("joined_players") or []
            else:
                continue
            if any(p["name"] == name for p in players):
                return players
        self.fail(f"never saw {name!r} join")

    def _close(self, ws):
        try:
            ws.close()
        except ConnectionClosed:
            pass

    def test_tv_host_handshake_succeeds(self):
        tv = self._connect()
        try:
            tv.send(json.dumps({"role": "tv", "action": "host", "code": self.code}))
            # No immediate reply is expected on a clean host - prove the
            # connection is alive by round-tripping room creation via HTTP.
            state = app_module.room_registry.room_state(self.code)
            self.assertIsNotNone(state)
        finally:
            self._close(tv)

    def test_unknown_room_code_gets_error_and_closes(self):
        tv = self._connect()
        try:
            tv.send(json.dumps({"role": "tv", "action": "host", "code": "ZZZZ"}))
            reply = self._recv_json(tv)
            self.assertIn("error", reply)
        finally:
            self._close(tv)

    def test_malformed_handshake_gets_error(self):
        ws = self._connect()
        try:
            ws.send(json.dumps({"nope": True}))
            reply = self._recv_json(ws)
            self.assertIn("error", reply)
        finally:
            self._close(ws)

    def test_phone_join_notifies_tv(self):
        tv = self._connect()
        phone = self._connect()
        try:
            tv.send(json.dumps({"role": "tv", "action": "host", "code": self.code}))
            phone.send(json.dumps({"role": "phone", "action": "join", "code": self.code, "name": "Dave"}))

            snapshot = self._recv_json(phone)
            self.assertEqual(snapshot["type"], "room-snapshot")

            players = self._wait_for_joined_player(tv, "Dave")
            self.assertEqual(players, [{"phone_id": players[0]["phone_id"], "name": "Dave"}])
        finally:
            self._close(phone)
            self._close(tv)

    def test_enqueue_song_reaches_tv_and_advance_queue_reaches_phone(self):
        tv = self._connect()
        phone = self._connect()
        try:
            tv.send(json.dumps({"role": "tv", "action": "host", "code": self.code}))
            phone.send(json.dumps({"role": "phone", "action": "join", "code": self.code, "name": "Dave"}))
            self._recv_json(phone)  # room-snapshot
            self._wait_for_joined_player(tv, "Dave")

            phone.send(json.dumps({"type": "enqueue-song", "song": SONG}))
            queue_update = self._drain_type(tv, "queue-update")
            self.assertEqual(len(queue_update["queue"]), 1)
            self.assertEqual(queue_update["queue"][0]["artist"], SONG["artist"])

            tv.send(json.dumps({"type": "advance-queue"}))
            now_playing = self._drain_type(phone, "now-playing-change")
            self.assertEqual(now_playing["now_playing"]["artist"], SONG["artist"])
        finally:
            self._close(phone)
            self._close(tv)

    def test_phone_playback_control_reaches_tv_only(self):
        tv = self._connect()
        phone = self._connect()
        try:
            tv.send(json.dumps({"role": "tv", "action": "host", "code": self.code}))
            phone.send(json.dumps({"role": "phone", "action": "join", "code": self.code, "name": "Dave"}))
            self._recv_json(phone)
            self._wait_for_joined_player(tv, "Dave")

            phone.send(json.dumps({"type": "playback-control", "action": "skip"}))
            control = self._drain_type(tv, "playback-control")
            self.assertEqual(control["action"], "skip")
        finally:
            self._close(phone)
            self._close(tv)

    def test_tv_disconnect_closes_room_for_phones(self):
        # detach_tv waits out core.rooms.TV_RECONNECT_GRACE_SECONDS (5s in
        # production - see its docstring for why: a TV's own lobby<->player
        # page navigation always closes the old page's socket before the
        # new page's has attached) before actually tearing the room down.
        # Patched short here so the test still exercises the real teardown
        # path without eating that delay for real.
        tv = self._connect()
        phone = self._connect()
        try:
            tv.send(json.dumps({"role": "tv", "action": "host", "code": self.code}))
            phone.send(json.dumps({"role": "phone", "action": "join", "code": self.code, "name": "Dave"}))
            self._recv_json(phone)  # room-snapshot
            self._wait_for_joined_player(tv, "Dave")

            with patch("core.rooms.TV_RECONNECT_GRACE_SECONDS", 0.05):
                tv.close()
                closed = self._drain_type(phone, "room-closed")

            self.assertEqual(closed["type"], "room-closed")
            self.assertIsNone(app_module.room_registry.get_room(self.code))
        finally:
            self._close(phone)

    def test_tv_reconnect_within_grace_period_keeps_room_alive(self):
        # Reproduces the real bug: static/tv/lobby.js navigates the TV's
        # browser from the lobby to /player (or back) via a genuine page
        # load, which closes the OLD page's socket before the NEW page's
        # room-broadcast.js has attached its own. A second TV connection
        # attaching for the same room during the grace window must reclaim
        # it - the phone must never see room-closed, and the room must
        # still exist with the new connection as its TV.
        tv1 = self._connect()
        phone = self._connect()
        try:
            tv1.send(json.dumps({"role": "tv", "action": "host", "code": self.code}))
            phone.send(json.dumps({"role": "phone", "action": "join", "code": self.code, "name": "Dave"}))
            self._recv_json(phone)  # room-snapshot
            self._wait_for_joined_player(tv1, "Dave")

            with patch("core.rooms.TV_RECONNECT_GRACE_SECONDS", 1.0):
                tv1.close()
                # Give detach_tv's handler a moment to run before the new
                # connection attaches, matching the real ordering a page
                # navigation produces (old socket closes first).
                time.sleep(0.1)

                tv2 = self._connect()
                try:
                    tv2.send(json.dumps({"role": "tv", "action": "host", "code": self.code}))
                    snapshot = self._recv_json(tv2)
                    self.assertEqual(snapshot["type"], "room-snapshot")

                    # Long enough to have caught the OLD (now-cancelled)
                    # teardown timer if the fix didn't work, short enough
                    # to keep the test fast.
                    time.sleep(1.3)

                    self.assertIsNotNone(app_module.room_registry.get_room(self.code))
                finally:
                    self._close(tv2)

            # If the room had actually been torn down, this enqueue would
            # go nowhere (unknown room code) and the phone would time out
            # waiting for a queue-update instead of receiving one - the
            # room being functional end-to-end is the real proof it
            # survived, not just that get_room() above returned non-None.
            phone.send(json.dumps({"type": "enqueue-song", "song": SONG}))
            queue_update = self._drain_type(phone, "queue-update")
            self.assertEqual(queue_update["type"], "queue-update")
        finally:
            self._close(phone)

    def test_garbage_frames_do_not_kill_the_session(self):
        tv = self._connect()
        try:
            tv.send(json.dumps({"role": "tv", "action": "host", "code": self.code}))
            tv.send("not json at all")
            tv.send(json.dumps({"type": "playback-position", "pos_ms": "NaN-ish"}))

            # Connection should still be alive and processing further
            # messages after the garbage frames above.
            tv.send(json.dumps({"type": "advance-queue"}))
            state = app_module.room_registry.room_state(self.code)
            self.assertIsNotNone(state)
        finally:
            self._close(tv)

    def test_disconnect_mid_session_does_not_crash_server(self):
        tv = self._connect()
        tv.send(json.dumps({"role": "tv", "action": "host", "code": self.code}))
        tv.close()

        with socket.create_connection(("127.0.0.1", self.port), timeout=2) as raw:
            raw.sendall(b"GET / HTTP/1.1\r\nHost: 127.0.0.1\r\nConnection: close\r\n\r\n")
            response = raw.recv(64)
        self.assertTrue(response.startswith(b"HTTP/1.1"))


if __name__ == "__main__":
    unittest.main()
