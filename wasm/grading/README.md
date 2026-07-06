# karaoke-grading-wasm

Rust port of `core/audio_grading.py`'s YIN pitch detector + `RealtimeGrader`
scoring, compiled to WebAssembly. This is the primary live-grading backend,
run client-side inside `static/grading-worklet.js` (an AudioWorklet, so it
executes on the browser's dedicated real-time audio thread with zero
network round-trip). See `static/player/grading.js` for the full
worklet -> main-thread-WASM -> server-WebSocket fallback ladder this sits
at the top of.

Rust is a **dev-time-only** dependency: the compiled artifacts are checked
into `static/player/wasm/` (see below), so running the app
(`python app.py`) needs no Rust toolchain at all, exactly as before this
crate existed.

## One-time dev-machine setup

```bash
curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh   # rustup: stable toolchain
rustup target add wasm32-unknown-unknown
cargo install wasm-pack --locked                                  # or the official installer, if it works on your platform/arch
```

## Rebuilding the WASM artifact

Whenever `wasm/grading/src/lib.rs` changes, rebuild and re-check-in the
compiled output:

```bash
cd wasm/grading
wasm-pack build --target web --release --out-dir ../../static/player/wasm --out-name grading_wasm

# wasm-pack drops a `.gitignore` containing `*` into the output dir by
# default, which would make `git add` silently skip the very artifacts
# this workflow exists to check in - delete it before committing:
rm -f ../../static/player/wasm/.gitignore
```

This produces (all checked into git under `static/player/wasm/`):

- `grading_wasm_bg.wasm` - the compiled module, fetched+compiled on the
  main thread (`static/player/grading.js`'s `compileWasmModule()`) since
  `AudioWorkletGlobalScope` has no `fetch()`.
- `grading_wasm.js` - wasm-bindgen's glue: the `Grader` class, the
  fetch-based async `init()` (used by the main-thread
  ScriptProcessorNode fallback), and the fetch-free `initSync()` (used by
  the worklet, which is handed an already-compiled `WebAssembly.Module`
  instead of a URL).
- `grading_wasm.d.ts` / `grading_wasm_bg.wasm.d.ts` - TypeScript types,
  unused at runtime (no TS in this project) but harmless to keep for
  editor tooling.

## Testing

`cargo test` runs the native (non-WASM) unit tests in `src/lib.rs` -
pure-Rust DSP logic, no `wasm-bindgen` JS interop involved, so this needs
no browser/Node and no wasm target:

```bash
cd wasm/grading
cargo test
```

These tests assert the same properties the Python reference test suite
(`tests/test_audio_grading.py`) checks: YIN detects known frequencies
within tolerance, silence yields no pitch, octave-folding matches
Python's floor-modulo `%` semantics (Rust's `%` on floats does not - see
`octave_folded_cents_off`'s doc comment), population (ddof=0) stddev
matches `np.std`'s default, and a full `Grader` session scores on-pitch
singing highly / scores silence at 0.

### Reproducing the cross-check

`wasm/grading/cross_check/` drives the Python reference (`RealtimeGrader`)
and the *actual compiled WASM artifact* (via Node, not just native Rust)
through identical synthetic input - on-pitch, off-pitch above/below the
melody target, silence, and non-tonal noise, with and without a
reference melody - and diffs the two side by side. This is stronger
evidence than `cargo test` alone since it exercises the real
`wasm-bindgen` JS boundary (string-in/string-out JSON, `Option<String>`
melody, `&[f32]` sample slices) rather than only the pure-Rust logic
underneath it.

It needs a separate `--target nodejs` build (the checked-in
`static/player/wasm/` build is `--target web`, whose glue assumes
browser-only `fetch`/`import.meta.url` that a plain Node process doesn't
provide the same way) - built once into a gitignored `pkg-node/`
directory, not checked in:

```bash
cd wasm/grading
wasm-pack build --target nodejs --release --out-dir cross_check/pkg-node --out-name grading_wasm
cd ../..
python3 wasm/grading/cross_check/cross_check_python.py
```

Expect one line per scenario per implementation (`n`, last score,
singing flag, target MIDI, mean detected frequency, pitched-frame count)
followed by `ALL SCENARIOS MATCH WITHIN TOLERANCE` - scores are allowed
to differ by a few points (float rounding through the EMA/std chain) and
mean frequency by <1Hz, but singing flags and rough scores must agree.

### Known gap: WASM support inside `AudioWorkletGlobalScope`

The primary (AudioWorklet) path's generated glue (`grading_wasm.js`)
instantiates a `TextEncoder`/`TextDecoder` at module load time (needed
for the `&str`/`String` boundary chosen to avoid an extra
`serde-wasm-bindgen` dependency) and uses `console.warn` for
diagnostics. Per the WHATWG Encoding (`TextEncoder`/`TextDecoder`) and
Console specs, both are declared `[Exposed=*]` - available in *every*
JavaScript global, `AudioWorkletGlobalScope` included - so current
spec-conformant browsers should run tier 1 fine. This was a real,
previously-reported gap in some engines (see e.g. the WebKit `console`
bug and various "TextDecoder missing in AudioWorklet" writeups predating
the `Exposed=*` widening), so a browser that predates that fix would
throw on `import` at worklet-load time; `static/player/grading.js` treats
that as `addModule()` rejecting and falls through to the
ScriptProcessorNode+WASM tier automatically - no user-visible break, just
one tier down (main thread instead of the audio thread). Not verified
against a real browser in this change (no headless browser available in
this environment - see the PR description); the Node cross-check above
can't observe this, since Node provides both globals unconditionally.
