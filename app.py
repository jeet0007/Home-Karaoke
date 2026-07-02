"""Minimal Flask GUI for karaoke-exclusive YouTube search."""

import json
import os
import re
import socket
import subprocess
import sys
import threading
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor

import httpx
import numpy as np
from flask import Flask, Response, copy_current_request_context, jsonify, redirect, render_template, request, stream_with_context, url_for
from flask_sock import Sock
from simple_websocket import ConnectionClosed

import lyrica_client
import waveform
from audio_grading import RealtimeGrader
from fallback_search import parse_title_identity_candidates
from lyrics_filter import filter_candidates_by_lyrics
from search import KaraokeSearch
from song_search import SongSearch, SongSearchError
from song_selection import pick_best_candidate

LYRICS_CHECK_CAP = 15
UNIFIED_SEARCH_DEFAULT_LIMIT = 12
SUGGESTIONS_DEFAULT_LIMIT = 8
SUGGESTIONS_MAX_LIMIT = 10
SUGGESTIONS_MIN_QUERY_LENGTH = 2
GRADE_MIN_SAMPLE_RATE = 8000
GRADE_MAX_SAMPLE_RATE = 192000

app = Flask(__name__)
# flask-sock upgrades a request to a raw WebSocket in-place on Werkzeug's dev
# server (via simple-websocket/wsproto) - no separate ASGI server or
# gunicorn/eventlet swap needed, so this coexists with app.run(threaded=True)
# below and with /stream-proxy's Range-request streaming untouched.
sock = Sock(app)
karaoke_search = KaraokeSearch()
song_search = SongSearch()
BINARY_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "binaries", "yt-dlp")

VIDEO_ID_PATTERN = re.compile(
    r"(?:v=|/videos/|embed/|youtu\.be/|/v/|/shorts/|/live/)([A-Za-z0-9_-]{11})"
)
VIDEO_ID_ONLY_PATTERN = re.compile(r"^[A-Za-z0-9_-]{11}$")

# googlevideo.com URLs are signed for the IP that resolved them and expire a few
# hours later; fetching them straight from the browser also gets blocked by CORS
# since the CDN sends no Access-Control-Allow-Origin. We keep resolved URLs
# server-side and proxy the bytes through this process instead, per video_id.
_STREAM_CACHE = {}
_STREAM_CACHE_LOCK = threading.Lock()
_STREAM_EXPIRY_BUFFER_S = 60
_PROXY_CHUNK_SIZE = 256 * 1024
_PASSTHROUGH_RESPONSE_HEADERS = ("Content-Type", "Content-Length", "Content-Range", "Accept-Ranges")

# Waveform peaks are cheap (a few hundred floats) and, unlike the resolved
# CDN URL in _STREAM_CACHE, never expire - so this is a plain in-memory
# video_id -> {"peaks": [...], "duration_s": float} cache with no TTL. It
# does not survive a restart; that's an acceptable tradeoff for this
# visualization-only feature (see waveform.py).
_WAVEFORM_CACHE = {}
_WAVEFORM_CACHE_LOCK = threading.Lock()
_WAVEFORM_RESOLVE_TIMEOUT_S = 20

_http_client = httpx.Client(follow_redirects=True, timeout=20.0)


def _resolve_host_port(env=None):
    env = os.environ if env is None else env
    host = env.get("APP_HOST", "127.0.0.1")
    port_text = env.get("APP_PORT", "5000")

    try:
        port = int(port_text)
    except ValueError as exc:
        raise ValueError(f"Invalid APP_PORT={port_text!r}; set APP_PORT to a number from 1 to 65535") from exc

    if port < 1 or port > 65535:
        raise ValueError(f"Invalid APP_PORT={port}; set APP_PORT to a number from 1 to 65535")

    return host, port


def _assert_port_available(host, port):
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    with socket.socket(family, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((host, port))
        except OSError as exc:
            raise RuntimeError(
                f"Port {port} is already in use on {host} — set APP_PORT=<other port> and retry, or free the port"
            ) from exc


def _resolve_stream_urls(url, format_selector, timeout):
    cmd = [
        BINARY_PATH,
        "--get-url",
        "-f",
        format_selector,
        url,
    ]

    proc = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=timeout,
    )

    if proc.returncode != 0:
        stderr = proc.stderr.strip() or "unknown yt-dlp error"
        raise RuntimeError(f"yt-dlp failed: {stderr}")

    return [line.strip() for line in proc.stdout.splitlines() if line.strip()]


