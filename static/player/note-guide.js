// -- Visual note guide (optional) -----------------------------------
//
// A scrolling piano-roll of the reference melody: time flows left to
// right, the vertical axis is pitch, and a "now" line sits at 25% of the
// width so most of the panel shows what's coming. The singer's own live
// pitch (from the same /grade updates that drive the score - see
// static/player/grading.js) is drawn as a dot on the now line,
// octave-folded into the melody's display range - matching how the server
// scores accuracy (singing in your own octave is on-pitch). Entirely
// additive: no processed melody, no panel.

const noteGuidePanel = document.getElementById('note-guide-panel');
const noteGuideStatus = document.getElementById('note-guide-status');
const noteGuideCanvas = document.getElementById('note-guide-canvas');
const noteGuideCtx = noteGuideCanvas.getContext('2d');
const bpmChip = document.getElementById('bpm-chip');

const NOTE_GUIDE_LOOKBACK_MS = 2000;
const NOTE_GUIDE_LOOKAHEAD_MS = 6000;
const NOTE_GUIDE_NOW_FRACTION = NOTE_GUIDE_LOOKBACK_MS / (NOTE_GUIDE_LOOKBACK_MS + NOTE_GUIDE_LOOKAHEAD_MS);
const LIVE_PITCH_STALE_MS = 700;

let melodyNotes = null;
let melodyMidiMin = 0;
let melodyMidiMax = 0;

let livePitchMidi = null;
let livePitchAtMs = 0;

function hzToMidi(frequencyHz) {
  return 69 + 12 * Math.log2(frequencyHz / 440);
}

// Called by grading.js with each score update's frequency_hz so the note
// guide can plot the singer's live pitch dot.
export function setLivePitch(frequencyHz) {
  livePitchMidi = hzToMidi(frequencyHz);
  livePitchAtMs = performance.now();
}

// Read by grading.js's WebSocket handshake so it can send the reference
// melody once, if one is loaded.
export function getMelodyNotes() {
  return melodyNotes;
}

// The song's estimated tempo (from librosa, server-side). Shown as a chip
// when available; songs processed without the tempo add-on just don't show
// one. See core/tempo.py.
export function setBpm(bpm) {
  const value = Number(bpm);
  if (Number.isFinite(value) && value > 0) {
    bpmChip.textContent = `♩ ${Math.round(value)} BPM`;
    bpmChip.hidden = false;
  } else {
    bpmChip.hidden = true;
  }
}

export function setupNoteGuide(notes) {
  const cleaned = (notes || [])
    .filter((n) => Number.isFinite(Number(n.start_ms)) && Number.isFinite(Number(n.end_ms)) && Number.isFinite(Number(n.midi)))
    .map((n) => ({ start_ms: Number(n.start_ms), end_ms: Number(n.end_ms), midi: Number(n.midi) }))
    .sort((a, b) => a.start_ms - b.start_ms);
  if (cleaned.length === 0) return false;

  melodyNotes = cleaned;
  const midis = cleaned.map((n) => n.midi);
  melodyMidiMin = Math.min(...midis) - 2;
  melodyMidiMax = Math.max(...midis) + 2;
  noteGuidePanel.hidden = false;
  noteGuidePanel.setAttribute('aria-hidden', 'false');
  noteGuideStatus.hidden = true;
  noteGuideCanvas.hidden = false;
  return true;
}

// First-play UX: a freshly picked song's melody is still being extracted
// in the background, so /select-song returns melody=null and there'd be
// no guide at all. Instead show the panel with a "preparing" message and
// poll the cheap /song-melody endpoint until the worker has stored it,
// then render the guide - no manual reload needed. Best-effort: if it
// never arrives (e.g. ffmpeg missing on the server) the message just
// stays and the rest of the player is unaffected.
const MELODY_POLL_INTERVAL_MS = 8000;
const MELODY_POLL_MAX_ATTEMPTS = 45; // ~6 min, enough for the slow Demucs path
let melodyPollTimer = null;

export function showNoteGuidePreparing() {
  noteGuidePanel.hidden = false;
  noteGuidePanel.setAttribute('aria-hidden', 'false');
  noteGuideCanvas.hidden = true;
  noteGuideStatus.hidden = false;
  noteGuideStatus.innerHTML =
    '<div><div class="spinner" aria-hidden="true"></div>Preparing the note guide for this song&hellip;<br>it will appear here once ready.</div>';
}

export function showNoteGuideUnavailable() {
  noteGuideStatus.innerHTML =
    '<div>No note guide is available for this song.<br>' +
    '(The note guide needs the vocal-transcription add-on installed on the server.)</div>';
}

