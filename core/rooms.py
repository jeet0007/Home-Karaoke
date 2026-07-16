"""In-memory room registry for the TV + phone-as-mic/remote pairing feature.

A "room" is created when a TV boots into its lobby and lasts exactly as
long as that TV's WebSocket connection - phones scan the TV's QR code to
join, browse/search and add songs to the room's shared sing-queue, and
later act as a synced-lyrics display + local mic/remote control while the
TV plays each song in turn.

Deliberately in-memory, not SQLite: a room has no meaning once its TV
disconnects (there's nothing to resume), so persisting it would only let a
room outlive the session that defines it. This mirrors the existing
`_live_select_cache` precedent in app.py and assumes the same
single-process, no-broker deployment the rest of this codebase is built
around (see CLAUDE.md's Docker section) - it does not work if the app is
ever run behind multiple WSGI worker processes.

Thread-safety: flask-sock hands each WebSocket connection its own thread,
blocked in a receive loop, so a broadcast (e.g. one phone enqueues a song
and everyone else needs to see the updated queue) means one thread sending
on a socket owned by another thread's connection. `Connection.send()`
below serializes concurrent sends on one socket with its own lock; the
registry's single `threading.Lock` only ever guards in-memory dict/list
mutations and payload construction, and is always released *before* any
network I/O - a slow or sleeping phone must never stall another thread's
room mutation or another recipient's delivery.
"""

import json
import secrets
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Optional

# Unambiguous alphabet for spoken/typed-aloud room codes - no 0/O, 1/I/L.
CODE_ALPHABET = "23456789ABCDEFGHJKMNPQRSTUVWXYZ"
CODE_LENGTH = 4
_MAX_CODE_ATTEMPTS = 100

# How long a room survives with no TV connection before being torn down.
# The TV is a single browser tab that navigates between screens (lobby ->
# player when a song starts, player -> lobby when the queue drains -
# static/tv/lobby.js / static/player/room-broadcast.js) via a real page
# load, not an in-page transition. A page load always closes the OLD
# page's WebSocket before the NEW page's JS has even started running, let
# alone reattached its own connection - so treating "TV connection closed"
# as "TV is gone, kill the room" tears every room down on its very first
# song. This grace period gives the new page's connection time to attach
# and reclaim the room before it's actually torn down. Read at call time
# (not captured into an instance attribute) so tests can monkeypatch it
# short instead of eating this delay for real.
TV_RECONNECT_GRACE_SECONDS = 5.0


class Connection:
    """Thread-safe wrapper around a single outbound send function.

    `send_fn` is whatever the caller's transport provides for pushing a
    text frame (e.g. a flask-sock `ws.send`). The lock only serializes
    calls to THIS connection's send - it is independent of the registry
    lock, which never blocks on it.
    """

    def __init__(self, role, send_fn):
        self.role = role
        self._send_fn = send_fn
        self._lock = threading.Lock()

    def send(self, payload):
        data = json.dumps(payload)
        with self._lock:
            self._send_fn(data)


@dataclass
class QueueEntry:
    entry_id: str
    artist: str
    title: str
    cover_art: str
    duration_seconds: Optional[int]
    ytmusic_video_id: Optional[str]
    singer_phone_id: str
    singer_name: str

    def to_dict(self):
        return {
            "entry_id": self.entry_id,
            "artist": self.artist,
            "title": self.title,
            "cover_art": self.cover_art,
            "duration_seconds": self.duration_seconds,
            "ytmusic_video_id": self.ytmusic_video_id,
            "singer_phone_id": self.singer_phone_id,
            "singer_name": self.singer_name,
        }


@dataclass
class NowPlaying:
    entry_id: str
    artist: str
    title: str
    singer_phone_id: str
    singer_name: str
    resolved: Optional[dict] = None
    position_ms: float = 0.0
    playing: bool = False
    voice_assist_volume: float = 0.0

    def to_dict(self):
        return {
            "entry_id": self.entry_id,
            "artist": self.artist,
            "title": self.title,
            "singer_phone_id": self.singer_phone_id,
            "singer_name": self.singer_name,
            "resolved": self.resolved,
            "position_ms": self.position_ms,
            "playing": self.playing,
            "voice_assist_volume": self.voice_assist_volume,
        }

    @classmethod
    def from_entry(cls, entry):
        return cls(
            entry_id=entry.entry_id,
            artist=entry.artist,
            title=entry.title,
            singer_phone_id=entry.singer_phone_id,
            singer_name=entry.singer_name,
        )


