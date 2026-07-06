"""Cross-check harness: drives the Python reference RealtimeGrader through
the same scenarios as cross_check_wasm.js (on-pitch, off-pitch above/below
the melody target, silence, noise; with/without a reference melody), on
bit-identical synthetic input (including a Python port of the JS
mulberry32 PRNG used for the noise scenario), then diffs against the WASM
run's JSON output.

Usage (from repo root, after building the Node-target WASM package - see
README.md's "Reproducing the cross-check"):

    python3 wasm/grading/cross_check/cross_check_python.py

Exits non-zero if any scenario falls outside tolerance.
"""
import json
import os
import subprocess
import sys

import numpy as np

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", ".."))
sys.path.insert(0, REPO_ROOT)

from core import audio_grading as ag  # noqa: E402

SAMPLE_RATE = 44100
MASK32 = 0xFFFFFFFF


def imul(x, y):
    return (x * y) & MASK32


def mulberry32(seed):
    """Python port of the mulberry32 PRNG used by cross_check_wasm.js's
    noise scenario, bit-for-bit - see that file for the JS source this
    mirrors line-for-line. All ops are done as unsigned 32-bit values
    (masked with MASK32); this is safe because every JS bitwise op used
    there (^, >>>, Math.imul) only depends on the 32-bit *bit pattern*,
    never on signed/unsigned interpretation.
    """
    state = seed & MASK32

    def rand():
        nonlocal state
        state = (state + 0x6D2B79F5) & MASK32
        a = state
        t = imul(a ^ (a >> 15), a | 1)
        old_t = t
        t = (old_t + imul(old_t ^ (old_t >> 7), old_t | 61)) & MASK32
        t = t ^ old_t
        return ((t ^ (t >> 14)) & MASK32) / 4294967296.0

    return rand


def sine(freq, seconds, amp=0.5):
    n = int(SAMPLE_RATE * seconds)
    t = np.arange(n) / SAMPLE_RATE
    return (amp * np.sin(2 * np.pi * freq * t)).astype(np.float32)


def noise(seconds, amp=0.5, seed=42):
    rand = mulberry32(seed)
    n = int(SAMPLE_RATE * seconds)
    return np.array([amp * (rand() * 2 - 1) for _ in range(n)], dtype=np.float32)


def feed_in_chunks(grader, samples, chunk_ms=20):
    chunk_n = max(1, int(SAMPLE_RATE * chunk_ms / 1000))
    updates = []
    for start in range(0, len(samples), chunk_n):
        chunk = samples[start : start + chunk_n]
        updates.extend(grader.push_samples(chunk))
    return updates


MELODY_A4 = [{"start_ms": 0.0, "end_ms": 5000.0, "midi": 69.0}]


def build_scenarios():
    return [
        ("on_pitch_no_melody", None, sine(440.0, 1.0), None),
        ("on_pitch_with_melody", MELODY_A4, sine(440.0, 1.0), 0.0),
        ("off_pitch_above_target_with_melody", MELODY_A4, sine(622.25, 1.0), 0.0),
        ("off_pitch_below_target_with_melody", MELODY_A4, sine(329.63, 1.0), 0.0),
        ("silence", None, np.zeros(int(SAMPLE_RATE * 1.0), dtype=np.float32), None),
        ("noise", None, noise(1.0), None),
    ]


def run_python():
    results = {}
    for name, melody, samples, set_position in build_scenarios():
        grader = ag.RealtimeGrader(SAMPLE_RATE, melody=melody)
        if set_position is not None:
            grader.set_position(set_position)
        results[name] = feed_in_chunks(grader, samples)
    return results


def summarize(updates):
    if not updates:
        return {"n": 0}
    last = updates[-1]
    freqs = [u["frequency_hz"] for u in updates if u["frequency_hz"] is not None]
    return {
        "n": len(updates),
        "last_score": last["score"],
        "last_singing": last["singing"],
        "last_target_midi": last["target_midi"],
        "mean_freq": sum(freqs) / len(freqs) if freqs else None,
        "n_pitched": len(freqs),
    }


def main():
    here = os.path.dirname(os.path.abspath(__file__))
    wasm_json_path = os.path.join(here, "wasm_output.json")
    with open(wasm_json_path, "w") as out:
        subprocess.run(
            ["node", os.path.join(here, "cross_check_wasm.js")],
            stdout=out,
            check=True,
            cwd=here,
        )
    with open(wasm_json_path) as f:
        wasm_results = json.load(f)
    os.remove(wasm_json_path)

    python_results = run_python()

    print(
        f"{'scenario':36s} {'side':6s} {'n':>4s} {'last_score':>10s} {'singing':>8s} "
        f"{'target_midi':>11s} {'mean_freq':>10s} {'n_pitched':>9s}"
    )
    all_ok = True
    for name, _, _, _ in build_scenarios():
        py_summary = summarize(python_results[name])
        wasm_summary = summarize(wasm_results[name])
        for side, summary in (("python", py_summary), ("wasm", wasm_summary)):
            print(
                f"{name:36s} {side:6s} {summary.get('n', 0):4d} "
                f"{summary.get('last_score', ''):>10} {str(summary.get('last_singing', '')):>8} "
                f"{str(summary.get('last_target_midi', '')):>11} "
                f"{('%.2f' % summary['mean_freq']) if summary.get('mean_freq') else 'None':>10} "
                f"{summary.get('n_pitched', 0):>9}"
            )

        # Tolerant cross-check assertions (see PR description for why
        # exact equality isn't the bar): scores may differ by rounding
        # noise in the EMA/std chain, mean detected frequency must be
        # close since both implementations run the identical YIN
        # algorithm on identical samples.
        score_delta = abs(py_summary["last_score"] - wasm_summary["last_score"])
        if score_delta > 3:
            all_ok = False
            print(f"  !! score mismatch: python={py_summary['last_score']} wasm={wasm_summary['last_score']}")
        if py_summary["last_singing"] != wasm_summary["last_singing"]:
            all_ok = False
            print("  !! singing flag mismatch")
        if py_summary.get("mean_freq") and wasm_summary.get("mean_freq"):
            freq_delta = abs(py_summary["mean_freq"] - wasm_summary["mean_freq"])
            if freq_delta > 1.0:
                all_ok = False
                print(f"  !! mean frequency mismatch: python={py_summary['mean_freq']} wasm={wasm_summary['mean_freq']}")

    print()
    print("ALL SCENARIOS MATCH WITHIN TOLERANCE" if all_ok else "MISMATCHES FOUND")
    sys.exit(0 if all_ok else 1)


if __name__ == "__main__":
    main()
