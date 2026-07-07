// Audio playback controls (play/pause/restart) and stream-URL resolution.
// The video is never shown - only its audio track streams in the
// background (see templates/player.html for why an <audio> element, not a
// hidden <video>, drives playback); the cover art is the whole visual
// surface. No volume control: this plays on a TV (remote) or device
// (hardware/OS controls already cover it).

import { setOverlayLoading, setOverlayError, hideOverlay } from './overlay.js';
import { ensureGradingStarted } from './grading.js';
import { resync as resyncLyrics } from './lyrics.js';
import { effectiveLyricMs } from './sync-offset.js';

const audio = document.getElementById('audio');
const playPauseBtn = document.getElementById('play-pause');
const restartBtn = document.getElementById('restart');

export function enableControls() {
  [playPauseBtn, restartBtn].forEach((button) => {
    button.disabled = false;
  });
}

export function setPlayButton() {
  playPauseBtn.textContent = audio.paused ? 'Play' : 'Pause';
}

export async function togglePlayback() {
  if (!audio.src) return;

  audio.muted = false;
  if (audio.paused) {
    try {
      await audio.play();
    } catch (err) {
      setOverlayError('The browser blocked playback until you interact with the page.');
    }
    ensureGradingStarted();
  } else {
    audio.pause();
  }
  setPlayButton();
}

export async function restartPlayback() {
  if (!audio.src) return;

  audio.muted = false;
  audio.currentTime = 0;
  resyncLyrics(effectiveLyricMs());
  try {
    await audio.play();
  } catch (err) {
    setOverlayError('The browser blocked playback until you interact with the page.');
  }
  ensureGradingStarted();
  setPlayButton();
}

// Single-source mode: the backing track is a library artifact (the
// original recording's instrumental), served by this app - nothing to
// resolve, no expiring upstream URL.
export function loadLocalTrack(url) {
  audio.src = url;
  audio.load();
  hideOverlay();
  enableControls();
}

export async function loadStream(videoId) {
  setOverlayLoading('Resolving the playable stream...');

  try {
    const res = await fetch(`/stream-url?video_id=${encodeURIComponent(videoId)}`);
    const data = await res.json();
    if (!res.ok || !data.stream_url) {
      throw new Error(data.error || 'The stream URL could not be resolved.');
    }

    if (data.warning) {
      console.warn(data.warning);
    }

    audio.src = data.stream_url;
    audio.load();
    hideOverlay();
    enableControls();
  } catch (err) {
    setOverlayError(err.message || 'The stream URL could not be resolved.');
  }
}
