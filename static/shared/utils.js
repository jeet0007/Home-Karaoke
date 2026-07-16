// Small helpers shared across the player/tv/phone modules.

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
