"""Minimal Flask GUI for karaoke-exclusive YouTube search."""

import re

from flask import Flask, jsonify, render_template, request

from search import KaraokeSearch

app = Flask(__name__)
karaoke_search = KaraokeSearch()

VIDEO_ID_PATTERN = re.compile(r"(?:v=|/videos/|embed/|youtu\.be/|/v/)([A-Za-z0-9_-]{11})")


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


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=True)
