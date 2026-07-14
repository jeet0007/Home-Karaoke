"""Standalone entrypoint for the pipeline (worker) container: runs ONLY the
background processing queue - no Flask, no web routes, no HTTP server at
all. This is the resource-heavy half of the app (Demucs/Basic Pitch/tempo,
via requirements-ml.txt), split into its own container specifically so it
can be stopped independently on a resource-constrained NAS (`docker compose
stop pipeline`) without taking the player/search UI down - newly-picked
songs simply sit `pending` until this container is running again.

Reuses app.py's start_library_worker() verbatim (same fetch_lyrics/
find_video/resolve_audio_url wiring the single-process dev flow already
uses) rather than duplicating it - importing app.py here is safe and
side-effect-light: it only builds the Flask WSGI object and registers
routes, it never binds a port unless app.run() is called, which only
happens in app.py's own `if __name__ == "__main__"` block.

Shares core/library.py's SQLite library.db and core/artifacts.py's on-disk
store with the web container via the same LIBRARY_DB/KARAOKE_DATA_DIR paths
(bind-mounted/named-volume-shared in docker-compose.yml) - no network API
between the two containers, matching this project's one-thread/one-.db-file,
no-broker philosophy (see CLAUDE.md's Song library & processing queue
section).
"""

import signal

import app as app_module

worker = app_module.start_library_worker()


def _shutdown(signum, frame):
    # Let the current song finish its in-flight stage rather than killing it
    # mid-write: stop() only asks the run loop to exit after the song being
    # processed settles, then join() blocks until it actually has.
    worker.stop()


signal.signal(signal.SIGTERM, _shutdown)
signal.signal(signal.SIGINT, _shutdown)

worker.join()
