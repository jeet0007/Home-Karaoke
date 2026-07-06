// Cross-check harness: drives the actual compiled WASM Grader through
// five scenarios (on-pitch, off-pitch above target, off-pitch below
// target, silence, noise), with and without a reference melody, and
// prints one JSON line per scenario for cross_check_python.py to diff
// against.
//
// This does NOT run against the browser-target build checked into
// static/player/wasm/ (that build's glue assumes `fetch`/`import.meta.url`,
// which a plain Node process doesn't provide the way a browser does).
// Instead it needs a `--target nodejs` build of the same source, built
// once into ./pkg-node/ (gitignored - see README.md's "Reproducing the
// cross-check" section for the exact command). Both builds compile the
// identical wasm/grading/src/lib.rs; only the JS glue differs.
const { Grader } = require('./pkg-node/grading_wasm.js');

const SAMPLE_RATE = 44100;

function sine(freq, seconds, amp = 0.5) {
  const n = Math.floor(SAMPLE_RATE * seconds);
  const out = new Float32Array(n);
  for (let i = 0; i < n; i++) {
    out[i] = amp * Math.sin((2 * Math.PI * freq * i) / SAMPLE_RATE);
  }
  return out;
}

function noise(seconds, amp = 0.5, seed = 42) {
  // Deterministic PRNG (mulberry32) so Python can reproduce identical
  // samples without sharing a random source across languages.
  let a = seed;
  function rand() {
    a |= 0;
    a = (a + 0x6d2b79f5) | 0;
    let t = Math.imul(a ^ (a >>> 15), 1 | a);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  }
  const n = Math.floor(SAMPLE_RATE * seconds);
  const out = new Float32Array(n);
  for (let i = 0; i < n; i++) {
    out[i] = amp * (rand() * 2 - 1);
  }
  return out;
}

function feedInChunks(grader, samples, chunkMs = 20) {
  const chunkN = Math.max(1, Math.floor((SAMPLE_RATE * chunkMs) / 1000));
  const updates = [];
  for (let start = 0; start < samples.length; start += chunkN) {
    const chunk = samples.slice(start, start + chunkN);
    const json = grader.push_samples(chunk);
    for (const u of JSON.parse(json)) updates.push(u);
  }
  return updates;
}

const MELODY_A4 = JSON.stringify([{ start_ms: 0.0, end_ms: 5000.0, midi: 69.0 }]); // A4 = 440Hz

const scenarios = [
  {
    name: 'on_pitch_no_melody',
    melody: undefined,
    samples: sine(440.0, 1.0),
    setPosition: null,
  },
  {
    name: 'on_pitch_with_melody',
    melody: MELODY_A4,
    samples: sine(440.0, 1.0),
    setPosition: 0.0,
  },
  {
    name: 'off_pitch_above_target_with_melody',
    // Sung a tritone above the target (A4=440Hz) -> D#5/Eb5 ~= 622.25Hz
    melody: MELODY_A4,
    samples: sine(622.25, 1.0),
    setPosition: 0.0,
  },
  {
    name: 'off_pitch_below_target_with_melody',
    // Sung 5 semitones *below* the target (A4=midi 69) -> E4 = midi 64,
    // ~329.63Hz. This is the case that specifically exercises
    // octave_folded_cents_off's rem_euclid vs Python's floor-modulo `%` -
    // Rust's plain `%` operator would diverge only in this direction.
    melody: MELODY_A4,
    samples: sine(329.63, 1.0),
    setPosition: 0.0,
  },
  {
    name: 'silence',
    melody: undefined,
    samples: new Float32Array(Math.floor(SAMPLE_RATE * 1.0)),
    setPosition: null,
  },
  {
    name: 'noise',
    melody: undefined,
    samples: noise(1.0),
    setPosition: null,
  },
];

const results = {};
for (const scenario of scenarios) {
  const grader = new Grader(SAMPLE_RATE, scenario.melody);
  if (scenario.setPosition !== null) grader.set_position(scenario.setPosition);
  const updates = feedInChunks(grader, scenario.samples);
  results[scenario.name] = updates;
  grader.free();
}

console.log(JSON.stringify(results, null, 2));
