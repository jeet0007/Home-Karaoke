// Phone "Now Singing" screen: synced lyrics (static/player/lyrics.js,
// reused unmodified - updateCurrentLyric(nowMs)/resync(nowMs) already take
// an explicit position argument rather than reading a local <audio>
// element) driven by a RemoteClock extrapolated from the TV's periodic
// playback-position broadcasts, plus remote playback control buttons that
// route through the TV via core/rooms.py's route_playback_control (never
// controls audio directly - the phone has no audio of its own to control,
// mic input is only ever used for local scoring, never streamed).

import { renderLyrics, showLyricsFallback, updateCurrentLyric, resync } from '../player/lyrics.js';
import { ensureGradingStarted } from '../player/grading.js';
import { createRemoteClock } from '../shared/clock.js';
import { connect, onMessage, send } from './socket.js';

const code = document.body.dataset.code;
const titleEl = document.getElementById('ns-title');
const artistEl = document.getElementById('ns-artist');
const toggleBtn = document.getElementById('ctl-toggle');
const restartBtn = document.getElementById('ctl-restart');
const skipBtn = document.getElementById('ctl-skip');
const micBtn = document.getElementById('ctl-mic');
const voiceAssistSlider = document.getElementById('voice-assist-slider');

const clock = createRemoteClock();
let currentEntryId = null;
let animationFrameId = null;
let remotePlaying = false;

function applyNowPlaying(nowPlaying) {
  if (!nowPlaying) {
    titleEl.textContent = 'Waiting for a song…';
    artistEl.textContent = '';
    currentEntryId = null;
    voiceAssistSlider.hidden = true;
    showLyricsFallback();
    return;
  }

  titleEl.textContent = nowPlaying.title;
  artistEl.textContent = nowPlaying.artist;
  // A vocal stem only exists for library-processed songs (see
  // core/artifacts.py's KIND_VOCALS) - the blend slider is meaningless
  // without one, same as the TV's own singer-assist toggle.
  voiceAssistSlider.hidden = !(nowPlaying.resolved && nowPlaying.resolved.has_singer_vocals);

  const synced = (nowPlaying.resolved && nowPlaying.resolved.lyrics && nowPlaying.resolved.lyrics.synced) || [];
  if (synced.length) {
    renderLyrics(synced);
  } else {
    showLyricsFallback();
  }

  if (nowPlaying.entry_id !== currentEntryId) {
    currentEntryId = nowPlaying.entry_id;
    resync(clock.nowMs());
  }
}

function loop() {
  updateCurrentLyric(clock.nowMs());
  animationFrameId = requestAnimationFrame(loop);
}

toggleBtn.addEventListener('click', () => send({ type: 'playback-control', action: 'toggle' }));
restartBtn.addEventListener('click', () => send({ type: 'playback-control', action: 'restart' }));
skipBtn.addEventListener('click', () => send({ type: 'playback-control', action: 'skip' }));

// Mic capture needs an explicit tap (a user gesture some browsers require
// before granting getUserMedia, and the wireframe's own mic-button
// affordance) rather than starting automatically. See grading.js's
// ensureGradingStarted() for the position-source/score-forwarding options
// this passes - the TV's own copy of this same function defaults to its
// local <audio> clock instead.
micBtn.addEventListener('click', async () => {
  micBtn.disabled = true;
  micBtn.textContent = 'Starting…';
  await ensureGradingStarted({
    getPositionMs: () => clock.nowMs(),
    isPlaying: () => remotePlaying,
    onScoreUpdate: (update) => send({ type: 'score-update', score: update.score, singing: update.singing, frequency_hz: update.frequency_hz }),
  });
  micBtn.disabled = false;
  micBtn.setAttribute('aria-pressed', 'true');
  micBtn.textContent = 'Mic: On';
});

voiceAssistSlider.addEventListener('input', () => {
  send({ type: 'voice-assist-volume', volume: Number(voiceAssistSlider.value) });
});

onMessage('room-snapshot', (payload) => applyNowPlaying(payload.now_playing));
onMessage('now-playing-change', (payload) => applyNowPlaying(payload.now_playing));
onMessage('playback-position', (payload) => {
  remotePlaying = Boolean(payload.playing);
  clock.update(payload.pos_ms, remotePlaying);
});

connect(code);
animationFrameId = requestAnimationFrame(loop);
window.addEventListener('beforeunload', () => cancelAnimationFrame(animationFrameId));
