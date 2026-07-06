/* tslint:disable */
/* eslint-disable */

/**
 * Direct port of `core/audio_grading.RealtimeGrader`. Feed it PCM via
 * `push_samples()` as chunks arrive (any chunk size - a AudioWorklet
 * quantum of 128 samples works fine, this buffers internally exactly
 * like the Python version); it returns zero or more score updates,
 * throttled to roughly `HOP_SECONDS` apart regardless of caller chunk
 * size.
 */
export class Grader {
    free(): void;
    [Symbol.dispose](): void;
    /**
     * `melody_json`, if provided, is a JSON array of `{start_ms, end_ms,
     * midi}` objects (the shape `note-guide.js`'s `getMelodyNotes()`
     * already produces) - pass `None`/`undefined` for no-reference
     * (stability-only) grading.
     */
    constructor(sample_rate: number, melody_json?: string | null);
    /**
     * Push a chunk of mono float32 PCM samples. Returns a JSON string
     * encoding an array of zero or more score-update objects (each
     * shaped like `{t_ms, singing, frequency_hz, target_midi, score}`,
     * matching `RealtimeGrader.push_samples()`'s Python dicts).
     */
    push_samples(chunk: Float32Array): string;
    /**
     * Sync: the backing track is at `pos_ms` right now. Maps the mic
     * stream's sample clock onto song time for melody lookups; call
     * repeatedly (the player does so every ~1s), so pause/seek/drift
     * self-correct at the next sync.
     */
    set_position(pos_ms: number): void;
}

export type InitInput = RequestInfo | URL | Response | BufferSource | WebAssembly.Module;

export interface InitOutput {
    readonly memory: WebAssembly.Memory;
    readonly __wbg_grader_free: (a: number, b: number) => void;
    readonly grader_new: (a: number, b: number, c: number) => number;
    readonly grader_push_samples: (a: number, b: number, c: number) => [number, number];
    readonly grader_set_position: (a: number, b: number) => void;
    readonly __wbindgen_externrefs: WebAssembly.Table;
    readonly __wbindgen_malloc: (a: number, b: number) => number;
    readonly __wbindgen_realloc: (a: number, b: number, c: number, d: number) => number;
    readonly __wbindgen_free: (a: number, b: number, c: number) => void;
    readonly __wbindgen_start: () => void;
}

export type SyncInitInput = BufferSource | WebAssembly.Module;

/**
 * Instantiates the given `module`, which can either be bytes or
 * a precompiled `WebAssembly.Module`.
 *
 * @param {{ module: SyncInitInput }} module - Passing `SyncInitInput` directly is deprecated.
 *
 * @returns {InitOutput}
 */
export function initSync(module: { module: SyncInitInput } | SyncInitInput): InitOutput;

/**
 * If `module_or_path` is {RequestInfo} or {URL}, makes a request and
 * for everything else, calls `WebAssembly.instantiate` directly.
 *
 * @param {{ module_or_path: InitInput | Promise<InitInput> }} module_or_path - Passing `InitInput` directly is deprecated.
 *
 * @returns {Promise<InitOutput>}
 */
export default function __wbg_init (module_or_path?: { module_or_path: InitInput | Promise<InitInput> } | InitInput | Promise<InitInput>): Promise<InitOutput>;
