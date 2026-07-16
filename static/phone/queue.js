// Renders the room's shared sing-queue ("Up next") from queue-update
// broadcasts and the initial room-snapshot - see core/rooms.py.

import { escapeHtml } from '../shared/utils.js';
import { onMessage } from './socket.js';

const queueListEl = document.getElementById('queue-list');
const queueCountEl = document.getElementById('queue-count');

function renderQueue(queue) {
  queueCountEl.textContent = `${queue.length} song${queue.length === 1 ? '' : 's'}`;

  if (queue.length === 0) {
    queueListEl.innerHTML = '<div class="empty-state">No songs queued yet — add one from Search.</div>';
    return;
  }

  queueListEl.innerHTML = queue
    .map(
      (entry, index) => `
        <div class="queue-row">
          <div class="queue-position">${index + 1}</div>
          <div class="queue-info">
            <div class="queue-title">${escapeHtml(entry.title)}</div>
            <div class="queue-singer">${escapeHtml(entry.singer_name)}</div>
          </div>
        </div>
      `
    )
    .join('');
}

export function initQueue() {
  onMessage('queue-update', (payload) => renderQueue(payload.queue || []));
  onMessage('room-snapshot', (payload) => renderQueue(payload.queue || []));
}
