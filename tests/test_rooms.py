import os
import sys
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import rooms  # noqa: E402

SONG = {
    "artist": "Journey",
    "title": "Don't Stop Believin'",
    "cover_art": "http://example.com/art.jpg",
    "duration_seconds": 251,
    "ytmusic_video_id": "abc123",
}


class FakeConnection:
    """Records every payload sent to it instead of touching a real socket."""

    def __init__(self, role):
        self.role = role
        self.sent = []

    def send(self, payload):
        self.sent.append(payload)

    def last_type(self):
        return self.sent[-1]["type"] if self.sent else None

    def types(self):
        return [p["type"] for p in self.sent]


class RaisingConnection:
    role = "phone"

    def send(self, payload):
        raise ConnectionError("socket is dead")


class RoomLifecycleTestCase(unittest.TestCase):
    def setUp(self):
        self.registry = rooms.RoomRegistry()

    def test_create_room_generates_unique_code(self):
        room = self.registry.create_room()
        self.assertEqual(len(room.code), rooms.CODE_LENGTH)
        for ch in room.code:
            self.assertIn(ch, rooms.CODE_ALPHABET)

        other = self.registry.create_room()
        self.assertNotEqual(room.code, other.code)

    def test_get_room_returns_none_for_unknown_code(self):
        self.assertIsNone(self.registry.get_room("ZZZZ"))

    def test_attach_tv_sets_connection(self):
        room = self.registry.create_room()
        tv = FakeConnection("tv")
        attached = self.registry.attach_tv(room.code, tv)
        self.assertIs(attached.tv_conn, tv)

    def test_attach_tv_unknown_code_returns_none(self):
        self.assertIsNone(self.registry.attach_tv("ZZZZ", FakeConnection("tv")))

    def test_attach_tv_sends_snapshot_including_players_who_joined_first(self):
        # Simulates the race where a phone's join lands before the TV's
        # attach - without a snapshot-on-attach, the TV would never learn
        # about a phone that joined an as-yet-TV-less room.
        room = self.registry.create_room()
        phone = FakeConnection("phone")
        self.registry.join_phone(room.code, phone, "Dave")

        tv = FakeConnection("tv")
        self.registry.attach_tv(room.code, tv)

        snapshot = [p for p in tv.sent if p["type"] == "room-snapshot"][0]
        self.assertEqual([p["name"] for p in snapshot["joined_players"]], ["Dave"])

    def test_detach_tv_tears_down_room_after_grace_period(self):
        # detach_tv doesn't tear the room down immediately - it waits out
        # TV_RECONNECT_GRACE_SECONDS first, since a TV's own lobby<->player
        # page navigation (static/tv/lobby.js / static/player/room-broadcast.js)
        # always closes the OLD page's socket before the NEW page's has
        # attached. Patched short here so the test doesn't eat the real
        # (5s in production) delay.
        room = self.registry.create_room()
        tv = FakeConnection("tv")
        self.registry.attach_tv(room.code, tv)
        phone = FakeConnection("phone")
        self.registry.join_phone(room.code, phone, "Dave")

        with patch.object(rooms, "TV_RECONNECT_GRACE_SECONDS", 0.05):
            detached = self.registry.detach_tv(room.code)
            self.assertIsNotNone(detached)
            # Still alive immediately after detach - this is the grace
            # period, not an instant teardown.
            self.assertIsNotNone(self.registry.get_room(room.code))
            self.assertNotIn("room-closed", phone.types())

            time.sleep(0.15)

        self.assertIsNone(self.registry.get_room(room.code))
        self.assertIn("room-closed", phone.types())

    def test_attach_tv_within_grace_period_cancels_teardown(self):
        # Reproduces the real bug this grace period fixes: a TV reconnecting
        # (its page navigation's new connection attaching) before the grace
        # period elapses must reclaim the room - phones must never see
        # room-closed.
        room = self.registry.create_room()
        old_tv = FakeConnection("tv")
        self.registry.attach_tv(room.code, old_tv)
        phone = FakeConnection("phone")
        self.registry.join_phone(room.code, phone, "Dave")

        with patch.object(rooms, "TV_RECONNECT_GRACE_SECONDS", 0.05):
            self.registry.detach_tv(room.code)
            new_tv = FakeConnection("tv")
            reattached = self.registry.attach_tv(room.code, new_tv)
            self.assertIsNotNone(reattached)

            time.sleep(0.15)

        self.assertIsNotNone(self.registry.get_room(room.code))
        self.assertNotIn("room-closed", phone.types())
        self.assertIs(self.registry.get_room(room.code).tv_conn, new_tv)

    def test_detach_tv_with_stale_connection_is_a_noop(self):
        room = self.registry.create_room()
        old_tv = FakeConnection("tv")
        self.registry.attach_tv(room.code, old_tv)
        new_tv = FakeConnection("tv")
        self.registry.attach_tv(room.code, new_tv)

        # A late teardown from the OLD connection's handler must not kill
        # the room the new connection already took over.
        result = self.registry.detach_tv(room.code, conn=old_tv)
        self.assertIsNone(result)
        self.assertIsNotNone(self.registry.get_room(room.code))


