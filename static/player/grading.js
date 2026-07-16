// -- Live pitch/energy grading (optional) --------------------------
//
// Entirely additive: if the mic is denied/unavailable, or every grading
// backend below is unusable, grading silently never starts and the rest
// of the player (video/audio playback + lyrics sync) works exactly as it
// did before this feature existed.
//
// Three backends, tried in order, each one a graceful degradation of the
// last (mirroring this codebase's existing "mic denied -> skip",
// "AudioWorklet unavailable -> ScriptProcessorNode" precedents):
//
//   1. AudioWorklet + WASM (static/grading-worklet.js + wasm/grading/,
//      the Rust port of core/audio_grading.py). Runs on the browser's
//      dedicated real-time audio thread; no network round trip.
//   2. ScriptProcessorNode + the *same* WASM module, run on the main
//      thread instead - used when audioWorklet.addModule() itself fails
//      (e.g. an insecure context, or a very old browser). Still no
//      network round trip, just not off the main thread.
//   3. The original /grade WebSocket, scored server-side by
//      core/audio_grading.RealtimeGrader - used only if WebAssembly
//      itself is unavailable, or fetching/compiling the .wasm module
//      fails. Kept deliberately unchanged as the final safety net; see
//      the PR description for why this was kept rather than removed.
//
// See audio_grading.py's module docstring for what the score does and
// does not measure (no reference-melody comparison unless a melody was
// loaded - pitch stability + voice-activity only otherwise).

import { getMelodyNotes, setLivePitch } from './note-guide.js';
import { effectiveLyricMs } from './sync-offset.js';

const audio = document.getElementById('audio');
const scoreValueEl = document.querySelector('.score span');
const GRADING_CHUNK_SAMPLES = 2048;

const WASM_DIR = '/static/player/wasm';
const WASM_MODULE_URL = `${WASM_DIR}/grading_wasm_bg.wasm`;
const WASM_GLUE_URL = `${WASM_DIR}/grading_wasm.js`;

let gradingStarted = false;
let gradingStream = null;
let gradingAudioContext = null;
let gradingSocket = null;
let gradingSourceNode = null;
let gradingProcessorNode = null;
let mainThreadGrader = null; // only set for the ScriptProcessorNode+WASM backend
// null | 'worklet' | 'scriptprocessor-wasm' | 'websocket' - which of the
// three backends above is currently active, so sendPositionSync() and
// stopGrading() know how to route/tear down.
let gradingBackend = null;

// Where the mic's song-position clock comes from, and how updates are
// surfaced beyond this module's own score display - injectable via
// ensureGradingStarted(options) so a phone (no local <audio>, no local
// playback state) can drive grading from a RemoteClock extrapolating the
// TV's playback-position broadcasts (static/shared/clock.js) and forward
// its own scores back to the TV, instead of this module's original
// TV-only assumptions. Defaults preserve the exact original behavior.
let getPositionMs = effectiveLyricMs;
let isAudioPlaying = () => !audio.paused;
let onScoreUpdate = null;

function setScoreDisplay(text) {
  if (scoreValueEl) scoreValueEl.textContent = text;
}

function renderScoreUpdate(update) {
  if (!update || update.error) return;
  setScoreDisplay(update.singing ? `★ ${update.score}` : '★ --');
  if (update.frequency_hz) {
    setLivePitch(update.frequency_hz);
  }
  if (onScoreUpdate) onScoreUpdate(update);
}

// Position syncs map the mic stream's clock onto song time (client-side
// for the WASM backends - see Grader.set_position() in
// wasm/grading/src/lib.rs; server-side via
// audio_grading.RealtimeGrader.set_position for the WebSocket backend) so
// melody-accuracy grading lines up with the backing track; without a
// melody they're harmless no-ops, so they're sent unconditionally.
const POSITION_SYNC_INTERVAL_MS = 1000;
let positionSyncIntervalId = null;

