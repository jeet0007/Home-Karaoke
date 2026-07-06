// -- Lyric/melody sync offset --------------------------------------
//
// The lyrics + note guide are timed to the ORIGINAL recording, but the
// backing track is a different karaoke video whose intro can be a
// different length - so they drift (e.g. lyrics start singing while the
// intro is still playing). This offset (ms) shifts the lyric/melody
// timeline against the audio clock: positive delays them (use when they
// run ahead of the music). It's applied to lyric highlighting, the note
// guide, and the grading position. Remembered per backing video in
// localStorage.
//
// This module only owns the offset value/display/persistence - it does
// NOT reach into lyrics or grading itself (see static/player/main.js,
// the composition root, for how an offset change is fanned out to both).

export const SYNC_STEP_MS = 200;
const SYNC_LIMIT_MS = 30000;

const audio = document.getElementById('audio');
const syncDisplay = document.getElementById('sync-display');

let syncOffsetMs = 0;
let syncStorageKey = null;

export function effectiveLyricMs() {
  return audio.currentTime * 1000 - syncOffsetMs;
}

function renderSyncDisplay() {
  const seconds = syncOffsetMs / 1000;
  const sign = seconds > 0 ? '+' : '';
  syncDisplay.textContent = `Sync ${sign}${seconds.toFixed(1)}s`;
  syncDisplay.classList.toggle('is-offset', syncOffsetMs !== 0);
}

export function loadSyncOffset(videoId) {
  syncStorageKey = videoId ? `karaoke-sync-${videoId}` : null;
  let stored = 0;
  try {
    if (syncStorageKey) stored = Number(localStorage.getItem(syncStorageKey)) || 0;
  } catch (err) {
    // localStorage unavailable (private mode) - offset just won't persist.
  }
  syncOffsetMs = Math.max(-SYNC_LIMIT_MS, Math.min(SYNC_LIMIT_MS, stored));
  renderSyncDisplay();
}

export function adjustSyncOffset(deltaMs) {
  syncOffsetMs = Math.max(-SYNC_LIMIT_MS, Math.min(SYNC_LIMIT_MS, syncOffsetMs + deltaMs));
  renderSyncDisplay();
  try {
    if (syncStorageKey) localStorage.setItem(syncStorageKey, String(syncOffsetMs));
  } catch (err) {
    // best-effort persistence
  }
}
