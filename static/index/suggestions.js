// -- Typeahead suggestions --------------------------------------------
//
// /song-suggestions is a thin, unfiltered wrapper around ytmusicapi search
// (no lyrics-availability check), so it's fast enough to call on every
// keystroke. Picking a suggestion jumps straight to /player (and from
// there /select-song) for that exact song, skipping the slow
// /unified-search lyrics-filter fan-out entirely. Enter/Search without
// picking a suggestion still falls back to the full /unified-search flow
// via search.js's runSearch() - see main.js, which owns the combined
// keydown handling since it's the one place these two modules' concerns
// overlap.

import { escapeHtml, formatDuration, openPlayer } from './utils.js';

const queryInput = document.getElementById('query');
const suggestionsEl = document.getElementById('suggestions');

const SUGGESTION_DEBOUNCE_MS = 275;
const SUGGESTION_MIN_QUERY_LENGTH = 2;
const SUGGESTION_LIMIT = 8;

let suggestionDebounceTimer = null;
let suggestionRequestId = 0; // bumped per request; only the latest id's response is rendered, so a slow stale reply can never clobber a newer one
let suggestionAbortController = null;
let currentSuggestions = [];
let activeSuggestionIndex = -1;
let suggestionsOpen = false;

export function isSuggestionsOpen() {
  return suggestionsOpen;
}

export function hasSuggestions() {
  return currentSuggestions.length > 0;
}

export function hasActiveSuggestion() {
  return activeSuggestionIndex >= 0;
}

export function closeSuggestions() {
  suggestionsOpen = false;
  suggestionsEl.classList.remove('open');
  suggestionsEl.innerHTML = '';
  currentSuggestions = [];
  activeSuggestionIndex = -1;
}

function renderSuggestionItem(song, index) {
  const meta = [song.album, formatDuration(song.duration_seconds)].filter(Boolean).join(' · ');
  const thumb = song.cover_art || '';
  return `
    <div class="suggestion-item${index === activeSuggestionIndex ? ' active' : ''}" data-index="${index}">
      ${thumb ? `<img class="suggestion-thumb" src="${escapeHtml(thumb)}" loading="lazy" alt="">` : ''}
      <div class="suggestion-text">
        <div class="suggestion-title">${escapeHtml(song.title)} <span style="color: var(--muted);">&mdash; ${escapeHtml(song.artist)}</span></div>
        ${meta ? `<div class="suggestion-meta">${escapeHtml(meta)}</div>` : ''}
      </div>
    </div>
  `;
}

function showSuggestionStatus(message) {
  suggestionsOpen = true;
  suggestionsEl.classList.add('open');
  suggestionsEl.innerHTML = `<div class="suggestion-status">${escapeHtml(message)}</div>`;
}

function showSuggestionList(songs) {
  suggestionsOpen = true;
  suggestionsEl.classList.add('open');
  suggestionsEl.innerHTML = songs.map((song, i) => renderSuggestionItem(song, i)).join('');
}

async function fetchSuggestions(query) {
  const requestId = ++suggestionRequestId;
  if (suggestionAbortController) suggestionAbortController.abort();
  suggestionAbortController = new AbortController();

  showSuggestionStatus('Loading…');

  try {
    const res = await fetch(
      `/song-suggestions?q=${encodeURIComponent(query)}&limit=${SUGGESTION_LIMIT}`,
      { signal: suggestionAbortController.signal }
    );
    // A newer keystroke superseded this request while it was in flight -
    // drop the response so it can't overwrite what's now on screen.
    if (requestId !== suggestionRequestId) return;

    const data = await res.json();
    if (requestId !== suggestionRequestId) return;

    if (!res.ok) {
      showSuggestionStatus(data.error || 'Suggestion lookup failed.');
      return;
    }

    currentSuggestions = data.results || [];
    activeSuggestionIndex = -1;
    if (currentSuggestions.length === 0) {
      showSuggestionStatus('No matches yet — keep typing, or press Enter to search.');
      return;
    }
    showSuggestionList(currentSuggestions);
  } catch (err) {
    if (err.name === 'AbortError' || requestId !== suggestionRequestId) return;
    showSuggestionStatus('Network error fetching suggestions.');
  }
}

function scheduleSuggestions(rawQuery) {
  if (suggestionDebounceTimer) clearTimeout(suggestionDebounceTimer);

  const query = rawQuery.trim();
  if (query.length < SUGGESTION_MIN_QUERY_LENGTH) {
    suggestionRequestId++; // invalidate any in-flight request too short to matter now
    if (suggestionAbortController) suggestionAbortController.abort();
    closeSuggestions();
    return;
  }

  suggestionDebounceTimer = setTimeout(() => fetchSuggestions(query), SUGGESTION_DEBOUNCE_MS);
}

function selectSuggestion(song) {
  closeSuggestions();
  openPlayer(song.artist, song.title, song.duration_seconds, song.ytmusic_video_id);
}

export function moveActiveSuggestion(delta) {
  if (currentSuggestions.length === 0) return;
  activeSuggestionIndex = Math.max(-1, Math.min(currentSuggestions.length - 1, activeSuggestionIndex + delta));
  showSuggestionList(currentSuggestions);
}

export function selectActiveSuggestion() {
  if (activeSuggestionIndex < 0) return;
  selectSuggestion(currentSuggestions[activeSuggestionIndex]);
}

export function initSuggestions() {
  queryInput.addEventListener('input', () => scheduleSuggestions(queryInput.value));

  // mousedown (not click) fires before the input's implicit blur, so the
  // dropdown is still in the DOM and hasn't been torn down by a blur handler
  // when we read which suggestion was picked.
  suggestionsEl.addEventListener('mousedown', (e) => {
    const item = e.target.closest('.suggestion-item');
    if (!item) return;
    e.preventDefault();
    const index = Number(item.dataset.index);
    selectSuggestion(currentSuggestions[index]);
  });

  document.addEventListener('click', (e) => {
    if (!e.target.closest('.search-bar-wrapper')) closeSuggestions();
  });
}
