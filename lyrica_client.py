"""Thin client for the Lyrica lyrics/metadata sidecar API.

Talks to a locally-running Lyrica instance (github.com/Wilooper/Lyrica).
Its real endpoints are `GET /lyrics/?artist=&song=&timestamps=true` and
`GET /metadata/?artist=&song=` — both return HTTP 200 even when nothing
is found, with `{"status": "error", ...}` in the body, so callers must
check the `status` field rather than trust the HTTP status code alone.
"""

import os

import httpx
from dotenv import load_dotenv

load_dotenv()

LYRICA_URL = os.environ.get("LYRICA_URL", "http://localhost:5001").rstrip("/")
TIMEOUT = 10.0


class LyricaUnavailableError(Exception):
    """Raised when Lyrica couldn't be reached or returned something we can't
    interpret - a network error, timeout, non-200, or unparsable body.

    Deliberately distinct from a confirmed "no lyrics for this song" result
    (see check_lyrics_available): callers use this distinction to fail open
    on service trouble instead of treating an outage as "no lyrics exist".
    """


def check_lyrics_available(artist, title, timeout=TIMEOUT, fast=True):
    """Return True/False for whether Lyrica has lyrics for artist/title.

    Raises LyricaUnavailableError instead of returning False when we can't get
    a definitive answer (network error, timeout, bad response) - the caller
    must not conflate "couldn't check" with "confirmed absent".

    `fast` (default True) sets Lyrica's `fast=true`, which races only
    LRCLIB + YouTube in parallel instead of walking Lyrica's full 6-source
    sequential chain (LRCLIB, YouTube, NetEase, Megalobiz, Musixmatch,
    SimpMusic). This is meant for pre-selection availability checks over many
    unpicked search candidates, where speed matters more than exhausting
    every source - see lyrics_filter.py. Callers doing a real one-shot lookup
    on a single confirmed song should pass fast=False.
    """
    params = {
        "artist": artist,
        "song": title,
        "timestamps": "true",
        "fast": "true" if fast else "false",
    }
    try:
        response = httpx.get(f"{LYRICA_URL}/lyrics/", params=params, timeout=timeout)
    except httpx.HTTPError as exc:
        raise LyricaUnavailableError(f"Lyrica request failed: {exc}") from exc

    if response.status_code != 200:
        raise LyricaUnavailableError(f"Lyrica returned HTTP {response.status_code}")

    try:
        payload = response.json()
    except ValueError as exc:
        raise LyricaUnavailableError("Lyrica returned an unparsable response") from exc

    if payload.get("status") != "success":
        # Lyrica's documented contract: HTTP 200 + {"status": "error"} means it
        # looked and found nothing, not that the request failed.
        return False

    data = payload.get("data") or {}
    return bool(data.get("timed_lyrics") or data.get("plain_lyrics") or data.get("lyrics"))


def _as_time_ms(value):
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _fetch_lyrics_data(artist, title):
    """Hit Lyrica's /lyrics/ endpoint and return its `data` payload, or None."""
    params = {"artist": artist, "song": title, "timestamps": "true"}
    try:
        response = httpx.get(f"{LYRICA_URL}/lyrics/", params=params, timeout=TIMEOUT)
    except httpx.HTTPError:
        return None

    if response.status_code != 200:
        return None

    try:
        payload = response.json()
    except ValueError:
        return None

    if payload.get("status") != "success":
        return None

    return payload.get("data") or {}


def get_lyrics(artist, title, duration=None):
    """Return synced lyrics as [{"time_ms": int, "text": str}], or [] if unavailable.

    Prefers timestamped lyrics; returns [] if Lyrica only has plain lyrics
    or nothing at all (use get_lyrics_full() for the plain-text fallback).
    """
    data = _fetch_lyrics_data(artist, title)
    if not data:
        return []

    timed = data.get("timed_lyrics") or []
    return [
        {"time_ms": _as_time_ms(line.get("start_time")), "text": line.get("text", "")}
        for line in timed
    ]


def get_lyrics_full(artist, title, duration=None):
    """Return {"synced": [...], "plain": str, "source": str}, or None if not found."""
    data = _fetch_lyrics_data(artist, title)
    if not data:
        return None

    timed = data.get("timed_lyrics") or []
    synced = [
        {"time_ms": _as_time_ms(line.get("start_time")), "text": line.get("text", "")}
        for line in timed
    ]

    # Some sources (e.g. LRCLIB) leave raw "[mm:ss.xx]" tags in `lyrics` once
    # timestamps are requested, so prefer reconstructing plain text from the
    # already-clean per-line text in `timed_lyrics` when it's available.
    if synced:
        plain = "\n".join(line["text"] for line in synced)
    else:
        plain = data.get("plain_lyrics") or data.get("lyrics") or ""

    if not synced and not plain:
        return None

    return {"synced": synced, "plain": plain, "source": data.get("source", "")}


def get_metadata(artist, title):
    """Return a metadata dict (cover_art, genre, duration_s, release_date, ...), or {}."""
    params = {"artist": artist, "song": title}
    try:
        response = httpx.get(f"{LYRICA_URL}/metadata/", params=params, timeout=TIMEOUT)
    except httpx.HTTPError:
        return {}

    if response.status_code != 200:
        return {}

    try:
        payload = response.json()
    except ValueError:
        return {}

    if payload.get("status") != "success":
        return {}

    meta = payload.get("metadata") or {}
    duration = meta.get("duration") or {}

    return {
        **meta,
        "cover_art": meta.get("album_art") or meta.get("wiki_thumbnail") or "",
        "genre": ", ".join(meta.get("tags") or []),
        "duration_s": duration.get("seconds"),
        "release_date": meta.get("release_date", ""),
    }
