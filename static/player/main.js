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
import { enableControls, setPlayButton, togglePlayback, restartPlayback, loadStream, loadLocalTrack } from './playback.js';
import { setupSingerAssist, resync as resyncSingerAssist } from './singer-assist.js';
import { initRoomBroadcast, reportResolved } from './room-broadcast.js';

const { title: songTitle, artist, duration: durationHint, ytmusicVideoId } = window.PLAYER_CONFIG;

const coverArt = document.getElementById('cover-art');
const artBackgroundImg = document.getElementById('art-background-img');
const songTitleEl = document.getElementById('song-title-text');
const songArtistEl = document.getElementById('song-artist-text');
const audio = document.getElementById('audio');
const playPauseBtn = document.getElementById('play-pause');
const restartBtn = document.getElementById('restart');
const syncEarlierBtn = document.getElementById('sync-earlier');
const syncLaterBtn = document.getElementById('sync-later');
const syncGroup = document.querySelector('.sync-group');

let animationFrameId = null;

// True when the backing audio is the library's instrumental artifact (the
// original recording minus its Demucs vocal stem). In that mode the audio,
// lyrics, note guide, and singer-assist stem all live on the original
// recording's timeline - in sync by construction - so the whole sync-offset
// mechanism (a correction for playing a DIFFERENT karaoke upload) is moot
// and its controls are hidden and inert.
let singleSource = false;

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
  if (singleSource) return;
  adjustSyncOffset(deltaMs);
  resyncLyrics(effectiveLyricMs());
  resyncSingerAssist();
  sendPositionSync();
}

function syncLyrics() {
  // Lyrics + note guide follow the (offset-adjusted) lyric timeline; the
  // singer-assist track re-anchors to the same timeline here too, so it
  // can't silently drift over a long song between discrete play/seek events.
  const lyricMs = effectiveLyricMs();
  updateCurrentLyric(lyricMs);
  drawNoteGuide(lyricMs);
  resyncSingerAssist();
  animationFrameId = requestAnimationFrame(syncLyrics);
}

const LYRICS_RETRY_DELAY_MS = 6000;

// Loads a song by identity rather than closing over the page's own
// PLAYER_CONFIG, so it can be called more than once per page load: once at
// boot for whatever song the URL names, and again by room-broadcast.js
// every time the room's TV advances to a new queue entry - the player page
// itself is never reloaded for a room's later songs (see
// static/player/room-broadcast.js). Returns the /select-song response on
// success, or null on failure (nothing meaningful to report to phones in
// that case - the existing overlay error state is enough).
async function loadSongByIdentity({ title: songTitle, artist, duration: durationHint, ytmusicVideoId }) {
  setOverlayLoading('Looking up lyrics and a matching video...');
  songTitleEl.textContent = songTitle;
  songArtistEl.textContent = artist;
  coverArt.hidden = true;
  artBackgroundImg.classList.remove('is-loaded');

  try {
    const params = new URLSearchParams({ artist, title: songTitle });
    if (durationHint) params.set('duration', durationHint);
    if (ytmusicVideoId) params.set('ytmusic_video_id', ytmusicVideoId);

    const res = await fetch(`/select-song?${params.toString()}`);
    const data = await res.json();

    if (!res.ok) {
      setOverlayError(data.error || 'Could not load this song.');
      showLyricsFallback();
      return null;
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
    setupSingerAssist(data.song_id, Boolean(data.has_singer_vocals));

    const syncedLines = (data.lyrics && data.lyrics.synced) || [];
    renderLyrics(syncedLines);
    if (syncedLines.length === 0) {
      setTimeout(() => retryLyricsOnce(artist, songTitle, durationHint), LYRICS_RETRY_DELAY_MS);
    }

    singleSource = Boolean(data.has_instrumental);
    syncGroup.hidden = singleSource;
    setArtBackground(data.video_id);
    reportResolved(data);

    if (singleSource) {
      // Preferred path: play the original recording's own instrumental
      // (see the singleSource comment above). The offset stays 0 - a saved
      // per-video nudge belongs to the karaoke-upload fallback, not here.
      loadSyncOffset(null);
      loadLocalTrack(`/library/song/${data.song_id}/instrumental`);
      return data;
    }

    if (!data.video_id) {
      setOverlayNoBackingTrack(data.message || 'No backing track found for this song.');
      return data;
    }

    // Fallback: stream the picked karaoke upload's audio. Restore any sync
    // nudge saved for this backing track on a past visit.
    loadSyncOffset(data.video_id);
    await loadStream(data.video_id);
    return data;
  } catch (err) {
    setOverlayError('Network error — is the server running?');
    showLyricsFallback();
    return null;
  }
}

playPauseBtn.addEventListener('click', togglePlayback);
restartBtn.addEventListener('click', restartPlayback);
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
loadSongByIdentity({ title: songTitle, artist, duration: durationHint, ytmusicVideoId });
initRoomBroadcast(loadSongByIdentity);
animationFrameId = requestAnimationFrame(syncLyrics);
window.addEventListener('beforeunload', () => {
  cancelAnimationFrame(animationFrameId);
  stopMelodyPoll();
  stopGrading();
});
