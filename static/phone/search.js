// Phone search: hits the same /unified-search endpoint the app has always
// used for song-identity search. Picking a result enqueues it over the
// room WS instead of navigating to /player - the room's TV is what
// actually plays it.

import { escapeHtml, formatDuration } from '../shared/utils.js';
import { send } from './socket.js';
import { showToast } from './toast.js';

const searchInput = document.getElementById('search-input');
const resultsEl = document.getElementById('search-results');

const SEARCH_DEBOUNCE_MS = 350;
const SEARCH_MIN_QUERY_LENGTH = 2;

let debounceTimer = null;
let requestId = 0;

function renderSongRow(song) {
  const meta = [song.album, formatDuration(song.duration_seconds)].filter(Boolean).join(' · ');
  const thumb = song.cover_art || '';
  return `
    <div class="result-row" data-artist="${escapeHtml(song.artist)}" data-title="${escapeHtml(song.title)}" data-duration="${song.duration_seconds != null ? song.duration_seconds : ''}" data-cover="${escapeHtml(thumb)}" data-ytm="${escapeHtml(song.ytmusic_video_id || '')}">
      ${thumb ? `<img class="result-thumb" src="${escapeHtml(thumb)}" loading="lazy" alt="">` : '<div class="result-thumb placeholder"></div>'}
      <div class="result-info">
        <div class="result-title">${escapeHtml(song.title)}</div>
        <div class="result-artist">${escapeHtml(song.artist)}${meta ? ` · ${escapeHtml(meta)}` : ''}</div>
      </div>
      <button class="add-btn" type="button" aria-label="Add to queue">+</button>
    </div>
  `;
}

async function runSearch(query) {
  const currentRequestId = ++requestId;
  resultsEl.innerHTML = '<div class="loading">Searching…</div>';

  try {
    const res = await fetch(`/unified-search?q=${encodeURIComponent(query)}&limit=12`);
    if (currentRequestId !== requestId) return; // superseded by a newer keystroke
    const data = await res.json();
    if (currentRequestId !== requestId) return;

    if (!res.ok) {
      resultsEl.innerHTML = `<div class="empty-state">${escapeHtml(data.error || 'Search failed.')}</div>`;
      return;
    }

    const songs = data.results || [];
    resultsEl.innerHTML = songs.length
      ? songs.map(renderSongRow).join('')
      : '<div class="empty-state">No songs found.</div>';
  } catch (err) {
    if (currentRequestId !== requestId) return;
    resultsEl.innerHTML = '<div class="empty-state">Network error — is the server running?</div>';
  }
}

export function enqueueSong(song) {
  send({
    type: 'enqueue-song',
    song: {
      artist: song.artist,
      title: song.title,
      cover_art: song.cover_art || '',
      duration_seconds: song.duration_seconds ? Number(song.duration_seconds) : null,
      ytmusic_video_id: song.ytmusic_video_id || null,
    },
  });
  showToast(`Added "${song.title}" to the queue`);
}

export function initSearch() {
  searchInput.addEventListener('input', () => {
    const query = searchInput.value.trim();
    if (debounceTimer) clearTimeout(debounceTimer);

    if (query.length < SEARCH_MIN_QUERY_LENGTH) {
      requestId++; // invalidate any in-flight request too short to matter now
      resultsEl.innerHTML = '';
      return;
    }

    debounceTimer = setTimeout(() => runSearch(query), SEARCH_DEBOUNCE_MS);
  });

  resultsEl.addEventListener('click', (e) => {
    const row = e.target.closest('.result-row');
    if (!row) return;
    enqueueSong({
      artist: row.dataset.artist,
      title: row.dataset.title,
      duration_seconds: row.dataset.duration || null,
      cover_art: row.dataset.cover,
      ytmusic_video_id: row.dataset.ytm,
    });
  });
}