export async function pollForMelody(artistName, songTitle) {
  let attempts = 0;
  const poll = async () => {
    attempts += 1;
    try {
      const params = new URLSearchParams({ artist: artistName, title: songTitle });
      const res = await fetch(`/song-melody?${params.toString()}`);
      if (res.ok) {
        const data = await res.json();
        if (data.melody && data.melody.length && setupNoteGuide(data.melody)) {
          setBpm(data.bpm);
          return; // guide rendered - stop polling
        }
        // Song finished processing but produced no melody (e.g. the ML
        // add-on isn't installed) - it's never going to appear, so stop
        // polling and say so plainly instead of spinning for minutes.
        if (data.ready) {
          showNoteGuideUnavailable();
          return;
        }
      }
    } catch (err) {
      // best-effort - keep trying until the song is ready or the cap
    }
    if (attempts < MELODY_POLL_MAX_ATTEMPTS) {
      melodyPollTimer = setTimeout(poll, MELODY_POLL_INTERVAL_MS);
    } else {
      showNoteGuideUnavailable();
    }
  };
  melodyPollTimer = setTimeout(poll, MELODY_POLL_INTERVAL_MS);
}

export function stopMelodyPoll() {
  if (melodyPollTimer !== null) {
    clearTimeout(melodyPollTimer);
    melodyPollTimer = null;
  }
}

function resizeNoteGuideCanvasIfNeeded() {
  const rect = noteGuideCanvas.getBoundingClientRect();
  const dpr = window.devicePixelRatio || 1;
  const width = Math.max(1, Math.round(rect.width * dpr));
  const height = Math.max(1, Math.round(rect.height * dpr));
  if (noteGuideCanvas.width !== width || noteGuideCanvas.height !== height) {
    noteGuideCanvas.width = width;
    noteGuideCanvas.height = height;
  }
}

function midiToY(midi, height) {
  const range = Math.max(1, melodyMidiMax - melodyMidiMin);
  const fraction = (midi - melodyMidiMin) / range;
  return height - fraction * height;
}

function foldMidiIntoRange(midi) {
  // Shift by whole octaves until the dot lands inside the melody's
  // displayed range - the same octave-equivalence the server grades with.
  let folded = midi;
  while (folded < melodyMidiMin && folded + 12 <= melodyMidiMax + 6) folded += 12;
  while (folded > melodyMidiMax && folded - 12 >= melodyMidiMin - 6) folded -= 12;
  return folded;
}

export function drawNoteGuide(nowMs) {
  if (!melodyNotes) return;

  resizeNoteGuideCanvasIfNeeded();
  const width = noteGuideCanvas.width;
  const height = noteGuideCanvas.height;
  const dpr = window.devicePixelRatio || 1;
  noteGuideCtx.clearRect(0, 0, width, height);

  const windowStart = nowMs - NOTE_GUIDE_LOOKBACK_MS;
  const windowMs = NOTE_GUIDE_LOOKBACK_MS + NOTE_GUIDE_LOOKAHEAD_MS;
  const barHeight = Math.max(3 * dpr, height / Math.max(1, melodyMidiMax - melodyMidiMin) * 0.9);

  for (const note of melodyNotes) {
    if (note.end_ms < windowStart) continue;
    if (note.start_ms > windowStart + windowMs) break;

    const x0 = ((note.start_ms - windowStart) / windowMs) * width;
    const x1 = ((note.end_ms - windowStart) / windowMs) * width;
    const y = midiToY(note.midi, height);
    const active = nowMs >= note.start_ms && nowMs < note.end_ms;

    if (active) {
      noteGuideCtx.fillStyle = '#ff4d6d';
    } else if (note.end_ms <= nowMs) {
      noteGuideCtx.fillStyle = 'rgba(255, 255, 255, 0.22)';
    } else {
      noteGuideCtx.fillStyle = 'rgba(255, 255, 255, 0.55)';
    }
    const barWidth = Math.max(2, x1 - x0 - 1);
    if (typeof noteGuideCtx.roundRect === 'function') {
      noteGuideCtx.beginPath();
      noteGuideCtx.roundRect(x0, y - barHeight / 2, barWidth, barHeight, barHeight / 2);
      noteGuideCtx.fill();
    } else {
      noteGuideCtx.fillRect(x0, y - barHeight / 2, barWidth, barHeight);
    }
  }

  // The "now" line.
  const nowX = NOTE_GUIDE_NOW_FRACTION * width;
  noteGuideCtx.fillStyle = 'rgba(255, 255, 255, 0.35)';
  noteGuideCtx.fillRect(nowX, 0, Math.max(1, dpr), height);

  // The singer's live pitch dot.
  if (livePitchMidi !== null && performance.now() - livePitchAtMs < LIVE_PITCH_STALE_MS) {
    const y = midiToY(foldMidiIntoRange(livePitchMidi), height);
    noteGuideCtx.fillStyle = '#4dd28c';
    noteGuideCtx.beginPath();
    noteGuideCtx.arc(nowX, Math.max(0, Math.min(height, y)), 5 * dpr, 0, Math.PI * 2);
    noteGuideCtx.fill();
  }
}