class PhoneJoinLeaveTestCase(unittest.TestCase):
    def setUp(self):
        self.registry = rooms.RoomRegistry()
        self.room = self.registry.create_room()
        self.tv = FakeConnection("tv")
        self.registry.attach_tv(self.room.code, self.tv)

    def test_join_phone_assigns_id_and_sends_snapshot(self):
        phone = FakeConnection("phone")
        room, phone_id = self.registry.join_phone(self.room.code, phone, "Dave")
        self.assertIsNotNone(room)
        self.assertTrue(phone_id)
        self.assertEqual(phone.last_type(), "joined-players")
        snapshot = [p for p in phone.sent if p["type"] == "room-snapshot"][0]
        self.assertEqual(snapshot["joined_players"], [{"phone_id": phone_id, "name": "Dave"}])

    def test_join_phone_without_name_gets_default(self):
        phone = FakeConnection("phone")
        _, phone_id = self.registry.join_phone(self.room.code, phone, None)
        room = self.registry.get_room(self.room.code)
        self.assertEqual(room.singers[phone_id], "Singer 1")

    def test_join_phone_unknown_code_returns_none(self):
        room, phone_id = self.registry.join_phone("ZZZZ", FakeConnection("phone"), "Dave")
        self.assertIsNone(room)
        self.assertIsNone(phone_id)

    def test_join_notifies_tv_and_other_phones(self):
        first = FakeConnection("phone")
        self.registry.join_phone(self.room.code, first, "Dave")
        second = FakeConnection("phone")
        self.registry.join_phone(self.room.code, second, "Mia")

        self.assertIn("joined-players", self.tv.types())
        last_tv_players = [p for p in self.tv.sent if p["type"] == "joined-players"][-1]["players"]
        self.assertEqual({p["name"] for p in last_tv_players}, {"Dave", "Mia"})

    def test_leave_phone_removes_singer_and_notifies_room(self):
        phone = FakeConnection("phone")
        _, phone_id = self.registry.join_phone(self.room.code, phone, "Dave")

        self.registry.leave_phone(self.room.code, phone_id)

        room = self.registry.get_room(self.room.code)
        self.assertNotIn(phone_id, room.phones)
        self.assertNotIn(phone_id, room.singers)
        last_tv_players = [p for p in self.tv.sent if p["type"] == "joined-players"][-1]["players"]
        self.assertEqual(last_tv_players, [])


