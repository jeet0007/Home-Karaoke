"""Multi-source lyrics lookup: Lyrica sidecar first, direct LRCLIB second.

Lyrica (lyrica_client.py) stays primary because it already races several
sources (LRCLIB, YouTube, NetEase, Megalobiz, SimpMusic) and so has the
best hit rate. But it's an optional sidecar process — when it isn't
running, is unreachable, or simply finds nothing, the direct LRCLIB client
(lrclib_client.py) gives lyrics a second, independent chance instead of
failing outright.

Both sources return the same shape, so callers (app.py's /lyrics and
/select-song, library.py's processing worker) are source-agnostic:

    {"synced": [{"time_ms": int, "text": str}, ...],
     "plain": str,
     "source": str}
"""

from lyrics import lrclib_client
from lyrics import lyrica_client


def get_lyrics_full(artist, title, duration=None):
    """Return the first source's result, or None when every source misses.

    A Lyrica result with no synced lines still triggers the LRCLIB
    fallback — synced lyrics are what the karaoke player actually needs,
    so a plain-only Lyrica hit is only used when LRCLIB can't do better.
    """
    try:
        lyrica_result = lyrica_client.get_lyrics_full(artist, title, duration=duration)
    except Exception:
        lyrica_result = None

    if lyrica_result and lyrica_result.get("synced"):
        return lyrica_result

    try:
        lrclib_result = lrclib_client.get_lyrics_full(artist, title, duration=duration)
    except Exception:
        lrclib_result = None

    if lrclib_result and lrclib_result.get("synced"):
        return lrclib_result

    # Neither source has synced lyrics - a plain-text result is still
    # better than nothing (the player shows it unsynced).
    return lyrica_result or lrclib_result
