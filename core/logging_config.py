"""Structured JSON logging for the pipeline + library worker.

Deliberately explicit and idempotent: `configure()` is the ONLY place that
creates the log directory and attaches a handler. It must never run at
import time - `core/pipeline.py` only does
`logging.getLogger("karaoke.pipeline")` at module level, which is a no-op
until a handler exists somewhere in the "karaoke" logger hierarchy. That
matters because importing core.pipeline (as every pipeline test does) must
stay a zero-side-effect operation - no logs/ directory appearing as a side
effect of running the test suite.

app.py's start_library_worker() is the only production call site.
"""

import json
import logging
import logging.handlers
import os

# Repo root (this module lives in core/), so logs default to a `logs/` dir
# at the project root, mirroring LIBRARY_DB / KARAOKE_DATA_DIR. Override
# with KARAOKE_LOG_DIR.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_LOG_DIR = os.path.join(_REPO_ROOT, "logs")

LOGGER_NAME = "karaoke"
_LOG_FILENAME = "pipeline.log"
_MAX_BYTES = 5 * 1024 * 1024
_BACKUP_COUNT = 5

# Extra fields _stage() (core/pipeline.py) attaches via `extra=`; anything
# not present on a given record is simply omitted from its JSON line.
_RECORD_EXTRA_FIELDS = ("song_id", "run_id", "stage", "status", "duration_ms")

_configured = False

# A NullHandler on the package logger at import time is the stdlib-idiomatic
# "library has no opinion on logging until configured" pattern - it creates
# no file/dir and silences "no handlers found" warnings, but is invisible to
# `configure()`'s own idempotency check (that only looks at _configured).
logging.getLogger(LOGGER_NAME).addHandler(logging.NullHandler())


class _JsonFormatter(logging.Formatter):
    def format(self, record):
        payload = {
            "time": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "level": record.levelname,
            "message": record.getMessage(),
        }
        for field in _RECORD_EXTRA_FIELDS:
            value = getattr(record, field, None)
            if value is not None:
                payload[field] = value
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(payload)


def configure(log_dir=None):
    """Attach a rotating JSON file handler to the "karaoke" logger namespace
    (parent of "karaoke.pipeline" etc.), so every module's logger.getLogger
    call under it is covered without each needing its own handler.

    Idempotent: a second call (e.g. the Werkzeug reloader re-importing app.py)
    is a no-op rather than attaching a second handler and duplicating every
    log line.
    """
    global _configured
    if _configured:
        return

    log_dir = log_dir or os.environ.get("KARAOKE_LOG_DIR", DEFAULT_LOG_DIR)
    os.makedirs(log_dir, exist_ok=True)

    handler = logging.handlers.RotatingFileHandler(
        os.path.join(log_dir, _LOG_FILENAME), maxBytes=_MAX_BYTES, backupCount=_BACKUP_COUNT
    )
    handler.setFormatter(_JsonFormatter())

    logger = logging.getLogger(LOGGER_NAME)
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False

    _configured = True
