// Composition root for the player page: wires together the leaf modules
// (overlay, sync-offset, lyrics, note-guide, grading, playback), owns the
// handful of things that genuinely cross module boundaries (the lyric/note
// -guide animation loop, and fanning a sync-offset change out to both the
// lyrics panel and the grader), and bootstraps the page.
//
// `window.PLAYER_CONFIG` is set by a small inline <script> in
// templates/player.html so the Jinja-rendered song identity can reach this
// static, server-agnostic module.

import { setOverlayLoading, setOverlayError, setOverlayNoBackingTrack } from './overlay.js';
import { SYNC_STEP_MS, effectiveLyricMs, loadSyncOffset, adjustSyncOffset } from './sync-offset.js';
import { renderLyrics, showLyricsFallback, retryLyricsOnce, updateCurrentLyric, resync as resyncLyrics } from './lyrics.js';
import {
  setupNoteGuide,
  showNoteGuidePreparing,
  showNoteGuideUnavailable,
  pollForMelody,
  stopMelodyPoll,
  setBpm,
  drawNoteGuide,
} from './note-guide.js';
import { ensureGradingStarted, stopGrading, sendPositionSync } from './grading.js';
import { enableControls, setPlayButton, togglePlayback, restartPlayback, changeVolume, loadStream } from './playback.js';

const { title: songTitle, artist, duration: durationHint, ytmusicVideoId } = window.PLAYER_CONFIG;

const coverArt = document.getElementById('cover-art');
const artBackgroundImg = document.getElementById('art-background-img');
const audio = document.getElementById('audio');
const playPauseBtn = document.getElementById('play-pause');
const restartBtn = document.getElementById('restart');
const volumeDownBtn = document.getElementById('volume-down');
const volumeUpBtn = document.getElementById('volume-up');
const syncEarlierBtn = document.getElementById('sync-earlier');
const syncLaterBtn = document.getElementById('sync-later');

let animationFrameId = null;

// Layer 1 of the player background: the song's own YouTube thumbnail,
// blurred full-bleed behind the lyrics/note-guide (see the .art-background
// rules in player.html). ThumbnailFallback (static/thumbnail-fallback.js)
// walks maxresdefault -> sddefault -> hqdefault -> mqdefault since
// maxresdefault doesn't exist for every upload. Best-effort: if every
// quality misses, the background just stays the plain --bg fill it already
// has.
function setArtBackground(videoId) {
  if (!videoId || typeof ThumbnailFallback === 'undefined') return;
  ThumbnailFallback.attachThumbnailFallback(artBackgroundImg, videoId, () => {
    artBackgroundImg.classList.add('is-loaded');
  });
}

// A sync-offset nudge ([  / ] keys, or the Lyrics -/+ buttons) shifts the
// lyric/melody timeline discontinuously, so the current lyric line and the
// grader's song-position clock both need to be re-placed immediately
// instead of waiting for the next animation frame / sync tick.
function handleSyncAdjust(deltaMs) {
  adjustSyncOffset(deltaMs);
  resyncLyrics(effectiveLyricMs());
  sendPositionSync();
}

function syncLyrics() {
  // Lyrics + note guide follow the (offset-adjusted) lyric timeline.
  const lyricMs = effectiveLyricMs();
  updateCurrentLyric(lyricMs);
  drawNoteGuide(lyricMs);
  animationFrameId = requestAnimationFrame(syncLyrics);
}

const LYRICS_RETRY_DELAY_MS = 6000;

async function loadSong() {
  setOverlayLoading('Looking up lyrics and a matching video...');

  try {
    const params = new URLSearchParams({ artist, title: songTitle });
    if (durationHint) params.set('duration', durationHint);
    if (ytmusicVideoId) params.set('ytmusic_video_id', ytmusicVideoId);

    const res = await fetch(`/select-song?${params.toString()}`);
    const data = await res.json();

    if (!res.ok) {
      setOverlayError(data.error || 'Could not load this song.');
      showLyricsFallback();
      return;
    }

    if (data.cover_art) {
      coverArt.src = data.cover_art;
      coverArt.hidden = false;
    }

    // Reference melody only exists for library-processed songs. A
    // first-time pick returns null while the background worker is still
    // extracting it - show a "preparing" state and poll until it lands,
    // rather than leaving the guide silently blank.
    if (data.melody && data.melody.length) {
      setupNoteGuide(data.melody);
    } else if (ytmusicVideoId) {
      showNoteGuidePreparing();
      pollForMelody(artist, songTitle);
    }
    setBpm(data.bpm);

    const syncedLines = (data.lyrics && data.lyrics.synced) || [];
    renderLyrics(syncedLines);
    if (syncedLines.length === 0) {
      setTimeout(() => retryLyricsOnce(artist, songTitle, durationHint), LYRICS_RETRY_DELAY_MS);
    }

    if (!data.video_id) {
      setOverlayNoBackingTrack(data.message || 'No backing track found for this song.');
      return;
    }

    // Restore any sync nudge saved for this backing track on a past visit.
    loadSyncOffset(data.video_id);
    setArtBackground(data.video_id);
    await loadStream(data.video_id);
  } catch (err) {
    setOverlayError('Network error — is the server running?');
    showLyricsFallback();
  }
}

playPauseBtn.addEventListener('click', togglePlayback);
restartBtn.addEventListener('click', restartPlayback);
volumeDownBtn.addEventListener('click', () => changeVolume(-0.1));
volumeUpBtn.addEventListener('click', () => changeVolume(0.1));
syncEarlierBtn.addEventListener('click', () => handleSyncAdjust(-SYNC_STEP_MS));
syncLaterBtn.addEventListener('click', () => handleSyncAdjust(SYNC_STEP_MS));

audio.addEventListener('play', setPlayButton);
audio.addEventListener('pause', setPlayButton);
audio.addEventListener('ended', setPlayButton);
// Re-sync the grader's song clock immediately on resume/scrub instead of
// waiting up to a full interval tick with a stale position.
audio.addEventListener('play', sendPositionSync);
audio.addEventListener('seeked', sendPositionSync);
audio.addEventListener('error', () => {
  setOverlayError('The resolved stream was rejected by the browser or expired.');
});

document.addEventListener('keydown', (e) => {
  const tagName = e.target.tagName;
  if (tagName === 'INPUT' || tagName === 'TEXTAREA' || e.target.isContentEditable) return;

  if (e.code === 'Space') {
    e.preventDefault();
    togglePlayback();
  } else if (e.key.toLowerCase() === 'r') {
    restartPlayback();
  } else if (e.key === 'ArrowUp') {
    e.preventDefault();
    changeVolume(0.1);
  } else if (e.key === 'ArrowDown') {
    e.preventDefault();
    changeVolume(-0.1);
  } else if (e.key === '[') {
    e.preventDefault();
    handleSyncAdjust(-SYNC_STEP_MS);
  } else if (e.key === ']') {
    e.preventDefault();
    handleSyncAdjust(SYNC_STEP_MS);
  }
});

audio.autoplay = true;
audio.muted = true;
loadSong();
animationFrameId = requestAnimationFrame(syncLyrics);
window.addEventListener('beforeunload', () => {
  cancelAnimationFrame(animationFrameId);
  stopMelodyPoll();
  stopGrading();
});
