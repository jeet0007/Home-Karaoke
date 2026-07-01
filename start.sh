#!/usr/bin/env bash
# Launches the Lyrica lyrics sidecar (if present), then the main karaoke app.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LYRICA_DIR="$SCRIPT_DIR/sidecar/lyrica"
LYRICA_PORT="${LYRICA_PORT:-5001}"
APP_HOST="${APP_HOST:-127.0.0.1}"
APP_PORT="${APP_PORT:-5000}"

LYRICA_PID=""
cleanup() {
    if [ -n "$LYRICA_PID" ]; then
        kill "$LYRICA_PID" 2>/dev/null || true
    fi
}
trap cleanup EXIT

if [ -d "$LYRICA_DIR" ]; then
    echo "Installing Lyrica sidecar dependencies..."
    pip install -r "$LYRICA_DIR/requirements.txt" -q

    echo "Starting Lyrica sidecar on port $LYRICA_PORT..."
    (cd "$LYRICA_DIR" && PORT="$LYRICA_PORT" python run.py) &
    LYRICA_PID=$!

    echo "Waiting for Lyrica to be ready..."
    ready=false
    for _ in $(seq 1 30); do
        if curl -sf "http://localhost:${LYRICA_PORT}/" >/dev/null 2>&1; then
            ready=true
            break
        fi
        sleep 1
    done
    if [ "$ready" = false ]; then
        echo "Warning: Lyrica did not become ready in time; continuing anyway."
    fi
else
    echo "Warning: sidecar/lyrica not found — /lyrics and /metadata will be unavailable."
    echo "Clone it with: git clone https://github.com/Wilooper/Lyrica sidecar/lyrica"
fi

export LYRICA_URL="http://localhost:${LYRICA_PORT}"
export APP_HOST
export APP_PORT
echo "Starting main app on ${APP_HOST}:${APP_PORT}..."
python app.py