def _parse_expire_param(stream_url):
    query = urllib.parse.urlparse(stream_url).query
    try:
        return int(urllib.parse.parse_qs(query)["expire"][0])
    except (KeyError, ValueError, IndexError):
        return None


def _resolve_playable_stream_url(video_id, timeout):
    """Resolve a browser-playable progressive stream URL, falling back from
    separate best video/audio tracks to a single combined format."""
    youtube_url = f"https://www.youtube.com/watch?v={video_id}"
    started_at = time.monotonic()

    stream_urls = _resolve_stream_urls(
        youtube_url,
        "bestvideo[ext=mp4]+bestaudio/best[ext=mp4]/best",
        timeout,
    )
    if not stream_urls:
        raise RuntimeError("yt-dlp did not return a stream URL")

    if len(stream_urls) == 1:
        return stream_urls[0], None

    remaining_timeout = max(1, timeout - int(time.monotonic() - started_at))
    progressive_urls = _resolve_stream_urls(youtube_url, "best[ext=mp4]/best", remaining_timeout)
    if not progressive_urls:
        raise RuntimeError("yt-dlp did not return a browser-playable stream URL")

    warning = (
        "yt-dlp returned separate best video/audio URLs; using a progressive browser-playable stream."
    )
    return progressive_urls[0], warning


def _get_upstream_stream_url(video_id, timeout=20, force_refresh=False):
    """Return a cached (or freshly resolved) upstream CDN URL for video_id.

    Cached per video_id and re-resolved once it's within _STREAM_EXPIRY_BUFFER_S
    of the signed URL's expiry, so scrubbing/buffering during a session doesn't
    re-invoke yt-dlp on every byte-range request.
    """
    if force_refresh:
        with _STREAM_CACHE_LOCK:
            _STREAM_CACHE.pop(video_id, None)
    else:
        with _STREAM_CACHE_LOCK:
            cached = _STREAM_CACHE.get(video_id)
        if cached and (cached["expire"] is None or cached["expire"] - time.time() > _STREAM_EXPIRY_BUFFER_S):
            return cached["url"], cached["warning"]

    stream_url, warning = _resolve_playable_stream_url(video_id, timeout)
    with _STREAM_CACHE_LOCK:
        _STREAM_CACHE[video_id] = {
            "url": stream_url,
            "expire": _parse_expire_param(stream_url),
            "warning": warning,
        }
    return stream_url, warning


def _compute_waveform(video_id, timeout=_WAVEFORM_RESOLVE_TIMEOUT_S):
    """Resolve an audio-only stream URL for video_id (deliberately not the
    progressive video+audio URL /stream-proxy plays - see waveform.py) and
    decode it into a peaks envelope."""
    youtube_url = f"https://www.youtube.com/watch?v={video_id}"
    audio_urls = _resolve_stream_urls(youtube_url, "bestaudio/best", timeout)
    if not audio_urls:
        raise RuntimeError("yt-dlp did not return an audio stream URL")

    pcm = waveform.decode_pcm_from_url(audio_urls[0])
    return {
        "peaks": waveform.compute_peaks(pcm),
        "duration_s": waveform.pcm_duration_s(pcm),
    }


def _get_or_compute_waveform(video_id, timeout=_WAVEFORM_RESOLVE_TIMEOUT_S):
    with _WAVEFORM_CACHE_LOCK:
        cached = _WAVEFORM_CACHE.get(video_id)
    if cached is not None:
        return cached

    result = _compute_waveform(video_id, timeout=timeout)
    with _WAVEFORM_CACHE_LOCK:
        _WAVEFORM_CACHE[video_id] = result
    return result


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/songs")
def songs():
    """Retired as its own page in favor of the unified `/` search - redirect
    so old bookmarks/links still land somewhere useful."""
    return redirect(url_for("index"), code=301)


