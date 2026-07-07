// -- Song library ------------------------------------------------------
//
// GET /library returns every stored song with its queue status. Ready
// songs play instantly (the player takes /select-song's library fast
// path, complete with the melody note guide); pending/processing rows
// show the background worker's progress and auto-refresh until they
// settle; failed rows carry the reason.

import { escapeHtml, formatDuration, openPlayer } from './utils.js';

const librarySection = document.getElementById('library-section');
const libraryCountEl = document.getElementById('library-count');
const libraryListEl = document.getElementById('library-list');

const LIBRARY_REFRESH_MS = 10000;
let libraryRefreshTimer = null;

// Per-stage outcome badges (lyrics / video / guide), from the song's
// `stages` map. Answers "what passed and what failed" at a glance; full
// reasons are in GET /library/song/<id>'s `report`.
const STAGE_LABELS = { lyrics: 'lyrics', video: 'backing', melody: 'guide' };
const STAGE_GLYPH = { ok: '✓', reused: '✓', skipped: '–', failed: '✗' };

function renderStageBadges(stages) {
  const entries = Object.entries(stages || {});
  if (entries.length === 0) return '';
  const badges = entries
    .map(([stage, status]) => {
      const label = STAGE_LABELS[stage] || stage;
      const glyph = STAGE_GLYPH[status] || '?';
      return `<span class="stage-badge ${escapeHtml(status)}" title="${escapeHtml(stage)}: ${escapeHtml(status)}">${glyph} ${escapeHtml(label)}</span>`;
    })
    .join('');
  return `<div class="stage-badges">${badges}</div>`;
}

function renderLibraryRow(song) {
  const playable = song.status === 'ready';
  const metaParts = [song.album, formatDuration(song.duration_seconds)];
  if (song.status === 'failed' && song.error) metaParts.push(song.error);
  const meta = metaParts.filter(Boolean).join(' · ');
  const thumb = song.cover_art || '';
  // Mid-flight rows say WHICH pipeline stage is running (the worker keeps
  // songs.current_stage fresh), so a 2-minute Demucs run reads as
  // "processing · separate" instead of an opaque "processing".
  const chipText =
    song.status === 'processing' && song.current_stage
      ? `processing · ${song.current_stage}`
      : song.status;
  return `
    <div class="song-row${playable ? '' : ' not-playable'}" data-playable="${playable ? '1' : ''}" data-artist="${escapeHtml(song.artist)}" data-title="${escapeHtml(song.title)}" data-duration="${song.duration_seconds != null ? song.duration_seconds : ''}">
      ${thumb ? `<img class="song-thumb" src="${escapeHtml(thumb)}" loading="lazy" alt="">` : ''}
      <div class="song-info">
        <div class="song-title">${escapeHtml(song.title)} <span style="color: var(--muted);">&mdash; ${escapeHtml(song.artist)}</span></div>
        ${meta ? `<div class="song-meta">${escapeHtml(meta)}</div>` : ''}
        ${renderStageBadges(song.stages)}
      </div>
      <span class="status-chip ${escapeHtml(song.status)}">${escapeHtml(chipText)}</span>
    </div>
  `;
}

export async function loadLibrary() {
  try {
    const res = await fetch('/library');
    if (!res.ok) return;
    const data = await res.json();
    const songs = data.songs || [];

    if (songs.length === 0) {
      librarySection.hidden = true;
      return;
    }

    const readyCount = songs.filter((s) => s.status === 'ready').length;
    libraryCountEl.textContent = `(${readyCount} ready of ${songs.length})`;
    libraryListEl.innerHTML = songs.map(renderLibraryRow).join('');
    librarySection.hidden = false;

    // Keep polling only while the background worker still has songs in
    // flight; a settled library stops generating requests.
    const inFlight = songs.some((s) => s.status === 'pending' || s.status === 'processing');
    if (inFlight && libraryRefreshTimer === null) {
      libraryRefreshTimer = setTimeout(() => {
        libraryRefreshTimer = null;
        loadLibrary();
      }, LIBRARY_REFRESH_MS);
    }
  } catch (err) {
    // Best-effort - search works fine without the library strip.
  }
}

export function initLibrary() {
  libraryListEl.addEventListener('click', (e) => {
    const songRow = e.target.closest('.song-row');
    if (songRow && songRow.dataset.playable) {
      openPlayer(songRow.dataset.artist, songRow.dataset.title, songRow.dataset.duration);
    }
  });

  loadLibrary();
}
