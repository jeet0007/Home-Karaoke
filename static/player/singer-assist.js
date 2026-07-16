// -- Singer-assist track (optional) ----------------------------------
//
// Plays the pipeline's isolated vocal stem (core/artifacts.KIND_VOCALS -
// the Demucs output already produced for the note guide) alongside the
// backing track, so a singer can dial in how much of the original vocal
// to hear as a guide. Entirely additive: songs processed without the ML
// add-on (see vocal_transcribe.py) have no stem, and the toggle stays
// hidden - this never blocks or alters normal playback.
//
// The vocal stem was extracted from the ORIGINAL recording, so its own
// timeline is the same one lyrics/melody are timed to - the offset-
// adjusted "lyric timeline" (sync-offset.js's effectiveLyricMs()), NOT
// the backing track's raw currentTime. Those two only coincide when the
// sync offset is exactly 0, which is why this used to look "in sync with
// the lyrics" (both ignoring the offset the same way) while drifting from
// whatever's actually audible in the backing track - and would have gone
// out of sync with lyrics the moment someone corrected that with the Sync
// control, since only the lyrics/note-guide were listening to it.

import { effectiveLyricMs } from './sync-offset.js';

const mainAudio = document.getElementById('audio');
const singerAudio = document.getElementById('singer-audio');
const toggleBtn = document.getElementById('singer-assist-toggle');
const volumeSlider = document.getElementById('singer-assist-volume');

// Re-anchored on play/pause/seek and every animation frame (see main.js's
// syncLyrics loop) rather than a continuous polling loop - close enough
// that drift isn't audible for a guide track, without fighting the
// browser over two clocks. The animation-frame call also catches
// independent clock drift between the two <audio> elements over a long
// song, which discrete events alone wouldn't.
const RESYNC_THRESHOLD_S = 0.15;

let songId = null;
let enabled = false;

function targetSeconds() {
  return Math.max(0, effectiveLyricMs() / 1000);
}

function syncTime() {
  // Don't stack a new seek on top of one the browser is still resolving -
  // this loop runs every animation frame, so without this guard a slow
  // seek (e.g. still buffering) gets re-issued before it lands, which can
  // thrash instead of converging.
  if (singerAudio.seeking) return;
  const target = targetSeconds();
  if (Math.abs(singerAudio.currentTime - target) > RESYNC_THRESHOLD_S) {
    singerAudio.currentTime = target;
  }
}

// Exported for main.js to call from its per-frame lyric/note-guide sync
// loop and immediately after a manual Sync ([ / ]) adjustment - a no-op
// whenever the track isn't toggled on.
export function resync() {
  if (enabled) syncTime();
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

// Called by static/player/room-broadcast.js when a phone's voice-assist
// blend slider changes (see core/rooms.py's set_voice_assist_volume) - the
// TV keeps its own on-screen slider in sync rather than hiding it, since
// either device can adjust the blend. A nonzero remote volume also turns
// the stem on if it wasn't already - the singer moving the slider clearly
// wants to hear some of it, and toggling it on manually first would be a
// pointless extra step for the one control this feature actually exposes
// remotely.
export function setVolumeRemote(volume) {
  const clamped = Math.max(0, Math.min(1, Number(volume)));
  volumeSlider.value = String(clamped);
  singerAudio.volume = clamped;
  if (clamped > 0 && !enabled) enable();
}

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