def _run_karaoke_search(query, limit):
    """Run KaraokeSearch and translate its exceptions into (json, status) error
    tuples, shared by /search and /video-search which differ only in how they
    build `query` (raw free text vs. a clean artist/title identity)."""
    try:
        return karaoke_search.search(query, max_results=limit), None
    except FileNotFoundError as exc:
        return None, (jsonify({"error": str(exc)}), 500)
    except TimeoutError as exc:
        return None, (jsonify({"error": str(exc)}), 504)
    except RuntimeError as exc:
        return None, (jsonify({"error": str(exc)}), 502)


@app.route("/search")
def search():
    query = request.args.get("q", "").strip()
    if not query:
        return jsonify({"error": "Missing required query parameter 'q'"}), 400

    limit = request.args.get("limit", 10, type=int) or 10
    limit = max(1, min(limit, LYRICS_CHECK_CAP))

    results, error = _run_karaoke_search(query, limit)
    if error:
        return error

    return jsonify({"query": query, "count": len(results), "results": results})


@app.route("/song-search")
def song_search_route():
    query = request.args.get("q", "").strip()
    if not query:
        return jsonify({"error": "Missing required query parameter 'q'"}), 400

    limit = request.args.get("limit", 10, type=int) or 10
    limit = max(1, min(limit, LYRICS_CHECK_CAP))

    try:
        results = song_search.search(query, limit=limit)
    except SongSearchError as exc:
        return jsonify({"error": str(exc)}), exc.status_code

    return jsonify({"query": query, "count": len(results), "results": results})


@app.route("/song-suggestions")
def song_suggestions():
    """Fast typeahead suggestions as the user types, deliberately with NO
    lyrics-availability filtering.

    /unified-search is slow because it lyrics-checks every ytmusicapi
    candidate against Lyrica before returning anything. This endpoint exists
    so the frontend can offer the user an exact song to pick (routed straight
    into /select-song) before that expensive fan-out ever runs - it's a thin,
    unfiltered wrapper around song_search.search().
    """
    query = request.args.get("q", "").strip()
    if len(query) < SUGGESTIONS_MIN_QUERY_LENGTH:
        return jsonify({"query": query, "count": 0, "results": []})

    limit = request.args.get("limit", SUGGESTIONS_DEFAULT_LIMIT, type=int) or SUGGESTIONS_DEFAULT_LIMIT
    limit = max(1, min(limit, SUGGESTIONS_MAX_LIMIT))

    try:
        results = song_search.search(query, limit=limit)
    except SongSearchError as exc:
        return jsonify({"error": str(exc)}), exc.status_code

    return jsonify({"query": query, "count": len(results), "results": results})


@app.route("/video-search")
def video_search():
    """Find a karaoke video for a known-good song identity (artist/title),
    reusing the existing yt-dlp karaoke ranking - the song identity is fixed,
    but the video choice stays flexible/ranked, same as /search."""
    artist = request.args.get("artist", "").strip()
    title = request.args.get("title", "").strip()
    if not artist or not title:
        return jsonify({"error": "Missing required query parameters 'artist' and 'title'"}), 400

    limit = request.args.get("limit", 10, type=int) or 10
    limit = max(1, min(limit, LYRICS_CHECK_CAP))

    query = f"{title} {artist}"
    results, error = _run_karaoke_search(query, limit)
    if error:
        return error

    return jsonify({"query": query, "artist": artist, "title": title, "count": len(results), "results": results})


def _song_identity(song):
    return (song.get("artist", ""), song.get("title", ""))


def _video_identity_candidates(video):
    return parse_title_identity_candidates(video.get("title", ""))


def _apply_resolved_identity(video):
    """Attach a best-guess artist/clean_title to a fallback video result, for
    the player/lyrics-fetch step downstream. Prefers the identity confirmed
    by the lyrics check; falls back to the raw uploader/title (today's
    behavior) when the check errored (fail-open) rather than confirmed one
    of the parsed guesses."""
    resolved = video.pop("_resolved_identity", None)
    artist, title = resolved if resolved else (video.get("uploader") or "", video.get("title") or "")
    return {**video, "artist": artist, "clean_title": title}


