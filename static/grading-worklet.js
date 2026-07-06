// AudioWorkletProcessor that runs live pitch/energy grading entirely on
// the browser's dedicated real-time audio thread.
//
// This is the Rust/WASM port of core/audio_grading.py's YIN pitch
// detector + RealtimeGrader (see wasm/grading/, the Rust source, and its
// generated glue at static/player/wasm/grading_wasm.js - built via
// `wasm-pack build --target web`, see wasm/grading/README.md for the
// one-time rebuild command). Scores are produced right here, with zero
// network round-trip - see static/player/grading.js for the full
// three-tier fallback ladder this sits at the top of (AudioWorklet+WASM
// -> ScriptProcessorNode+WASM on the main thread -> the original /grade
// WebSocket, in that order).
//
// If WASM setup fails for any reason (processorOptions carries no
// compiled module, or instantiation throws), this processor is inert;
// static/player/grading.js only wires this node up after successfully
// compiling the WASM module on the main thread, so a throw here is
// unexpected and only reported for diagnostics, not recovered from - its
// caller falls back based on whether score updates ever arrive.
//
// Loading WASM inside an AudioWorkletGlobalScope has two restrictions
// that shape this file: there is no `fetch()` here, and the default
// async wasm-bindgen `init()` needs one. So the *compiled*
// WebAssembly.Module is fetched+compiled on the main thread instead (see
// static/player/grading.js) and handed to this worklet via
// AudioWorkletNodeOptions.processorOptions, where `initSync()` -
// wasm-bindgen's synchronous, fetch-free entry point - instantiates it
// straight from that already-compiled Module (no I/O needed here at
// all). `import` of a same-origin ES module does work inside
// AudioWorkletGlobalScope (worklets are loaded via `addModule`, which is
// module-aware, unlike a plain <script>) - see the official
// wasm-bindgen wasm-audio-worklet example this design follows.
import { initSync, Grader } from './player/wasm/grading_wasm.js';

class GradingProcessor extends AudioWorkletProcessor {
  constructor(options) {
    super();
    this._grader = null;
    const processorOptions = (options && options.processorOptions) || {};
    const { wasmModule, melodyJson } = processorOptions;

    if (wasmModule) {
      try {
        initSync({ module: wasmModule });
        // `sampleRate` is a global provided by AudioWorkletGlobalScope
        // (the owning AudioContext's rate) - no need to pass it through
        // processorOptions.
        this._grader = new Grader(sampleRate, melodyJson || undefined);
      } catch (err) {
        this.port.postMessage({ error: `grading-worklet WASM init failed: ${(err && err.message) || err}` });
      }
    }

    // Position syncs (see static/player/grading.js's sendPositionSync)
    // arrive here as {pos_ms} messages so melody-accuracy grading can
    // map the mic stream's sample clock onto song time - see
    // RealtimeGrader.set_position()'s Python counterpart for why this is
    // sent periodically rather than once (pause/seek/drift correct at
    // the next sync).
    this.port.onmessage = (event) => {
      if (this._grader && event.data && typeof event.data.pos_ms === 'number') {
        this._grader.set_position(event.data.pos_ms);
      }
    };
  }

  process(inputs) {
    if (!this._grader) return true;
    const channelData = inputs[0] && inputs[0][0];
    if (channelData && channelData.length) {
      const updatesJson = this._grader.push_samples(channelData);
      if (updatesJson && updatesJson !== '[]') {
        for (const update of JSON.parse(updatesJson)) {
          this.port.postMessage(update);
        }
      }
    }
    return true;
  }
}

registerProcessor('grading-processor', GradingProcessor);
