// Synced-lyrics rendering + current-line tracking for the lyrics panel.

const lyricsPanel = document.getElementById('lyrics-panel');

let lyrics = [];
let lyricNodes = [];
let currentLyricIndex = -1;

export function showLyricsFallback() {
  lyricsPanel.innerHTML = '<div class="lyrics-empty">No lyrics found &mdash; enjoy the music!</div>';
}

export function renderLyrics(syncedLines) {
  const cleaned = (syncedLines || [])
    .filter((line) => Number.isFinite(Number(line.time_ms)) && line.text)
    .map((line) => ({ time_ms: Number(line.time_ms), text: String(line.text) }))
    .sort((a, b) => a.time_ms - b.time_ms);

  if (cleaned.length === 0) {
    showLyricsFallback();
    return;
  }

  lyrics = cleaned;
  lyricsPanel.innerHTML = '';
  lyricNodes = lyrics.map((line) => {
    const node = document.createElement('div');
    node.className = 'lyric-line upcoming';
    node.textContent = line.text;
    lyricsPanel.appendChild(node);
    return node;
  });
}

function findCurrentLyricIndex(nowMs) {
  let low = 0;
  let high = lyrics.length - 1;
  let result = -1;

  while (low <= high) {
    const mid = Math.floor((low + high) / 2);
    if (lyrics[mid].time_ms <= nowMs) {
      result = mid;
      low = mid + 1;
    } else {
      high = mid - 1;
    }
  }

  return result;
}

export function updateCurrentLyric(nowMs) {
  if (lyrics.length === 0) return;

  const nextIndex = findCurrentLyricIndex(nowMs);
  if (nextIndex === currentLyricIndex) return;

  currentLyricIndex = nextIndex;
  lyricNodes.forEach((node, index) => {
    node.classList.toggle('past', index < currentLyricIndex);
    node.classList.toggle('current', index === currentLyricIndex);
    node.classList.toggle('upcoming', index > currentLyricIndex);
  });

  if (currentLyricIndex >= 0) {
    lyricNodes[currentLyricIndex].scrollIntoView({ behavior: 'smooth', block: 'center' });
  }
}

// Resets the current-line pointer and re-places it against `nowMs` - used
// whenever the lyric timeline jumps discontinuously (restart, sync-offset
// nudge) instead of advancing frame-by-frame.
export function resync(nowMs) {
  currentLyricIndex = -1;
  updateCurrentLyric(nowMs);
}

// /select-song's lyrics fetch now runs with a generous timeout (see
// lyrica_client.py), but Lyrica itself can still take longer than that to
// finish caching a slow source (e.g. a struggling LRCLIB response) - in
// that window /select-song already returned with no lyrics. One retry a
// few seconds later, straight against Lyrica via the existing /lyrics
// route, catches the case where the result showed up moments too late
// without holding up the initial response. Best-effort only: on any
// failure the "no lyrics" fallback already on screen simply stays.
export async function retryLyricsOnce(artistName, songTitle, durationHint) {
  try {
    const params = new URLSearchParams({ artist: artistName, title: songTitle });
    if (durationHint) params.set('duration', durationHint);

    const res = await fetch(`/lyrics?${params.toString()}`);
    if (!res.ok) return;

    const data = await res.json();
    renderLyrics(data.synced);
  } catch (err) {
    // Best-effort - leave the existing fallback state as-is.
  }
}