def _video_to_song_result(video):
    """Reshape a lyrics-confirmed fallback video candidate into the same
    song-result shape the primary ytmusicapi path returns.

    The frontend must never see or pick a video (video_id, url, score,
    uploader, view_count are all dropped here on purpose) - even when a
    result only exists because we had to fall back to raw video search, it
    is presented to the user as a song, with its artist/title resolved by
    the lyrics check (see _apply_resolved_identity)."""
    return {
        "artist": video.get("artist", ""),
        "title": video.get("clean_title", ""),
        "album": None,
        "duration_seconds": video.get("duration_seconds"),
        "cover_art": video.get("thumbnail") or "",
    }


@app.route("/unified-search")
def unified_search():
    """Single search entry point behind the unified `/` page.

    Primary path: resolve the query to a clean song identity via ytmusicapi
    (song_search.py), then keep only identities Lyrica confirms have lyrics.
    If that yields nothing usable - ytmusicapi found nothing, errored, or
    none of what it found has lyrics - fall back to ranking raw karaoke
    videos directly (search.py), lyrics-filtered the same way but with
    artist/title guessed from the video title (fallback_search.py) since
    those videos carry no structured metadata.

    Either way, every result returned to the caller is a *song* - artist,
    title, album, duration, cover art - never a video candidate. Picking a
    karaoke video happens entirely server-side in /select-song once the user
    picks one of these songs.
    """
    query = request.args.get("q", "").strip()
    if not query:
        return jsonify({"error": "Missing required query parameter 'q'"}), 400

    limit = request.args.get("limit", UNIFIED_SEARCH_DEFAULT_LIMIT, type=int) or UNIFIED_SEARCH_DEFAULT_LIMIT
    limit = max(1, min(limit, LYRICS_CHECK_CAP))

    song_error = None
    song_results = []
    try:
        song_results = song_search.search(query, limit=limit)
    except SongSearchError as exc:
        song_error = exc

    if song_results:
        kept, degraded = filter_candidates_by_lyrics(song_results, _song_identity, cap=LYRICS_CHECK_CAP)
        if kept:
            response = {"query": query, "source": "identity", "count": len(kept[:limit]), "results": kept[:limit]}
            if degraded:
                response["warning"] = "Lyrics availability check is temporarily unavailable; results are unfiltered."
            return jsonify(response)

    # Nothing usable from the song-identity path - fall back to direct
    # karaoke video search, same as the original /search route, but still
    # surface the result as a song (see _video_to_song_result).
    video_results, error = _run_karaoke_search(query, limit)
    if error:
        if song_error:
            return jsonify({"error": f"song search failed ({song_error}); fallback video search also failed"}), 502
        return error

    kept, degraded = filter_candidates_by_lyrics(video_results, _video_identity_candidates, cap=LYRICS_CHECK_CAP)
    kept = [_apply_resolved_identity(v) for v in kept][:limit]
    song_shaped = [_video_to_song_result(v) for v in kept]

    if song_error:
        warning = f"Song search unavailable ({song_error}); showing best-guess song matches instead."
    elif not song_results:
        warning = "No song match found for this query; showing best-guess song matches instead."
    else:
        warning = "Found song matches, but none have lyrics available; showing best-guess song matches instead."
    if degraded:
        warning += " Lyrics availability check is also temporarily unavailable; these results are unfiltered."

    return jsonify(
        {"query": query, "source": "fallback", "count": len(song_shaped), "results": song_shaped, "warning": warning}
    )


@app.route("/preview")
def preview():
    url = request.args.get("url", "").strip()
    if not url:
        return jsonify({"error": "Missing required query parameter 'url'"}), 400

    match = VIDEO_ID_PATTERN.search(url)
    if not match:
        return jsonify({"error": "Could not extract a video id from that URL"}), 400

    return jsonify({"embed_url": f"https://www.youtube.com/embed/{match.group(1)}"})


def _fetch_metadata_safe(artist, title):
    """lyrica_client.get_metadata already fails open (returns {}) on network/
    parse errors; this only guards against an unexpected exception escaping
    the worker thread and taking down the other two concurrent lookups."""
    try:
        return lyrica_client.get_metadata(artist, title) or {}
    except Exception:
        return {}


