#!/usr/bin/env python3
"""Detects this machine's OS/CPU/Python version and installs the matching
`requirements-ml.txt` + platform-specific extras, instead of everyone running
the same flat `pip install -r requirements-ml.txt` and then chasing whatever
backend-mismatch warnings basic-pitch prints for their particular device.

Run it from inside the venv you intend to run app.py from (on Apple Silicon,
that must already be a Python 3.10 venv - see requirements-ml.txt/README.md
for why; this script checks and refuses to guess around that constraint).

Usage:
    python scripts/bootstrap_ml.py [--dry-run]
"""

import argparse
import platform
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
BASE_REQUIREMENTS = REPO_ROOT / "requirements-ml.txt"


def detect():
    return {
        "system": platform.system(),  # "Darwin" / "Linux" / "Windows"
        "machine": platform.machine(),  # "arm64" / "x86_64" / ...
        "python": sys.version_info[:2],
    }


def plan(info):
    """Returns (extra_packages, notes) for this machine. Raises SystemExit
    if the machine can't run the ML add-on at all under the current
    interpreter (the Apple Silicon / Python 3.10 constraint)."""
    is_apple_silicon = info["system"] == "Darwin" and info["machine"] == "arm64"

    if is_apple_silicon and info["python"] != (3, 10):
        major, minor = info["python"]
        raise SystemExit(
            f"Apple Silicon only supports Basic Pitch under Python 3.10 (this interpreter is "
            f"{major}.{minor}). Create that venv first, then re-run this script from inside it:\n\n"
            "  pyenv install 3.10.20 && ~/.pyenv/versions/3.10.20/bin/python3.10 -m venv .venv-ml\n"
            "  source .venv-ml/bin/activate\n"
            "  python scripts/bootstrap_ml.py"
        )

    extras = []
    notes = []

    if is_apple_silicon:
        # basic-pitch's inference on Apple Silicon goes through coremltools.
        # coremltools' *scikit-learn model conversion* API (unrelated to
        # basic-pitch's own neural-net model) only supports scikit-learn
        # <=1.5.1 - a newer one is harmless but prints a loud warning on
        # every run. Pin it down so the warning doesn't show up at all.
        extras.append("scikit-learn<=1.5.1")
        notes.append(
            "Expected benign warning even after this: coremltools' \"Minimum required torch "
            "version for importing coremltools.optimize.torch is 2.1.0\" - Demucs pins "
            "torchaudio<2.1 (see requirements-ml.txt), which pulls a torch <2.1 build. That's a "
            "real, currently-irreconcilable version conflict between Demucs and coremltools' "
            "optional optimizer, not a bug to chase - it only disables an optimization path "
            "basic-pitch doesn't need."
        )
    else:
        # No CoreML backend off-Mac, so make sure basic-pitch has a definite
        # working inference backend rather than relying on whatever it
        # happened to pull in transitively. TFLite runtime is the lightweight
        # option (see requirements-ml.txt) - not full TensorFlow.
        extras.append("tflite-runtime")

    return extras, notes


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="print the plan without installing anything")
    args = parser.parse_args()

    info = detect()
    print(f"Detected: {info['system']} {info['machine']}, Python {info['python'][0]}.{info['python'][1]}")

    extras, notes = plan(info)

    base_cmd = [sys.executable, "-m", "pip", "install", "-r", str(BASE_REQUIREMENTS)]
    extras_cmd = [sys.executable, "-m", "pip", "install", *extras] if extras else None

    print("\nWill run:")
    print("  " + " ".join(base_cmd))
    if extras_cmd:
        print("  " + " ".join(extras_cmd))

    if args.dry_run:
        return

    subprocess.run(base_cmd, check=True)
    if extras_cmd:
        subprocess.run(extras_cmd, check=True)

    if notes:
        print("\n" + "\n\n".join(notes))


if __name__ == "__main__":
    main()
