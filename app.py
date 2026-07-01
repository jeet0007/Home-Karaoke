"""Minimal Flask GUI for karaoke-exclusive YouTube search."""

import os
import re
import subprocess
import time

from flask import Flask, jsonify, render_template, request

from search import KaraokeSearch

app = Flask(__name__)
karaoke_search = KaraokeSearch()
BINARY_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "binaries", "yt-dlp")

VIDEO_ID_PATTERN = re.compile(
    r"(?:v=|/videos/|embed/|youtu\.be/|/v/|/shorts/|/live/)([A-Za-z0-9_-]{11})"
)
VIDEO_ID_ONLY_PATTERN = re.compile(r"^[A-Za-z0-9_-]{11}$")


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


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/search")
def search():
    query = request.args.get("q", "").strip()
    if not query:
        return jsonify({"error": "Missing required query parameter 'q'"}), 400

    limit = request.args.get("limit", 10, type=int) or 10
    limit = max(1, min(limit, 50))

    try:
        results = karaoke_search.search(query, max_results=limit)
    except FileNotFoundError as exc:
        return jsonify({"error": str(exc)}), 500
    except TimeoutError as exc:
        return jsonify({"error": str(exc)}), 504
    except RuntimeError as exc:
        return jsonify({"error": str(exc)}), 502

    return jsonify({"query": query, "count": len(results), "results": results})


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

    if not url:
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

    url = f"https://www.youtube.com/watch?v={video_id}"
    timeout = 20
    started_at = time.monotonic()

    try:
        stream_urls = _resolve_stream_urls(
            url,
            "bestvideo[ext=mp4]+bestaudio/best[ext=mp4]/best",
            timeout,
        )
    except subprocess.TimeoutExpired:
        return jsonify({"error": "Timed out while resolving the video stream URL"}), 504
    except OSError as exc:
        return jsonify({"error": f"failed to execute yt-dlp: {exc}"}), 500
    except RuntimeError as exc:
        return jsonify({"error": str(exc)}), 502

    if not stream_urls:
        return jsonify({"error": "yt-dlp did not return a stream URL"}), 502

    response = {}
    if len(stream_urls) > 1:
        remaining_timeout = max(1, timeout - int(time.monotonic() - started_at))
        try:
            progressive_urls = _resolve_stream_urls(url, "best[ext=mp4]/best", remaining_timeout)
        except subprocess.TimeoutExpired:
            return jsonify({"error": "Timed out while resolving a browser-playable stream URL"}), 504
        except OSError as exc:
            return jsonify({"error": f"failed to execute yt-dlp: {exc}"}), 500
        except RuntimeError as exc:
            return jsonify({"error": str(exc)}), 502

        if not progressive_urls:
            return jsonify({"error": "yt-dlp did not return a browser-playable stream URL"}), 502

        response["stream_url"] = progressive_urls[0]
        response["warning"] = (
            "yt-dlp returned separate best video/audio URLs; using a progressive browser-playable stream."
        )
    else:
        response["stream_url"] = stream_urls[0]

    return jsonify(response)


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=True)