def _fetch_lyrics_safe(artist, title, duration_hint):
    try:
        return lyrica_client.get_lyrics_full(artist, title, duration=duration_hint) or {}
    except Exception:
        return {}


def _prewarm_stream_cache(video_id):
    """Best-effort background warm of _STREAM_CACHE for the video /select-song
    just picked, so the frontend's near-certain follow-up /stream-url call
    often finds a warm cache instead of paying yt-dlp's full cold resolution
    cost. Fire-and-forget: /select-song does not wait on this thread, and any
    failure here is silently absorbed - /stream-url falls back to resolving
    cold itself if this hasn't finished (or failed) in time."""
    try:
        _get_upstream_stream_url(video_id)
    except Exception:
        pass


def _prewarm_waveform(video_id):
    """Best-effort background compute of _WAVEFORM_CACHE for the video
    /select-song just picked, so the frontend's likely follow-up /waveform
    call often finds a warm cache instead of paying decode cost inline.
    Fire-and-forget, same pattern as _prewarm_stream_cache: on any failure
    (including a missing ffmpeg binary) the frontend's own /waveform request
    will surface the error, or the waveform simply never appears - either
    way playback and lyrics are unaffected."""
    if not waveform.ffmpeg_available():
        return
    try:
        _get_or_compute_waveform(video_id)
    except Exception:
        pass


@app.route("/select-song")
def select_song():
    """Given a song identity the user picked from /unified-search (artist,
    title, and optionally a duration hint), resolve everything the player
    page needs in one response: synced lyrics + cover art (Lyrica metadata),
    and the single best-matching karaoke video - auto-picked server-side via
    the existing karaoke video ranking (search.py) plus a duration-proximity
    bonus/penalty (song_selection.py). The frontend never sees the runner-up
    candidates, only the winner's video_id (or null if nothing was found).

    Metadata, lyrics, and the video search are independent lookups - none
    consumes another's output (target_duration below only feeds the cheap
    local pick_best_candidate() call) - so they run concurrently rather than
    back-to-back, collapsing total latency from roughly their sum to roughly
    the slowest of the three.
    """
    artist = request.args.get("artist", "").strip()
    title = request.args.get("title", "").strip()
    duration_hint = request.args.get("duration", type=int)
    if not artist or not title:
        return jsonify({"error": "Missing required query parameters 'artist' and 'title'"}), 400

    query = f"{title} {artist}"

    # _run_karaoke_search's error paths call jsonify(), which needs an active
    # Flask app/request context - not available by default inside a
    # ThreadPoolExecutor worker thread, so it's wrapped to carry this
    # request's context over. The other two calls touch no Flask context.
    with ThreadPoolExecutor(max_workers=3) as executor:
        metadata_future = executor.submit(_fetch_metadata_safe, artist, title)
        lyrics_future = executor.submit(_fetch_lyrics_safe, artist, title, duration_hint)
        search_future = executor.submit(copy_current_request_context(_run_karaoke_search), query, LYRICS_CHECK_CAP)

        metadata = metadata_future.result()
        lyrics_result = lyrics_future.result()
        candidates, error = search_future.result()

    if error:
        return error

    # Lyrica's metadata is the authoritative source for the song's real
    # duration (used to score candidate videos below); the caller-supplied
    # duration is only a fallback when Lyrica has no metadata for this song.
    target_duration = metadata.get("duration_s")
    if target_duration is None:
        target_duration = duration_hint

    best = pick_best_candidate(candidates, target_duration)

    response = {
        "artist": artist,
        "title": title,
        "duration_seconds": target_duration,
        "cover_art": metadata.get("cover_art", ""),
        "lyrics": {
            "synced": lyrics_result.get("synced", []),
            "plain": lyrics_result.get("plain", ""),
            "source": lyrics_result.get("source", ""),
        },
        "video_id": None,
    }

    if best is None:
        response["message"] = "No backing track found for this song."
        return jsonify(response)

    response["video_id"] = best["video_id"]
    threading.Thread(target=_prewarm_stream_cache, args=(best["video_id"],), daemon=True).start()
    threading.Thread(target=_prewarm_waveform, args=(best["video_id"],), daemon=True).start()
    return jsonify(response)