export function sendPositionSync() {
  const posMs = getPositionMs();
  if (gradingBackend === 'worklet' && gradingProcessorNode) {
    gradingProcessorNode.port.postMessage({ pos_ms: posMs });
  } else if (gradingBackend === 'scriptprocessor-wasm' && mainThreadGrader) {
    mainThreadGrader.set_position(posMs);
  } else if (gradingBackend === 'websocket' && gradingSocket && gradingSocket.readyState === WebSocket.OPEN) {
    // The grader compares the mic to the melody, which is on the lyric
    // timeline - so sync it to the offset-adjusted position too.
    gradingSocket.send(JSON.stringify({ pos_ms: posMs }));
  }
}

function startPositionSyncs() {
  if (positionSyncIntervalId !== null) return;
  positionSyncIntervalId = setInterval(() => {
    if (isAudioPlaying()) sendPositionSync();
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
  if (mainThreadGrader) {
    // Releases the WASM-side Grader's linear-memory allocation
    // immediately rather than waiting on GC + FinalizationRegistry.
    mainThreadGrader.free();
    mainThreadGrader = null;
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
  gradingBackend = null;
  setScoreDisplay('★ --');
  getPositionMs = effectiveLyricMs;
  isAudioPlaying = () => !audio.paused;
  onScoreUpdate = null;
}

// -- Backend 1: AudioWorklet + WASM (preferred) --------------------------

async function compileWasmModule() {
  const response = await fetch(WASM_MODULE_URL);
  if (!response.ok) {
    throw new Error(`Fetching ${WASM_MODULE_URL} failed: ${response.status}`);
  }
  const contentType = response.headers.get('Content-Type') || '';
  if (typeof WebAssembly.compileStreaming === 'function' && contentType.startsWith('application/wasm')) {
    return WebAssembly.compileStreaming(response);
  }
  // Some servers (or an older Python `mimetypes` module that doesn't
  // know the .wasm extension) don't serve it as application/wasm, which
  // compileStreaming requires exactly - compiling from bytes works
  // regardless of Content-Type.
  const bytes = await response.arrayBuffer();
  return WebAssembly.compile(bytes);
}

async function attachAudioWorklet(audioContext, source, silentGain, melodyJson) {
  // Compiled on the main thread (which has fetch/WebAssembly.compileStreaming)
  // and handed to the worklet as a WebAssembly.Module via
  // processorOptions - AudioWorkletGlobalScope has neither fetch() nor a
  // usable async wasm-bindgen init(). See static/grading-worklet.js.
  const wasmModule = await compileWasmModule();
  await audioContext.audioWorklet.addModule('/static/grading-worklet.js');
  const node = new AudioWorkletNode(audioContext, 'grading-processor', {
    processorOptions: { wasmModule, melodyJson },
  });
  node.port.onmessage = (event) => {
    if (event.data && event.data.error) {
      // Note: a throw *inside* the worklet's constructor (e.g. WASM
      // instantiation failing only in that realm) doesn't reject this
      // function's promise per the AudioWorklet spec - it can only be
      // observed here, after the fact, as an inert processor. There is
      // deliberately no cascade to the next backend in that case; see
      // the PR description.
      console.warn('grading-worklet reported an error:', event.data.error);
      return;
    }
    renderScoreUpdate(event.data);
  };
  source.connect(node);
  node.connect(silentGain);
  gradingProcessorNode = node;
  gradingBackend = 'worklet';
}

// -- Backend 2: ScriptProcessorNode + the same WASM module, main thread --

async function attachWasmScriptProcessor(audioContext, source, silentGain, melodyJson) {
  const { default: initWasm, Grader } = await import(WASM_GLUE_URL);
  // The default async loader fetches+instantiates itself - fine here,
  // unlike inside the worklet, since the main thread has fetch().
  await initWasm();
  mainThreadGrader = new Grader(audioContext.sampleRate, melodyJson);

  // ScriptProcessorNode is deprecated but kept as a fallback for
  // contexts where audioWorklet.addModule() fails, since it needs no
  // separate module file and is still broadly supported.
  const processor = audioContext.createScriptProcessor(GRADING_CHUNK_SAMPLES, 1, 1);
  processor.onaudioprocess = (event) => {
    const updatesJson = mainThreadGrader.push_samples(event.inputBuffer.getChannelData(0));
    if (updatesJson && updatesJson !== '[]') {
      for (const update of JSON.parse(updatesJson)) renderScoreUpdate(update);
    }
  };
  source.connect(processor);
  // Per the Web Audio spec a node only reliably processes once it's part
  // of a graph reachable from the destination; route through a
  // zero-gain node so the mic is never actually audible.
  processor.connect(silentGain);
  gradingProcessorNode = processor;
  gradingBackend = 'scriptprocessor-wasm';
}

// -- Backend 3: original server-side /grade WebSocket (final fallback) --

async function attachWebSocketGrading(audioContext, source, silentGain, melodyNotes) {
  const wsProtocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
  const socket = new WebSocket(`${wsProtocol}//${window.location.host}/grade`);
  socket.binaryType = 'arraybuffer';
  gradingSocket = socket;
  gradingBackend = 'websocket';

  socket.addEventListener('open', () => {
    const handshake = { sample_rate: audioContext.sampleRate };
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

  const processor = audioContext.createScriptProcessor(GRADING_CHUNK_SAMPLES, 1, 1);
  processor.onaudioprocess = (event) => {
    if (socket.readyState === WebSocket.OPEN) {
      // .slice() copies the underlying buffer so it's safe to hand
      // off/transfer even though the source may be reused by the audio
      // graph right after this call returns.
      socket.send(event.inputBuffer.getChannelData(0).slice().buffer);
    }
  };
  source.connect(processor);
  processor.connect(silentGain);
  gradingProcessorNode = processor;
}

// `options.getPositionMs`/`options.isPlaying` override where the mic's
// song-position clock and play/pause state come from (both default to the
// TV's own <audio> element); `options.onScoreUpdate(update)` is called
// alongside this module's own score display for every update, letting a
// caller forward scores elsewhere (e.g. a phone relaying them to the TV
// over /room-ws - see static/phone/now-singing.js).
export async function ensureGradingStarted(options = {}) {
  if (gradingStarted) return;
  gradingStarted = true;
  if (options.getPositionMs) getPositionMs = options.getPositionMs;
  if (options.isPlaying) isAudioPlaying = options.isPlaying;
  if (options.onScoreUpdate) onScoreUpdate = options.onScoreUpdate;

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

  const source = audioContext.createMediaStreamSource(stream);
  gradingSourceNode = source;
  const silentGain = audioContext.createGain();
  silentGain.gain.value = 0;
  silentGain.connect(audioContext.destination);

  // Captured once, at grading start - matching the previous
  // handshake-only behavior (a melody that finishes processing mid-song
  // isn't picked up retroactively; same as before this port).
  const melodyNotes = getMelodyNotes();
  const melodyJson = melodyNotes ? JSON.stringify(melodyNotes) : undefined;

  try {
    await attachAudioWorklet(audioContext, source, silentGain, melodyJson);
  } catch (err) {
    console.warn('WASM AudioWorklet grading unavailable, falling back to main-thread WASM:', err.message || err);
    try {
      await attachWasmScriptProcessor(audioContext, source, silentGain, melodyJson);
    } catch (fallbackErr) {
      console.warn(
        'Main-thread WASM grading unavailable, falling back to server-side /grade:',
        fallbackErr.message || fallbackErr,
      );
      try {
        await attachWebSocketGrading(audioContext, source, silentGain, melodyNotes);
      } catch (wsErr) {
        console.warn('Live scoring disabled (no usable grading backend):', wsErr.message || wsErr);
        stopGrading();
        return;
      }
    }
  }

  // The WASM backends have no handshake round trip to key a first sync
  // off of - they apply position updates immediately - so kick one off
  // here. The WebSocket backend does the same from its own 'open'
  // handler above instead, since it needs the socket open first.
  if (gradingBackend !== 'websocket') {
    sendPositionSync();
    startPositionSyncs();
  }
}
