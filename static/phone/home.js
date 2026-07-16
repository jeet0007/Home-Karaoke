// Phone app shell composition root: view-switching between Home/Search/
// Queue (client-side, so the /room-ws connection persists rather than
// reconnecting on every tab change - see static/phone/socket.js), the
// Home screen's trending/group-picks rows, and the invite QR.

import { escapeHtml, formatDuration } from '../shared/utils.js';
import { connect, onMessage } from './socket.js';
import { initSearch, enqueueSong } from './search.js';
import { initQueue } from './queue.js';
import { showToast } from './toast.js';

const body = document.body;
const code = body.dataset.code;

const inviteQrImg = document.getElementById('invite-qr');
const trendingRowEl = document.getElementById('trending-row');
const groupPicksEl = document.getElementById('group-picks-list');
const homeSearchTrigger = document.getElementById('home-search-trigger');
const searchInputEl = document.getElementById('search-input');
const tabButtons = document.querySelectorAll('.tab-btn');
const views = document.querySelectorAll('.view');
const nowPlayingBanner = document.getElementById('now-playing-banner');
const nowPlayingTitleEl = document.getElementById('now-playing-title');
const nowPlayingOpenLink = document.getElementById('now-playing-open');

function showView(name) {
  views.forEach((view) => view.classList.toggle('active', view.id === `view-${name}`));
  tabButtons.forEach((btn) => btn.classList.toggle('active', btn.dataset.view === name));
  if (name === 'search') searchInputEl.focus();
}

function songFromRowDataset(el) {
  return {
    artist: el.dataset.artist,
    title: el.dataset.title,
    duration_seconds: el.dataset.duration || null,
    cover_art: el.dataset.cover,
    ytmusic_video_id: el.dataset.ytm,
  };
}

function renderTrendingCard(song) {
  const thumb = song.cover_art || '';
  return `
    <div class="trending-card" data-artist="${escapeHtml(song.artist)}" data-title="${escapeHtml(song.title)}" data-duration="${song.duration_seconds != null ? song.duration_seconds : ''}" data-cover="${escapeHtml(thumb)}" data-ytm="${escapeHtml(song.ytmusic_video_id || '')}">
      ${thumb ? `<img class="trending-thumb" src="${escapeHtml(thumb)}" loading="lazy" alt="">` : '<div class="trending-thumb placeholder"></div>'}
      <div class="trending-title">${escapeHtml(song.title)}</div>
      <div class="trending-artist">${escapeHtml(song.artist)}</div>
    </div>
  `;
}

function renderGroupPickRow(song) {
  const thumb = song.cover_art || '';
  const meta = [song.album, formatDuration(song.duration_seconds)].filter(Boolean).join(' · ');
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

async function loadSuggestions() {
  try {
    const res = await fetch('/room-suggestions');
    if (!res.ok) return;
    const data = await res.json();
    trendingRowEl.innerHTML = (data.trending || []).map(renderTrendingCard).join('');
    groupPicksEl.innerHTML = (data.group_picks || []).map(renderGroupPickRow).join('');
  } catch (err) {
    // Best-effort - Home just shows empty sections on failure.
  }
}

function initHomeInteractions() {
  homeSearchTrigger.addEventListener('click', () => showView('search'));
  trendingRowEl.addEventListener('click', (e) => {
    const card = e.target.closest('.trending-card');
    if (card) enqueueSong(songFromRowDataset(card));
  });
  groupPicksEl.addEventListener('click', (e) => {
    const row = e.target.closest('.result-row');
    if (row) enqueueSong(songFromRowDataset(row));
  });
}

function initTabs() {
  tabButtons.forEach((btn) => btn.addEventListener('click', () => showView(btn.dataset.view)));
}

// A song playing surfaces a persistent banner (visible across all three
// tabs, not just Queue) linking to the synced-lyrics + remote-control
// screen - see templates/phone_now_singing.html.
function renderNowPlaying(nowPlaying) {
  if (!nowPlaying) {
    nowPlayingBanner.hidden = true;
    return;
  }
  nowPlayingTitleEl.textContent = `${nowPlaying.title} — ${nowPlaying.artist}`;
  nowPlayingOpenLink.href = `/room/${code}/now-singing`;
  nowPlayingBanner.hidden = false;
}

function initNowPlayingBanner() {
  onMessage('room-snapshot', (payload) => renderNowPlaying(payload.now_playing));
  onMessage('now-playing-change', (payload) => renderNowPlaying(payload.now_playing));
}

function boot() {
  inviteQrImg.src = `/room/${code}/qr.svg`;

  initTabs();
  initHomeInteractions();
  initSearch();
  initQueue();
  initNowPlayingBanner();
  loadSuggestions();

  onMessage('room-closed', () => {
    showToast('TV disconnected — ask them to reopen the Sing Room screen', 5000);
  });

  connect(code);
}

boot();