class QueueTestCase(unittest.TestCase):
    def setUp(self):
        self.registry = rooms.RoomRegistry()
        self.room = self.registry.create_room()
        self.tv = FakeConnection("tv")
        self.registry.attach_tv(self.room.code, self.tv)
        self.phone = FakeConnection("phone")
        _, self.phone_id = self.registry.join_phone(self.room.code, self.phone, "Dave")

    def test_enqueue_song_appends_and_broadcasts(self):
        entry = self.registry.enqueue_song(self.room.code, SONG, self.phone_id)
        self.assertEqual(entry.artist, SONG["artist"])
        self.assertEqual(entry.singer_name, "Dave")

        room = self.registry.get_room(self.room.code)
        self.assertEqual(room.queue, [entry])
        queue_updates = [p for p in self.tv.sent if p["type"] == "queue-update"]
        self.assertEqual(queue_updates[-1]["queue"][0]["entry_id"], entry.entry_id)

    def test_enqueue_song_unknown_code_returns_none(self):
        self.assertIsNone(self.registry.enqueue_song("ZZZZ", SONG, self.phone_id))

    def test_remove_queue_entry(self):
        entry = self.registry.enqueue_song(self.room.code, SONG, self.phone_id)
        removed = self.registry.remove_queue_entry(self.room.code, entry.entry_id)
        self.assertTrue(removed)
        self.assertEqual(self.registry.get_room(self.room.code).queue, [])

    def test_remove_unknown_entry_returns_false(self):
        self.assertFalse(self.registry.remove_queue_entry(self.room.code, "nope"))

    def test_reorder_queue_applies_new_order(self):
        first = self.registry.enqueue_song(self.room.code, SONG, self.phone_id)
        second = self.registry.enqueue_song(self.room.code, dict(SONG, title="Zombie"), self.phone_id)

        self.registry.reorder_queue(self.room.code, [second.entry_id, first.entry_id])

        room = self.registry.get_room(self.room.code)
        self.assertEqual([e.entry_id for e in room.queue], [second.entry_id, first.entry_id])

    def test_reorder_queue_appends_omitted_ids_instead_of_dropping(self):
        first = self.registry.enqueue_song(self.room.code, SONG, self.phone_id)
        second = self.registry.enqueue_song(self.room.code, dict(SONG, title="Zombie"), self.phone_id)

        self.registry.reorder_queue(self.room.code, [second.entry_id])

        room = self.registry.get_room(self.room.code)
        self.assertEqual([e.entry_id for e in room.queue], [second.entry_id, first.entry_id])

    def test_advance_queue_pops_head_into_now_playing(self):
        first = self.registry.enqueue_song(self.room.code, SONG, self.phone_id)
        self.registry.enqueue_song(self.room.code, dict(SONG, title="Zombie"), self.phone_id)

        popped = self.registry.advance_queue(self.room.code)

        self.assertEqual(popped.entry_id, first.entry_id)
        room = self.registry.get_room(self.room.code)
        self.assertEqual(room.now_playing.entry_id, first.entry_id)
        self.assertEqual(len(room.queue), 1)
        now_playing_updates = [p for p in self.phone.sent if p["type"] == "now-playing-change"]
        self.assertEqual(now_playing_updates[-1]["now_playing"]["entry_id"], first.entry_id)

    def test_advance_queue_on_empty_queue_clears_now_playing(self):
        popped = self.registry.advance_queue(self.room.code)
        self.assertIsNone(popped)
        room = self.registry.get_room(self.room.code)
        self.assertIsNone(room.now_playing)

    def test_advance_queue_unknown_code_returns_none(self):
        self.assertIsNone(self.registry.advance_queue("ZZZZ"))

    def test_set_now_playing_resolved_updates_and_broadcasts(self):
        self.registry.enqueue_song(self.room.code, SONG, self.phone_id)
        self.registry.advance_queue(self.room.code)

        resolved = {"song_id": 7, "has_singer_vocals": True}
        updated = self.registry.set_now_playing_resolved(self.room.code, resolved)

        self.assertEqual(updated.resolved, resolved)
        last = [p for p in self.phone.sent if p["type"] == "now-playing-change"][-1]
        self.assertEqual(last["now_playing"]["resolved"], resolved)

    def test_set_now_playing_resolved_without_now_playing_returns_none(self):
        self.assertIsNone(self.registry.set_now_playing_resolved(self.room.code, {}))