@app.route("/player")
def player():
    title = request.args.get("title", "").strip() or "Untitled"
    artist = request.args.get("artist", "").strip() or "Unknown artist"
    duration = request.args.get("duration", "").strip()

    return render_template(
        "player.html",
        title=title,
        artist=artist,
        duration=duration,
    )


@app.route("/stream-url")
def stream_url():
    video_id = request.args.get("video_id", "").strip()
    if not video_id:
        return jsonify({"error": "Missing required query parameter 'video_id'"}), 400

    if not VIDEO_ID_ONLY_PATTERN.match(video_id):
        return jsonify({"error": "Invalid YouTube video id"}), 400

    if not os.path.isfile(BINARY_PATH):
        return jsonify({"error": f"yt-dlp binary not found at {BINARY_PATH}"}), 500

    try:
        _, warning = _get_upstream_stream_url(video_id)
    except subprocess.TimeoutExpired:
        return jsonify({"error": "Timed out while resolving the video stream URL"}), 504
    except OSError as exc:
        return jsonify({"error": f"failed to execute yt-dlp: {exc}"}), 500
    except RuntimeError as exc:
        return jsonify({"error": str(exc)}), 502

    # Point the player at our own proxy, not the raw googlevideo CDN URL: that
    # URL is IP-signed to this server, has no CORS headers, and expires - all
    # of which break direct browser playback. See /stream-proxy.
    response = {"stream_url": f"/stream-proxy/{video_id}"}
    if warning:
        response["warning"] = warning
    return jsonify(response)


@app.route("/stream-proxy/<video_id>")
def stream_proxy(video_id):
    video_id = video_id.strip()
    if not VIDEO_ID_ONLY_PATTERN.match(video_id):
        return jsonify({"error": "Invalid YouTube video id"}), 400

    if not os.path.isfile(BINARY_PATH):
        return jsonify({"error": f"yt-dlp binary not found at {BINARY_PATH}"}), 500

    range_header = request.headers.get("Range")
    req_headers = {"Range": range_header} if range_header else {}

    def send_upstream(force_refresh=False):
        upstream_url, _ = _get_upstream_stream_url(video_id, force_refresh=force_refresh)
        upstream_request = _http_client.build_request("GET", upstream_url, headers=req_headers)
        return _http_client.send(upstream_request, stream=True)

    try:
        upstream = send_upstream()
        if upstream.status_code in (403, 404):
            upstream.close()
            upstream = send_upstream(force_refresh=True)
    except subprocess.TimeoutExpired:
        return jsonify({"error": "Timed out while resolving the video stream URL"}), 504
    except OSError as exc:
        return jsonify({"error": f"failed to execute yt-dlp: {exc}"}), 500
    except RuntimeError as exc:
        return jsonify({"error": str(exc)}), 502
    except httpx.HTTPError as exc:
        return jsonify({"error": f"failed to reach the video CDN: {exc}"}), 502

    if upstream.status_code >= 400:
        status = upstream.status_code
        upstream.close()
        return jsonify({"error": f"upstream CDN returned HTTP {status}"}), 502

    def generate():
        try:
            for chunk in upstream.iter_bytes(_PROXY_CHUNK_SIZE):
                yield chunk
        finally:
            upstream.close()

    headers = {name: upstream.headers[name] for name in _PASSTHROUGH_RESPONSE_HEADERS if name in upstream.headers}
    headers.setdefault("Accept-Ranges", "bytes")
    headers["Access-Control-Allow-Origin"] = "*"
    headers["Cache-Control"] = "no-store"

    return Response(stream_with_context(generate()), status=upstream.status_code, headers=headers)


