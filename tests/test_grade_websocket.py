"""Real WebSocket integration tests for /grade.

flask-sock's test support doesn't run through Flask's test_client (it needs
a real WebSocket upgrade handshake), so these tests boot the actual app on a
background thread bound to an OS-assigned free port and connect with
simple_websocket.Client - the same library flask-sock uses on the server
side, already a project dependency, so this needs nothing extra installed.
"""

import json
import os
import socket
import sys
import threading
import time
import unittest

import numpy as np
from simple_websocket import Client, ConnectionClosed

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app as app_module  # noqa: E402

SAMPLE_RATE = 44100


def _free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _sine(freq, seconds, amp=0.5):
    t = np.arange(int(SAMPLE_RATE * seconds)) / SAMPLE_RATE
    return (amp * np.sin(2 * np.pi * freq * t)).astype(np.float32)


class GradeWebSocketTestCase(unittest.TestCase):
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

    def _connect(self):
        return Client.connect(f"ws://127.0.0.1:{self.port}/grade")

    def _grade_samples(self, samples, chunk_ms=20):
        ws = self._connect()
        try:
            ws.send(json.dumps({"sample_rate": SAMPLE_RATE}))
            chunk_n = int(SAMPLE_RATE * chunk_ms / 1000)
            updates = []
            for start in range(0, len(samples), chunk_n):
                chunk = samples[start : start + chunk_n]
                ws.send(chunk.tobytes())
            # Give the server a moment to process the final chunks, then
            # drain whatever score updates it already produced.
            while True:
                try:
                    message = ws.receive(timeout=1)
                except Exception:
                    break
                if message is None:
                    break
                updates.append(json.loads(message))
        finally:
            try:
                ws.close()
            except ConnectionClosed:
                pass
        return updates

    def test_steady_tone_produces_high_score_updates(self):
        updates = self._grade_samples(_sine(220.0, seconds=2.0))

        self.assertTrue(updates, "expected at least one score update over the socket")
        for update in updates:
            self.assertIn("score", update)
            self.assertIn("singing", update)
            self.assertTrue(update["singing"])

        self.assertGreaterEqual(updates[-1]["score"], 80)
        self.assertAlmostEqual(updates[-1]["frequency_hz"], 220.0, delta=5.0)

    def test_silence_produces_low_no_singing_updates(self):
        rng = np.random.default_rng(0)
        samples = rng.normal(0, 0.0005, int(SAMPLE_RATE * 1.5)).astype(np.float32)

        updates = self._grade_samples(samples)

        self.assertTrue(updates)
        for update in updates:
            self.assertFalse(update["singing"])
            self.assertEqual(update["score"], 0)

    def test_rejects_missing_handshake_sample_rate(self):
        ws = self._connect()
        try:
            ws.send(json.dumps({"nope": True}))
            reply = json.loads(ws.receive(timeout=2))
            self.assertIn("error", reply)
        finally:
            # The server already closes the connection after sending the
            # error reply, so the client-side close is a no-op that raises
            # ConnectionClosed - expected here, not a test failure.
            try:
                ws.close()
            except ConnectionClosed:
                pass

    def _grade_with_melody(self, samples, melody, pos_ms=0, chunk_ms=20):
        """Same as _grade_samples but with a reference melody in the
        handshake and a position sync before the PCM stream."""
        ws = self._connect()
        try:
            ws.send(json.dumps({"sample_rate": SAMPLE_RATE, "melody": melody}))
            ws.send(json.dumps({"pos_ms": pos_ms}))
            chunk_n = int(SAMPLE_RATE * chunk_ms / 1000)
            updates = []
            for start in range(0, len(samples), chunk_n):
                ws.send(samples[start : start + chunk_n].tobytes())
            while True:
                try:
                    message = ws.receive(timeout=1)
                except Exception:
                    break
                if message is None:
                    break
                updates.append(json.loads(message))
        finally:
            try:
                ws.close()
            except ConnectionClosed:
                pass
        return updates

    def test_melody_handshake_enables_reference_grading(self):
        melody = [{"start_ms": 0, "end_ms": 60000, "midi": 57}]  # A3 throughout

        on_pitch = self._grade_with_melody(_sine(220.0, seconds=2.0), melody)  # A3
        off_pitch = self._grade_with_melody(_sine(311.1, seconds=2.0), melody)  # D#4, tritone off

        self.assertTrue(on_pitch and off_pitch)
        self.assertEqual(on_pitch[-1]["target_midi"], 57)
        self.assertGreaterEqual(on_pitch[-1]["score"], 80)
        self.assertGreater(on_pitch[-1]["score"], off_pitch[-1]["score"] + 30)

    def test_garbage_melody_and_sync_frames_do_not_kill_the_session(self):
        ws = self._connect()
        try:
            ws.send(json.dumps({"sample_rate": SAMPLE_RATE, "melody": "not-a-list"}))
            ws.send("not json at all")
            ws.send(json.dumps({"pos_ms": "NaN-ish"}))
            ws.send(_sine(220.0, seconds=0.5).tobytes())
            message = ws.receive(timeout=2)
            self.assertIsNotNone(message)
            update = json.loads(message)
            self.assertIn("score", update)
            self.assertIsNone(update["target_midi"])
        finally:
            try:
                ws.close()
            except ConnectionClosed:
                pass

    def test_disconnect_mid_stream_does_not_crash_server(self):
        ws = self._connect()
        ws.send(json.dumps({"sample_rate": SAMPLE_RATE}))
        ws.send(_sine(220.0, seconds=0.1).tobytes())
        ws.close()

        # The server (and its dev-reloader-free thread) should still be
        # answering ordinary HTTP requests after an abrupt WS disconnect.
        with socket.create_connection(("127.0.0.1", self.port), timeout=2) as sock:
            sock.sendall(b"GET / HTTP/1.1\r\nHost: 127.0.0.1\r\nConnection: close\r\n\r\n")
            response = sock.recv(64)
        self.assertTrue(response.startswith(b"HTTP/1.1"))


if __name__ == "__main__":
    unittest.main()
