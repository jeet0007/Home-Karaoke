// TV lobby: creates a room, shows its QR code, and renders phones as they
// join over /room-ws. See core/rooms.py for the room/message model this
// mirrors client-side.

import { escapeHtml } from '../shared/utils.js';

const qrImg = document.getElementById('qr-code');
const joinedPlayersEl = document.getElementById('joined-players');
const roomCodeEl = document.getElementById('room-code');

const WAITING_SLOT_HTML = '<div class="waiting-slot"><div class="avatar"></div><div class="name">waiting...</div></div>';

// Tracks whether the room currently has a song playing, and guards against
// sending advance-queue twice while the first request is still in flight -
// both queue-update and room-snapshot can observe "queue has songs, nothing
// playing" around the same time.
let nowPlayingIsNull = true;
let advanceRequested = false;

function renderPlayers(players) {
  joinedPlayersEl.innerHTML = players
    .map((player) => `<div class="joined-player"><div class="avatar"></div><div class="name">${escapeHtml(player.name)}</div></div>`)
    .join('');
  joinedPlayersEl.insertAdjacentHTML('beforeend', WAITING_SLOT_HTML);
}

function goToPlayer(code, nowPlaying) {
  const params = new URLSearchParams({
    artist: nowPlaying.artist,
    title: nowPlaying.title,
    room: code,
    entry: nowPlaying.entry_id,
  });
  window.location.href = `/player?${params.toString()}`;
}

function openRoomSocket(code) {
  const wsProtocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
  const socket = new WebSocket(`${wsProtocol}//${window.location.host}/room-ws`);

  function maybeAdvance() {
    if (nowPlayingIsNull && !advanceRequested) {
      advanceRequested = true;
      socket.send(JSON.stringify({ type: 'advance-queue' }));
    }
  }

  socket.addEventListener('open', () => {
    socket.send(JSON.stringify({ role: 'tv', action: 'host', code }));
  });

  socket.addEventListener('message', (event) => {
    let payload;
    try {
      payload = JSON.parse(event.data);
    } catch (err) {
      return;
    }
    if (payload.type === 'joined-players') {
      renderPlayers(payload.players || []);
    } else if (payload.type === 'room-snapshot') {
      // Sent once, right after attaching - covers phones that joined
      // before this TV connection existed (a fresh boot always starts
      // empty, but a reconnect may not).
      renderPlayers(payload.joined_players || []);
      nowPlayingIsNull = !payload.now_playing;
      if (payload.now_playing) {
        goToPlayer(code, payload.now_playing);
        return;
      }
      if ((payload.queue || []).length > 0) maybeAdvance();
    } else if (payload.type === 'queue-update') {
      if (nowPlayingIsNull && (payload.queue || []).length > 0) maybeAdvance();
    } else if (payload.type === 'now-playing-change') {
      nowPlayingIsNull = !payload.now_playing;
      if (payload.now_playing) goToPlayer(code, payload.now_playing);
    }
  });

  // A dropped connection (dev-reload, transient network blip, or the room
  // was actually torn down) reloads the page - boot()'s ?code= handling
  // above takes it from there: reattach if the room is still alive,
  // create a fresh one otherwise.
  socket.addEventListener('close', () => {
    setTimeout(() => window.location.reload(), 2000);
  });
}

async function boot() {
  // A TV returning from the player screen once its queue drains carries
  // its existing room code forward (?code=) so it reattaches to the SAME
  // room instead of abandoning it for a fresh one - see
  // static/player/room-broadcast.js's goToLobby(). RoomRegistry is
  // in-memory only (see core/rooms.py), so a code from before a server
  // restart is just as "unknown" as a mistyped one - verify it before
  // trying to use it, otherwise a stale ?code= in the URL reloads forever
  // retrying a room that will never exist again.
  const existingCode = new URLSearchParams(window.location.search).get('code');
  let code = existingCode ? existingCode.trim().toUpperCase() : null;

  if (code) {
    const stateRes = await fetch(`/room/${code}/state`);
    if (!stateRes.ok) code = null;
  }

  if (!code) {
    const res = await fetch('/room/create', { method: 'POST' });
    if (!res.ok) {
      console.error('Failed to create a room');
      return;
    }
    ({ code } = await res.json());
    history.replaceState(null, '', `/tv?code=${code}`);
  }

  qrImg.src = `/room/${code}/qr.svg`;
  roomCodeEl.textContent = code;
  openRoomSocket(code);
}

boot();