class PlaybackSyncTestCase(unittest.TestCase):
    def setUp(self):
        self.registry = rooms.RoomRegistry()
        self.room = self.registry.create_room()
        self.tv = FakeConnection("tv")
        self.registry.attach_tv(self.room.code, self.tv)
        self.phone = FakeConnection("phone")
        _, self.phone_id = self.registry.join_phone(self.room.code, self.phone, "Dave")
        self.registry.enqueue_song(self.room.code, SONG, self.phone_id)
        self.registry.advance_queue(self.room.code)

    def test_set_now_playing_position_broadcasts_to_phones_only(self):
        before = len(self.tv.sent)
        self.registry.set_now_playing_position(self.room.code, 1500.0, playing=True)

        self.assertEqual(len(self.tv.sent), before)  # TV is the source, not a recipient
        last = [p for p in self.phone.sent if p["type"] == "playback-position"][-1]
        self.assertEqual(last["pos_ms"], 1500.0)
        self.assertTrue(last["playing"])

    def test_set_now_playing_position_without_now_playing_returns_none(self):
        registry = rooms.RoomRegistry()
        room = registry.create_room()
        registry.attach_tv(room.code, FakeConnection("tv"))
        self.assertIsNone(registry.set_now_playing_position(room.code, 100.0))

    def test_route_playback_control_forwards_to_tv_only(self):
        ok = self.registry.route_playback_control(self.room.code, "skip")
        self.assertTrue(ok)
        self.assertEqual(self.tv.last_type(), "playback-control")
        self.assertEqual(self.tv.sent[-1]["action"], "skip")

    def test_route_playback_control_no_tv_returns_false(self):
        registry = rooms.RoomRegistry()
        room = registry.create_room()
        self.assertFalse(registry.route_playback_control(room.code, "skip"))

    def test_set_voice_assist_volume_clamped_and_forwarded_to_tv(self):
        vol = self.registry.set_voice_assist_volume(self.room.code, 1.7)
        self.assertEqual(vol, 1.0)
        self.assertEqual(self.tv.sent[-1], {"type": "voice-assist-volume", "volume": 1.0})

    def test_route_score_update_forwards_to_tv_with_singer_name(self):
        ok = self.registry.route_score_update(self.room.code, self.phone_id, {"score": 88, "singing": True})
        self.assertTrue(ok)
        last = self.tv.sent[-1]
        self.assertEqual(last["type"], "score-update")
        self.assertEqual(last["singer_name"], "Dave")
        self.assertEqual(last["score"], 88)


class BroadcastResilienceTestCase(unittest.TestCase):
    def test_a_dead_connection_does_not_block_delivery_to_others(self):
        registry = rooms.RoomRegistry()
        room = registry.create_room()
        registry.attach_tv(room.code, FakeConnection("tv"))
        registry.join_phone(room.code, RaisingConnection(), "Ghost")
        good_phone = FakeConnection("phone")
        registry.join_phone(room.code, good_phone, "Dave")

        # Should not raise even though the "Ghost" connection's send() blows up.
        registry.enqueue_song(room.code, SONG, "whoever")

        self.assertIn("queue-update", good_phone.types())


class ConcurrencyStressTestCase(unittest.TestCase):
    def test_concurrent_enqueues_all_land_without_deadlock_or_loss(self):
        registry = rooms.RoomRegistry()
        room = registry.create_room()
        registry.attach_tv(room.code, FakeConnection("tv"))
        _, phone_id = registry.join_phone(room.code, FakeConnection("phone"), "Dave")

        n_threads = 8
        songs_per_thread = 10
        barrier = threading.Barrier(n_threads)

        def worker(i):
            barrier.wait(timeout=5)
            for j in range(songs_per_thread):
                registry.enqueue_song(room.code, dict(SONG, title=f"song-{i}-{j}"), phone_id)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)
            self.assertFalse(t.is_alive(), "worker thread did not finish - possible deadlock")

        final_room = registry.get_room(room.code)
        self.assertEqual(len(final_room.queue), n_threads * songs_per_thread)


if __name__ == "__main__":
    unittest.main()
