// Shared /room-ws connection + a tiny pub/sub for the phone app shell -
// home.js/search.js/queue.js all need to send/receive over the same one
// socket, so it's factored out here rather than each module opening its
// own connection.

let socket = null;
const listeners = new Map(); // message type -> Set<fn>

export function onMessage(type, handler) {
  if (!listeners.has(type)) listeners.set(type, new Set());
  listeners.get(type).add(handler);
}

export function send(payload) {
  if (socket && socket.readyState === WebSocket.OPEN) {
    socket.send(JSON.stringify(payload));
  }
}

export function connect(code) {
  const wsProtocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
  socket = new WebSocket(`${wsProtocol}//${window.location.host}/room-ws`);

  socket.addEventListener('open', () => {
    socket.send(JSON.stringify({ role: 'phone', action: 'join', code }));
  });

  socket.addEventListener('message', (event) => {
    let payload;
    try {
      payload = JSON.parse(event.data);
    } catch (err) {
      return;
    }
    const handlers = listeners.get(payload.type);
    if (handlers) handlers.forEach((fn) => fn(payload));
  });

  return socket;
}
