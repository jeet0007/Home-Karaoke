#!/usr/bin/env bash
# Launches the Lyrica lyrics sidecar (if present), then the main karaoke app.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LYRICA_DIR="$SCRIPT_DIR/sidecar/lyrica"
LYRICA_PORT="${LYRICA_PORT:-5001}"
APP_HOST="${APP_HOST:-127.0.0.1}"
APP_PORT="${APP_PORT:-3000}"

LYRICA_PID=""

# Sends SIGTERM to a whole process group (so any children the sidecar spawns
# die too), waits briefly, then escalates to SIGKILL if it's still alive.
kill_group() {
    local pid="$1"
    kill -0 "$pid" 2>/dev/null || return 0
    kill -TERM "-$pid" 2>/dev/null || kill -TERM "$pid" 2>/dev/null || true
    for _ in $(seq 1 30); do
        kill -0 "$pid" 2>/dev/null || return 0
        sleep 0.1
    done
    echo "Lyrica sidecar didn't exit after SIGTERM; sending SIGKILL..."
    kill -KILL "-$pid" 2>/dev/null || kill -KILL "$pid" 2>/dev/null || true
}

# Safe to call more than once: LYRICA_PID is cleared once the process is gone.
cleanup() {
    if [ -n "$LYRICA_PID" ]; then
        kill_group "$LYRICA_PID"
        LYRICA_PID=""
    fi
}
trap cleanup EXIT INT TERM

if [ -d "$LYRICA_DIR" ]; then
    echo "Installing Lyrica sidecar dependencies..."
    pip install -r "$LYRICA_DIR/requirements.txt" -q

    echo "Starting Lyrica sidecar on port $LYRICA_PORT..."
    # `set -m` gives the backgrounded job its own process group, and `exec`
    # collapses the subshell into `python run.py` so $! is the real PID
    # (not a subshell wrapper) and doubles as that group's PGID. Toggled
    # back off immediately so it doesn't affect the app.py foreground job.
    set -m
    (cd "$LYRICA_DIR" && exec env PORT="$LYRICA_PORT" python run.py) &
    LYRICA_PID=$!
    set +m

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
