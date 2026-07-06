// -- Singer-assist track (optional) ----------------------------------
//
// Plays the pipeline's isolated vocal stem (core/artifacts.KIND_VOCALS -
// the Demucs output already produced for the note guide) alongside the
// backing track, so a singer can dial in how much of the original vocal
// to hear as a guide. Entirely additive: songs processed without the ML
// add-on (see vocal_transcribe.py) have no stem, and the toggle stays
// hidden - this never blocks or alters normal playback.

const mainAudio = document.getElementById('audio');
const singerAudio = document.getElementById('singer-audio');
const toggleBtn = document.getElementById('singer-assist-toggle');
const volumeSlider = document.getElementById('singer-assist-volume');

// Loosely synced to the main track (re-anchored on play/pause/seek below,
// not a continuous polling loop) - close enough that drift isn't audible
// for a guide track, without fighting the browser over two clocks.
const RESYNC_THRESHOLD_S = 0.15;

let songId = null;
let enabled = false;

function syncTime() {
  if (Math.abs(singerAudio.currentTime - mainAudio.currentTime) > RESYNC_THRESHOLD_S) {
    singerAudio.currentTime = mainAudio.currentTime;
  }
}

function setToggleState(isEnabled) {
  enabled = isEnabled;
  toggleBtn.setAttribute('aria-pressed', String(isEnabled));
  toggleBtn.textContent = isEnabled ? 'Singer: On' : 'Singer: Off';
  volumeSlider.hidden = !isEnabled;
}

function enable() {
  if (!songId) return;
  if (!singerAudio.src) {
    singerAudio.src = `/library/song/${songId}/vocals`;
    singerAudio.volume = Number(volumeSlider.value);
  }
  setToggleState(true);
  syncTime();
  if (!mainAudio.paused) {
    singerAudio.play().catch(() => {});
  }
}

function disable() {
  setToggleState(false);
  singerAudio.pause();
}

// Called once per song load with the id + whether a vocal stem exists
// (payload.has_singer_vocals from /select-song's library fast path - live,
// not-yet-processed songs simply have none yet). Resets any previous
// song's toggle state so a new song never inherits a stale playing track.
export function setupSingerAssist(id, available) {
  disable();
  singerAudio.removeAttribute('src');
  songId = available ? id : null;
  toggleBtn.hidden = !available;
}

toggleBtn.addEventListener('click', () => {
  if (enabled) disable();
  else enable();
});

volumeSlider.addEventListener('input', () => {
  singerAudio.volume = Number(volumeSlider.value);
});

mainAudio.addEventListener('play', () => {
  if (!enabled) return;
  syncTime();
  singerAudio.play().catch(() => {});
});
mainAudio.addEventListener('pause', () => {
  if (enabled) singerAudio.pause();
});
mainAudio.addEventListener('seeked', () => {
  if (enabled) syncTime();
});
