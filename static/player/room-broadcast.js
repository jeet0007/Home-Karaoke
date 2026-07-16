// TV-side integration with the room-pairing feature (see core/rooms.py):
// when the player page's URL carries ?room=<code>, this module attaches to
// that room as its TV, broadcasts the playback position for phones to
// extrapolate from (static/shared/clock.js), and applies phones' remote-
// control requests through the SAME functions the on-screen buttons
// already use - no parallel control path. A no-op entirely when ?room=
// isn't present, i.e. the existing single-device player is unaffected.
//
// main.js calls initRoomBroadcast(loadSongByIdentity) once at boot and
// reportResolved(data) after every song load (its own initial one and any
// later one this module triggers) - see main.js for why those two calls
// are split rather than this module loading songs itself.

import { togglePlayback, restartPlayback } from './playback.js';
import { effectiveLyricMs } from './sync-offset.js';
import { setVolumeRemote } from './singer-assist.js';

const audio = document.getElementById('audio');
const roomOverlay = document.getElementById('room-overlay');
const roomOverlayCodeEl = document.getElementById('room-overlay-code');
const roomOverlayUpNextEl = document.getElementById('room-overlay-upnext');

const POSITION_BROADCAST_INTERVAL_MS = 500;

let socket = null;
let roomCode = null;
let currentEntryId = null;
let loadSongByIdentity = null;
// The very first song's /select-song fetch (often answered instantly from
// the library's fast path) can resolve before this module's own WebSocket
// has finished its connect+handshake round trip - reportResolved() would
// otherwise silently drop that first now-playing-resolved. Queue at most
// the latest one and flush it once the socket opens; every other message
// this module sends is periodic or user-triggered and safely retried on
// its own (a position tick, a control forward), so only this one needs it.
let pendingResolved = null;

function urlParams() {
  return new URLSearchParams(window.location.search);
}

function send(payload) {
  if (socket && socket.readyState === WebSocket.OPEN) {
    socket.send(JSON.stringify(payload));
    return true;
  }
  return false;
}

function goToLobby() {
  window.location.href = `/tv?code=${encodeURIComponent(roomCode)}`;
}

function renderUpNext(queue) {
  if (!roomCode) return;
  if (!queue || queue.length === 0) {
    roomOverlayUpNextEl.textContent = 'Queue is empty';
    return;
  }
  roomOverlayUpNextEl.textContent = `Up next: ${queue[0].title} — ${queue[0].singer_name}`;
}

function applyNowPlaying(nowPlaying) {
  if (!nowPlaying) {
    goToLobby();
    return;
  }
  if (nowPlaying.entry_id === currentEntryId) return;
  currentEntryId = nowPlaying.entry_id;
  loadSongByIdentity({
    title: nowPlaying.title,
    artist: nowPlaying.artist,
    duration: null,
    ytmusicVideoId: null,
  });
}

function handlePlaybackControl(action) {
  if (action === 'play' || action === 'pause' || action === 'toggle') {
    togglePlayback();
  } else if (action === 'restart') {
    restartPlayback();
  } else if (action === 'skip') {
    send({ type: 'advance-queue' });
  }
}

function handleMessage(payload) {
  if (payload.type === 'room-snapshot') {
    renderUpNext(payload.queue);
    applyNowPlaying(payload.now_playing);
  } else if (payload.type === 'now-playing-change') {
    applyNowPlaying(payload.now_playing);
  } else if (payload.type === 'queue-update') {
    renderUpNext(payload.queue);
  } else if (payload.type === 'playback-control') {
    handlePlaybackControl(payload.action);
  } else if (payload.type === 'voice-assist-volume') {
    setVolumeRemote(payload.volume);
  } else if (payload.type === 'room-closed') {
    goToLobby();
  }
}

function broadcastPosition() {
  if (!audio.src) return;
  send({ type: 'playback-position', pos_ms: effectiveLyricMs(), playing: !audio.paused });
}

export function initRoomBroadcast(loadSongByIdentityFn) {
  roomCode = urlParams().get('room');
  if (!roomCode) return;
  roomCode = roomCode.trim().toUpperCase();
  currentEntryId = urlParams().get('entry');
  loadSongByIdentity = loadSongByIdentityFn;

  roomOverlay.hidden = false;
  roomOverlayCodeEl.textContent = roomCode;

  const wsProtocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
  socket = new WebSocket(`${wsProtocol}//${window.location.host}/room-ws`);

  socket.addEventListener('open', () => {
    socket.send(JSON.stringify({ role: 'tv', action: 'host', code: roomCode }));
    if (pendingResolved) {
      socket.send(JSON.stringify(pendingResolved));
      pendingResolved = null;
    }
  });
  socket.addEventListener('message', (event) => {
    let payload;
    try {
      payload = JSON.parse(event.data);
    } catch (err) {
      return;
    }
    handleMessage(payload);
  });
  // A dropped connection (dev-reload, transient network blip, or the room
  // was torn down) sends the TV back to the lobby to reattach/recreate -
  // mirrors static/tv/lobby.js's own reconnect-by-reload behavior.
  socket.addEventListener('close', goToLobby);

  audio.addEventListener('ended', () => send({ type: 'advance-queue' }));
  setInterval(broadcastPosition, POSITION_BROADCAST_INTERVAL_MS);
}

// Called by main.js after every song load, room mode or not - a no-op
// unless this module is actively attached to a room.
export function reportResolved(data) {
  if (!roomCode || !data) return;
  const payload = {
    type: 'now-playing-resolved',
    resolved: {
      song_id: data.song_id || null,
      cover_art: data.cover_art || '',
      lyrics: { synced: (data.lyrics && data.lyrics.synced) || [] },
      has_singer_vocals: Boolean(data.has_singer_vocals),
      duration_seconds: data.duration_seconds || null,
    },
  };
  if (!send(payload)) pendingResolved = payload;
}
