// Small helpers shared across the search/suggestions/library modules.

export function escapeHtml(str) {
  const div = document.createElement('div');
  div.textContent = str == null ? '' : String(str);
  return div.innerHTML;
}

export function formatDuration(seconds) {
  if (seconds == null) return '';
  seconds = Math.round(Number(seconds));
  if (!Number.isFinite(seconds)) return '';
  const minutes = Math.floor(seconds / 60);
  const secs = seconds % 60;
  return `${minutes}:${String(secs).padStart(2, '0')}`;
}

// Picking a song is the whole journey now - no separate video-picking step.
// The player page resolves and auto-picks the karaoke video itself via
// /select-song once it loads. `ytm` (the original recording's YouTube
// Music video id) rides along so the library worker can extract a
// reference melody for the note guide.
export function openPlayer(artist, title, duration, ytmusicVideoId) {
  const params = new URLSearchParams({ artist, title });
  if (duration) params.set('duration', duration);
  if (ytmusicVideoId) params.set('ytm', ytmusicVideoId);
  window.location.href = `/player?${params.toString()}`;
}
