"""Direct LRCLIB (lrclib.net) client — the second lyrics source.

The primary lyrics path is the Lyrica sidecar (lyrica_client.py), which
races several sources including LRCLIB. But Lyrica is an optional extra
process (start.sh skips it when sidecar/lyrica isn't cloned) and can be
down/slow independently of this app. LRCLIB's own public API is free,
keyless, and returns synced LRC directly — so it makes a natural direct
fallback that keeps lyrics working with no sidecar at all.

See lyrics_sources.py for the Lyrica-first / LRCLIB-fallback ordering.
"""

import re

import httpx

LRCLIB_URL = "https://lrclib.net/api/get"
TIMEOUT = 10.0
SOURCE_NAME = "lrclib-direct"

# "[mm:ss.xx] text" (also tolerates hour and 3-digit fraction variants).
_LRC_LINE = re.compile(r"\[(\d+):(\d{1,2})(?:[.:](\d{1,3}))?\]\s?(.*)")


def parse_lrc(lrc_text):
    """Parse LRC text into [{"time_ms": int, "text": str}], sorted by time.
    Lines without a timestamp tag (metadata like [ar:...]) are skipped; a
    line may carry multiple timestamps (repeated chorus), producing one
    entry per timestamp."""
    entries = []
    for line in (lrc_text or "").splitlines():
        rest = line
        times = []
        while True:
            match = _LRC_LINE.match(rest)
            if not match:
                break
            minutes, seconds, fraction, rest = match.groups()
            fraction = (fraction or "0").ljust(3, "0")[:3]
            times.append((int(minutes) * 60 + int(seconds)) * 1000 + int(fraction))
        text = rest.strip()
        if not text:
            continue
        for time_ms in times:
            entries.append({"time_ms": time_ms, "text": text})
    entries.sort(key=lambda e: e["time_ms"])
    return entries


def get_lyrics_full(artist, title, duration=None, timeout=TIMEOUT):
    """Return {"synced": [...], "plain": str, "source": "lrclib-direct"} or
    None when LRCLIB has nothing (or can't be reached — this is already the
    fallback source, so there's nothing further to fail over to)."""
    params = {"artist_name": artist, "track_name": title}
    if duration:
        params["duration"] = int(duration)

    try:
        response = httpx.get(LRCLIB_URL, params=params, timeout=timeout)
    except httpx.HTTPError:
        return None

    if response.status_code != 200:
        return None

    try:
        payload = response.json()
    except ValueError:
        return None

    synced = parse_lrc(payload.get("syncedLyrics") or "")
    if synced:
        plain = "\n".join(line["text"] for line in synced)
    else:
        plain = (payload.get("plainLyrics") or "").strip()

    if not synced and not plain:
        return None

    return {"synced": synced, "plain": plain, "source": SOURCE_NAME}