@dataclass
class Room:
    code: str
    tv_conn: Optional[Connection] = None
    phones: dict = field(default_factory=dict)  # phone_id -> Connection
    singers: dict = field(default_factory=dict)  # phone_id -> display name
    queue: list = field(default_factory=list)  # list[QueueEntry], head = up next
    now_playing: Optional[NowPlaying] = None
    created_at: float = field(default_factory=time.time)
    last_activity_at: float = field(default_factory=time.time)
    # Set by detach_tv while waiting out TV_RECONNECT_GRACE_SECONDS for a
    # replacement TV connection; cancelled by attach_tv if one arrives.
    pending_teardown: Optional[threading.Timer] = None


class RoomRegistry:
    """Owns every live room. All mutation happens under one lock; every
    method that changes room state also builds the resulting broadcast
    payload(s) while holding it, then releases the lock before doing any
    `.send()` calls."""

    def __init__(self):
        self._rooms = {}
        self._lock = threading.Lock()

    # -- room lifecycle ----------------------------------------------

    def create_room(self):
        with self._lock:
            code = self._generate_code_locked()
            room = Room(code=code)
            self._rooms[code] = room
            return room

    def get_room(self, code):
        with self._lock:
            return self._rooms.get(code)

    def room_state(self, code):
        with self._lock:
            room = self._rooms.get(code)
            if room is None:
                return None
            return {"code": room.code, **self._snapshot_locked(room)}

    def attach_tv(self, code, conn):
        """Attaches conn as the room's TV and immediately sends it a full
        state snapshot - not just a courtesy for a TV reconnecting mid-room,
        but required for correctness: without this, a phone that joins
        before the TV finishes attaching would broadcast its
        "joined-players" update to a room with no TV connection yet, and
        the TV would never learn about it.

        Also cancels any pending grace-period teardown (see detach_tv) -
        this is exactly what that grace period exists for: the TV's own
        page navigation (lobby -> player, player -> lobby) reconnecting in
        time."""
        with self._lock:
            room = self._rooms.get(code)
            if room is None:
                return None
            if room.pending_teardown is not None:
                room.pending_teardown.cancel()
                room.pending_teardown = None
            room.tv_conn = conn
            room.last_activity_at = time.time()
            snapshot_payload = {"type": "room-snapshot", **self._snapshot_locked(room)}
        self._fanout([conn], snapshot_payload)
        return room

    def detach_tv(self, code, conn=None):
        """A TV connection closing doesn't immediately kill the room. The
        TV is one browser tab that moves between screens via a real page
        navigation (static/tv/lobby.js <-> static/player/room-broadcast.js),
        which closes the OLD page's socket before the NEW page's JS has
        even started running - so treating every disconnect as "the TV is
        gone" would tear the room down on its first song, every time. If
        this really was the room's current TV, mark it detached and start
        a TV_RECONNECT_GRACE_SECONDS timer instead: attach_tv cancels it if
        a replacement connection arrives in time, and _finalize_teardown
        (below) actually tears the room down if nothing does.

        If `conn` is given and no longer matches the room's current TV
        connection, this is a no-op: a stale disconnect handler (from a
        connection a reconnect already replaced) must not re-detach a room
        that's already been reclaimed.
        """
        with self._lock:
            room = self._rooms.get(code)
            if room is None:
                return None
            if conn is not None and room.tv_conn is not conn:
                return None
            room.tv_conn = None
            if room.pending_teardown is not None:
                room.pending_teardown.cancel()
            timer = threading.Timer(TV_RECONNECT_GRACE_SECONDS, self._finalize_teardown, args=(code,))
            timer.daemon = True
            room.pending_teardown = timer
            timer.start()
        return room

    def _finalize_teardown(self, code):
        """Fired by detach_tv's grace-period timer. Only actually tears the
        room down if it's still TV-less - a reattach in the meantime
        (attach_tv) already cancelled this timer, but the check here is
        what makes a late/duplicate firing harmless too, without needing to
        track timer identity."""
        with self._lock:
            room = self._rooms.get(code)
            if room is None or room.tv_conn is not None:
                return
            room = self._rooms.pop(code)
            recipients = list(room.phones.values())
        self._fanout(recipients, {"type": "room-closed"})

    # -- phone join/leave ---------------------------------------------

    def join_phone(self, code, conn, name=None):
        with self._lock:
            room = self._rooms.get(code)
            if room is None:
                return None, None
            phone_id = uuid.uuid4().hex
            display_name = (name or "").strip() or f"Singer {len(room.phones) + 1}"
            room.phones[phone_id] = conn
            room.singers[phone_id] = display_name
            room.last_activity_at = time.time()
            snapshot_payload = {"type": "room-snapshot", "phone_id": phone_id, **self._snapshot_locked(room)}
            players_payload = {"type": "joined-players", "players": self._players_locked(room)}
            recipients = self._all_connections_locked(room)
        self._fanout([conn], snapshot_payload)
        self._fanout(recipients, players_payload)
        return room, phone_id

    def leave_phone(self, code, phone_id):
        with self._lock:
            room = self._rooms.get(code)
            if room is None:
                return None
            room.phones.pop(phone_id, None)
            room.singers.pop(phone_id, None)
            room.last_activity_at = time.time()
            payload = {"type": "joined-players", "players": self._players_locked(room)}
            recipients = self._all_connections_locked(room)
        self._fanout(recipients, payload)
        return room

    # -- sing-queue -----------------------------------------------------

    def enqueue_song(self, code, song, phone_id):
        with self._lock:
            room = self._rooms.get(code)
            if room is None:
                return None
            singer_name = room.singers.get(phone_id, "Singer")
            entry = QueueEntry(
                entry_id=uuid.uuid4().hex,
                artist=str(song.get("artist", "")),
                title=str(song.get("title", "")),
                cover_art=str(song.get("cover_art", "")),
                duration_seconds=song.get("duration_seconds"),
                ytmusic_video_id=song.get("ytmusic_video_id"),
                singer_phone_id=phone_id,
                singer_name=singer_name,
            )
            room.queue.append(entry)
            room.last_activity_at = time.time()
            payload = self._queue_update_locked(room)
            recipients = self._all_connections_locked(room)
        self._fanout(recipients, payload)
        return entry

    def remove_queue_entry(self, code, entry_id):
        with self._lock:
            room = self._rooms.get(code)
            if room is None:
                return False
            before = len(room.queue)
            room.queue = [e for e in room.queue if e.entry_id != entry_id]
            if len(room.queue) == before:
                return False
            room.last_activity_at = time.time()
            payload = self._queue_update_locked(room)
            recipients = self._all_connections_locked(room)
        self._fanout(recipients, payload)
        return True

    def reorder_queue(self, code, entry_ids):
        """`entry_ids` is the desired full ordering by id. Ids from the
        current queue that are missing from it are appended at the end
        rather than silently dropped; unknown ids are ignored."""
        with self._lock:
            room = self._rooms.get(code)
            if room is None:
                return False
            by_id = {e.entry_id: e for e in room.queue}
            reordered = [by_id[eid] for eid in entry_ids if eid in by_id]
            missing = [e for e in room.queue if e.entry_id not in entry_ids]
            room.queue = reordered + missing
            room.last_activity_at = time.time()
            payload = self._queue_update_locked(room)
            recipients = self._all_connections_locked(room)
        self._fanout(recipients, payload)
        return True

    def advance_queue(self, code):
        """Pops the head of the queue into `now_playing` (or clears
        `now_playing` if the queue is empty) and broadcasts both the new
        now-playing state and the updated queue. Returns the popped
        QueueEntry, or None if the queue was empty."""
        with self._lock:
            room = self._rooms.get(code)
            if room is None:
                return None
            entry = room.queue.pop(0) if room.queue else None
            room.now_playing = NowPlaying.from_entry(entry) if entry else None
            room.last_activity_at = time.time()
            payloads = [
                {"type": "now-playing-change", "now_playing": room.now_playing.to_dict() if room.now_playing else None},
                self._queue_update_locked(room),
            ]
            recipients = self._all_connections_locked(room)
        for payload in payloads:
            self._fanout(recipients, payload)
        return entry

    def set_now_playing_resolved(self, code, resolved):
        """Called by the TV once it has resolved a queue entry's identity
        into a playable song (lyrics, video, artifacts) via the existing
        /select-song path, so phones get the full payload."""
        with self._lock:
            room = self._rooms.get(code)
            if room is None or room.now_playing is None:
                return None
            room.now_playing.resolved = resolved
            room.last_activity_at = time.time()
            payload = {"type": "now-playing-change", "now_playing": room.now_playing.to_dict()}
            recipients = self._all_connections_locked(room)
        self._fanout(recipients, payload)
        return room.now_playing

    # -- playback sync / remote control ---------------------------------

    def set_now_playing_position(self, code, pos_ms, playing=True):
        with self._lock:
            room = self._rooms.get(code)
            if room is None or room.now_playing is None:
                return None
            room.now_playing.position_ms = float(pos_ms)
            room.now_playing.playing = bool(playing)
            room.last_activity_at = time.time()
            payload = {
                "type": "playback-position",
                "pos_ms": room.now_playing.position_ms,
                "playing": room.now_playing.playing,
                "sent_at": time.time(),
            }
            recipients = list(room.phones.values())
        self._fanout(recipients, payload)
        return room.now_playing

    def route_playback_control(self, code, action, entry_id=None):
        """Forwards a phone's play/pause/restart/skip/remove request to the
        TV, which is the sole source of truth for actual playback state."""
        with self._lock:
            room = self._rooms.get(code)
            if room is None:
                return False
            tv_conn = room.tv_conn
        if tv_conn is None:
            return False
        payload = {"type": "playback-control", "action": action}
        if entry_id is not None:
            payload["entry_id"] = entry_id
        self._fanout([tv_conn], payload)
        return True

    def set_voice_assist_volume(self, code, volume):
        with self._lock:
            room = self._rooms.get(code)
            if room is None:
                return None
            volume = max(0.0, min(1.0, float(volume)))
            if room.now_playing is not None:
                room.now_playing.voice_assist_volume = volume
            room.last_activity_at = time.time()
            tv_conn = room.tv_conn
        if tv_conn is not None:
            self._fanout([tv_conn], {"type": "voice-assist-volume", "volume": volume})
        return volume

    def route_score_update(self, code, phone_id, score_payload):
        """Forwards a phone's local WASM-grader score to the TV (display
        only - the phone remains the sole owner of its own mic/scoring)."""
        with self._lock:
            room = self._rooms.get(code)
            if room is None:
                return False
            singer_name = room.singers.get(phone_id, "Singer")
            tv_conn = room.tv_conn
        if tv_conn is None:
            return False
        payload = {"type": "score-update", "phone_id": phone_id, "singer_name": singer_name, **score_payload}
        self._fanout([tv_conn], payload)
        return True

    # -- internals --------------------------------------------------------

    def _generate_code_locked(self):
        for _ in range(_MAX_CODE_ATTEMPTS):
            code = "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LENGTH))
            if code not in self._rooms:
                return code
        raise RuntimeError("could not allocate a unique room code")

    @staticmethod
    def _players_locked(room):
        return [{"phone_id": pid, "name": name} for pid, name in room.singers.items()]

    @staticmethod
    def _all_connections_locked(room):
        conns = list(room.phones.values())
        if room.tv_conn is not None:
            conns.append(room.tv_conn)
        return conns

    @staticmethod
    def _queue_update_locked(room):
        return {"type": "queue-update", "queue": [e.to_dict() for e in room.queue]}

    @classmethod
    def _snapshot_locked(cls, room):
        return {
            "queue": [e.to_dict() for e in room.queue],
            "now_playing": room.now_playing.to_dict() if room.now_playing else None,
            "joined_players": cls._players_locked(room),
        }

    @staticmethod
    def _fanout(connections, payload):
        for conn in connections:
            try:
                conn.send(payload)
            except Exception:
                # A dead/slow connection must not stop delivery to the rest
                # of the room - its own receive loop will notice the closed
                # socket and call leave_phone/detach_tv.
                pass
