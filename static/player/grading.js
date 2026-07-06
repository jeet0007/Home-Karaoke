// -- Live pitch/energy grading (optional) --------------------------
//
// Entirely additive: if the mic is denied/unavailable, or the browser
// lacks WebSocket/AudioContext support, grading silently never starts
// and the rest of the player (video/audio playback + lyrics sync) works
// exactly as it did before this feature existed. See audio_grading.py
// for what the score does and does not measure (no reference-melody
// comparison - pitch stability + voice-activity only).

import { getMelodyNotes, setLivePitch } from './note-guide.js';
import { effectiveLyricMs } from './sync-offset.js';

const audio = document.getElementById('audio');
const scoreValueEl = document.querySelector('.score span');
const GRADING_CHUNK_SAMPLES = 2048;

let gradingStarted = false;
let gradingStream = null;
let gradingAudioContext = null;
let gradingSocket = null;
let gradingSourceNode = null;
let gradingProcessorNode = null;

function setScoreDisplay(text) {
  if (scoreValueEl) scoreValueEl.textContent = text;
}

function renderScoreUpdate(update) {
  if (!update || update.error) return;
  setScoreDisplay(update.singing ? `★ ${update.score}` : '★ --');
  if (update.frequency_hz) {
    setLivePitch(update.frequency_hz);
  }
}

// Position syncs map the mic stream's clock onto song time server-side
// (see audio_grading.RealtimeGrader.set_position) so melody-accuracy
// grading lines up with the backing track; without a melody they're
// harmless no-ops, so they're sent unconditionally.
const POSITION_SYNC_INTERVAL_MS = 1000;
let positionSyncIntervalId = null;

export function sendPositionSync() {
  if (gradingSocket && gradingSocket.readyState === WebSocket.OPEN) {
    // The grader compares the mic to the melody, which is on the lyric
    // timeline - so sync it to the offset-adjusted position too.
    gradingSocket.send(JSON.stringify({ pos_ms: effectiveLyricMs() }));
  }
}

function startPositionSyncs() {
  if (positionSyncIntervalId !== null) return;
  positionSyncIntervalId = setInterval(() => {
    if (!audio.paused) sendPositionSync();
  }, POSITION_SYNC_INTERVAL_MS);
}

export function stopGrading() {
  if (positionSyncIntervalId !== null) {
    clearInterval(positionSyncIntervalId);
    positionSyncIntervalId = null;
  }
  if (gradingProcessorNode) {
    gradingProcessorNode.disconnect();
    gradingProcessorNode.port && (gradingProcessorNode.port.onmessage = null);
    gradingProcessorNode.onaudioprocess = null;
    gradingProcessorNode = null;
  }
  if (gradingSourceNode) {
    gradingSourceNode.disconnect();
    gradingSourceNode = null;
  }
  if (gradingStream) {
    gradingStream.getTracks().forEach((track) => track.stop());
    gradingStream = null;
  }
  if (gradingSocket) {
    const socket = gradingSocket;
    gradingSocket = null;
    try {
      socket.close();
    } catch (err) {
      // already closed/closing - nothing to do
    }
  }
  if (gradingAudioContext) {
    const ctx = gradingAudioContext;
    gradingAudioContext = null;
    ctx.close().catch(() => {});
  }
  setScoreDisplay('★ --');
}

function sendPcmChunk(socket, samples) {
  if (socket.readyState === WebSocket.OPEN) {
    // Float32Array.slice() copies the underlying buffer so it's safe to
    // hand off/transfer even though `samples` may be reused by the audio
    // graph right after this call returns.
    socket.send(samples.slice().buffer);
  }
}

async function attachScriptProcessorFallback(audioContext, source, socket, silentGain) {
  // AudioWorkletNode is preferred (see below); ScriptProcessorNode is
  // deprecated but kept as a fallback for browsers/contexts where
  // audioWorklet.addModule() fails, since it needs no separate module
  // file and is still broadly supported.
  const processor = audioContext.createScriptProcessor(GRADING_CHUNK_SAMPLES, 1, 1);
  processor.onaudioprocess = (event) => {
    sendPcmChunk(socket, event.inputBuffer.getChannelData(0));
  };
  source.connect(processor);
  // Per the Web Audio spec a node only reliably processes once it's part
  // of a graph reachable from the destination; route through a
  // zero-gain node so the mic is never actually audible.
  processor.connect(silentGain);
  gradingProcessorNode = processor;
}

async function attachAudioWorklet(audioContext, source, socket, silentGain) {
  await audioContext.audioWorklet.addModule('/static/grading-worklet.js');
  const node = new AudioWorkletNode(audioContext, 'grading-processor');
  node.port.onmessage = (event) => sendPcmChunk(socket, event.data);
  source.connect(node);
  node.connect(silentGain);
  gradingProcessorNode = node;
}

export async function ensureGradingStarted() {
  if (gradingStarted) return;
  gradingStarted = true;

  if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia || !window.WebSocket) {
    return;
  }

  let stream;
  try {
    stream = await navigator.mediaDevices.getUserMedia({ audio: true });
  } catch (err) {
    // Mic permission denied or no mic available - grading is optional,
    // the rest of the player is unaffected.
    console.warn('Live scoring disabled (mic unavailable):', err.message || err);
    return;
  }
  gradingStream = stream;

  const AudioContextCtor = window.AudioContext || window.webkitAudioContext;
  if (!AudioContextCtor) {
    stopGrading();
    return;
  }
  const audioContext = new AudioContextCtor();
  gradingAudioContext = audioContext;

  const wsProtocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
  let socket;
  try {
    socket = new WebSocket(`${wsProtocol}//${window.location.host}/grade`);
  } catch (err) {
    stopGrading();
    return;
  }
  socket.binaryType = 'arraybuffer';
  gradingSocket = socket;

  socket.addEventListener('open', () => {
    const handshake = { sample_rate: audioContext.sampleRate };
    const melodyNotes = getMelodyNotes();
    if (melodyNotes) handshake.melody = melodyNotes;
    socket.send(JSON.stringify(handshake));
    sendPositionSync();
    startPositionSyncs();
  });
  socket.addEventListener('message', (event) => {
    try {
      renderScoreUpdate(JSON.parse(event.data));
    } catch (err) {
      // ignore malformed/unexpected message
    }
  });
  socket.addEventListener('close', stopGrading);
  socket.addEventListener('error', stopGrading);

  const source = audioContext.createMediaStreamSource(stream);
  gradingSourceNode = source;
  const silentGain = audioContext.createGain();
  silentGain.gain.value = 0;
  silentGain.connect(audioContext.destination);

  try {
    await attachAudioWorklet(audioContext, source, socket, silentGain);
  } catch (err) {
    console.warn('AudioWorklet unavailable, falling back to ScriptProcessorNode:', err.message || err);
    try {
      await attachScriptProcessorFallback(audioContext, source, socket, silentGain);
    } catch (fallbackErr) {
      console.warn('Live scoring disabled (no usable audio processing node):', fallbackErr.message || fallbackErr);
      stopGrading();
    }
  }
}
