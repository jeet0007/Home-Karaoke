// The main song search (/unified-search) and its results list. The
// typeahead suggestions dropdown (search.js's sibling, suggestions.js) is a
// separate, faster path that bypasses this entirely for an exact pick.

import { escapeHtml, formatDuration, openPlayer } from './utils.js';
import { closeSuggestions } from './suggestions.js';

const queryInput = document.getElementById('query');
const searchBtn = document.getElementById('search-btn');
const statusEl = document.getElementById('status');
const warningEl = document.getElementById('warning-banner');
const resultsEl = document.getElementById('results');

export function setStatus(html, isError) {
  statusEl.innerHTML = html;
  statusEl.className = isError ? 'error' : '';
}

export function setWarning(message) {
  if (!message) {
    warningEl.style.display = 'none';
    warningEl.textContent = '';
    return;
  }
  warningEl.textContent = message;
  warningEl.style.display = 'block';
}

function renderSongRow(song) {
  const meta = [song.album, formatDuration(song.duration_seconds)].filter(Boolean).join(' · ');
  const thumb = song.cover_art || '';
  return `
    <div class="song-row" data-artist="${escapeHtml(song.artist)}" data-title="${escapeHtml(song.title)}" data-duration="${song.duration_seconds != null ? song.duration_seconds : ''}" data-ytm="${escapeHtml(song.ytmusic_video_id || '')}">
      ${thumb ? `<img class="song-thumb" src="${escapeHtml(thumb)}" loading="lazy" alt="">` : ''}
      <div class="song-info">
        <div class="song-title">${escapeHtml(song.title)} <span style="color: var(--muted);">&mdash; ${escapeHtml(song.artist)}</span></div>
        ${meta ? `<div class="song-meta">${escapeHtml(meta)}</div>` : ''}
      </div>
    </div>
  `;
}

export async function runSearch() {
  const query = queryInput.value.trim();
  if (!query) return;

  closeSuggestions();
  resultsEl.innerHTML = '';
  setWarning('');
  setStatus('<div class="spinner"></div>', false);
  searchBtn.disabled = true;

  try {
    const res = await fetch(`/unified-search?q=${encodeURIComponent(query)}&limit=12`);
    const data = await res.json();

    if (!res.ok) {
      setStatus(escapeHtml(data.error || 'Search failed.'), true);
      return;
    }

    setWarning(data.warning || '');

    const songs = data.results || [];
    if (songs.length === 0) {
      setStatus('');
      resultsEl.innerHTML = `<div class="empty-state">No songs with lyrics found for "${escapeHtml(query)}". Try a different search.</div>`;
      return;
    }

    setStatus(`${songs.length} song${songs.length === 1 ? '' : 's'} found — pick one to play`, false);
    resultsEl.innerHTML = songs.map(renderSongRow).join('');
  } catch (err) {
    setStatus('Network error — is the server running?', true);
  } finally {
    searchBtn.disabled = false;
  }
}

export function initSearch() {
  resultsEl.addEventListener('click', (e) => {
    const songRow = e.target.closest('.song-row');
    if (songRow) {
      openPlayer(songRow.dataset.artist, songRow.dataset.title, songRow.dataset.duration, songRow.dataset.ytm);
    }
  });

  searchBtn.addEventListener('click', runSearch);
}
