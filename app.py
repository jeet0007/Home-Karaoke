"""Minimal Flask GUI for karaoke-exclusive YouTube search."""

import os
import re
import socket
import subprocess
import sys
import threading
import time
import urllib.parse

import httpx
from flask import Flask, Response, jsonify, render_template, request, stream_with_context

import lyrica_client
from search import KaraokeSearch
from song_search import SongSearch, SongSearchError

app = Flask(__name__)
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


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/songs")
def songs():
    return render_template("song_search.html")


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
    limit = max(1, min(limit, 50))

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
    limit = max(1, min(limit, 50))

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
    limit = max(1, min(limit, 50))

    query = f"{title} {artist}"
    results, error = _run_karaoke_search(query, limit)
    if error:
        return error

    return jsonify({"query": query, "artist": artist, "title": title, "count": len(results), "results": results})


@app.route("/preview")
def preview():
    url = request.args.get("url", "").strip()
    if not url:
        return jsonify({"error": "Missing required query parameter 'url'"}), 400

    match = VIDEO_ID_PATTERN.search(url)
    if not match:
        return jsonify({"error": "Could not extract a video id from that URL"}), 400

    return jsonify({"embed_url": f"https://www.youtube.com/embed/{match.group(1)}"})


@app.route("/player")
def player():
    video_id = request.args.get("video_id", "").strip()
    title = request.args.get("title", "").strip() or "Untitled"
    artist = request.args.get("artist", "").strip() or "Unknown artist"
    url = request.args.get("url", "").strip()

    if not video_id:
        return "Missing required query parameter 'video_id'", 400

    if not url.startswith(("http://", "https://")):
        url = f"https://www.youtube.com/watch?v={video_id}"

    return render_template(
        "player.html",
        video_id=video_id,
        title=title,
        artist=artist,
        url=url,
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
