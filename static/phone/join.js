// Phone join landing (/join/<code>): auto-joins the room over /room-ws
// with no name prompt - matching the wireframe's "joins automatically, no
// code to type" flow - then hands off to /room/<code>, the real phone app
// shell (templates/phone_home.html), once the join is confirmed.

const body = document.body;
const code = body.dataset.code;
const roomExists = body.dataset.roomExists === 'true';

function joinRoom() {
  const wsProtocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
  const socket = new WebSocket(`${wsProtocol}//${window.location.host}/room-ws`);

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

    if (payload.type === 'room-snapshot') {
      // The join succeeded - close this socket and let /room/<code> open
      // its own, rather than trying to hand a live WebSocket across a page
      // navigation.
      socket.close();
      window.location.href = `/room/${code}`;
    } else if (payload.type === 'error') {
      document.getElementById('state-joining').style.display = 'none';
      document.getElementById('state-error').style.display = 'flex';
    }
  });
}

if (roomExists) {
  joinRoom();
}