@app.route("/waveform/<video_id>")
def waveform_route(video_id):
    """Coarse peak envelope for the player's waveform visualization - see
    waveform.py for why this decodes its own audio-only stream rather than
    reusing /stream-proxy's progressive video+audio one. Purely additive: on
    any failure here the frontend just doesn't draw a waveform, playback and
    lyrics are unaffected."""
    video_id = video_id.strip()
    if not VIDEO_ID_ONLY_PATTERN.match(video_id):
        return jsonify({"error": "Invalid YouTube video id"}), 400

    if not os.path.isfile(BINARY_PATH):
        return jsonify({"error": f"yt-dlp binary not found at {BINARY_PATH}"}), 500

    if not waveform.ffmpeg_available():
        return jsonify({"error": "ffmpeg is not installed on this server"}), 503

    try:
        result = _get_or_compute_waveform(video_id)
    except subprocess.TimeoutExpired:
        return jsonify({"error": "Timed out while generating the waveform"}), 504
    except OSError as exc:
        return jsonify({"error": f"failed to execute yt-dlp: {exc}"}), 500
    except RuntimeError as exc:
        return jsonify({"error": str(exc)}), 502

    return jsonify(result)


@sock.route("/grade")
def grade(ws):
    """Streams live pitch/energy performance scores while the user sings
    along to the backing track - see audio_grading.py for what this scores
    (and, just as importantly, what it does NOT: there's no isolated
    reference vocal to compare against, so this is not melody-accuracy
    grading against the original song).

    Protocol: the client's first message must be a JSON text frame
    {"sample_rate": <int>} matching the AudioContext sample rate it
    captured at. Every message after that is a binary frame of raw
    little-endian float32 mono PCM samples. The server replies with one
    JSON text frame per score update produced (a few times a second),
    shaped like audio_grading.RealtimeGrader.push_samples()'s output.
    """
    try:
        handshake_raw = ws.receive()
        if handshake_raw is None:
            return

        try:
            handshake = json.loads(handshake_raw)
            sample_rate = int(handshake["sample_rate"])
            if not (GRADE_MIN_SAMPLE_RATE <= sample_rate <= GRADE_MAX_SAMPLE_RATE):
                raise ValueError("sample_rate out of range")
        except (TypeError, ValueError, KeyError, json.JSONDecodeError):
            ws.send(json.dumps({"error": 'First message must be JSON {"sample_rate": <int>}'}))
            return

        grader = RealtimeGrader(sample_rate)

        while True:
            message = ws.receive()
            if message is None:
                break
            if isinstance(message, str):
                # Ignore stray text frames (e.g. a client-side keepalive)
                # rather than tearing down the session over a non-PCM
                # message.
                continue

            try:
                samples = np.frombuffer(message, dtype=np.float32)
            except ValueError:
                # Malformed/truncated frame (not a multiple of 4 bytes) -
                # drop it and keep grading rather than killing the session.
                continue

            for update in grader.push_samples(samples):
                ws.send(json.dumps(update))
    except ConnectionClosed:
        pass


@app.route("/lyrics")
def lyrics():
    artist = request.args.get("artist", "").strip()
    title = request.args.get("title", "").strip()
    duration = request.args.get("duration", type=int)
    if not artist or not title:
        return jsonify({"error": "Missing required query parameters 'artist' and 'title'"}), 400

    result = lyrica_client.get_lyrics_full(artist, title, duration=duration)
    if not result:
        return jsonify({"error": "no lyrics found"}), 404

    return jsonify(result)


@app.route("/metadata")
def metadata():
    artist = request.args.get("artist", "").strip()
    title = request.args.get("title", "").strip()
    if not artist or not title:
        return jsonify({"error": "Missing required query parameters 'artist' and 'title'"}), 400

    result = lyrica_client.get_metadata(artist, title)
    if not result:
        return jsonify({"error": "no metadata found"}), 404

    return jsonify(result)


if __name__ == "__main__":
    # threaded=True: /stream-proxy holds a connection open per in-flight range
    # request (buffering + seeking issue several concurrently), which would
    # otherwise serialize behind the dev server's single worker thread.
    try:
        app_host, app_port = _resolve_host_port()
        if os.environ.get("WERKZEUG_RUN_MAIN") != "true":
            _assert_port_available(app_host, app_port)
    except (RuntimeError, ValueError) as exc:
        print(exc, file=sys.stderr)
        sys.exit(1)

    app.run(host=app_host, port=app_port, debug=True, threaded=True)
