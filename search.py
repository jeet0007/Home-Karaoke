"""Karaoke-focused YouTube search built on the bundled yt-dlp binary."""

import json
import os
import re
import subprocess

BINARY_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "binaries", "yt-dlp")

# Substrings checked against the lowercased title.
BOOST_KEYWORDS = {
    "karaoke": 15,
    "instrumental": 10,
    "backing track": 10,
    "no vocals": 10,
    "minus one": 10,
}
PENALTY_KEYWORDS = {
    "cover": 8,
    "reaction": 8,
}

# Substrings checked against the lowercased uploader/channel name.
CHANNEL_BOOST_KEYWORDS = (
    "karaoke",
    "instrumental",
    "backingtracks",
    "backing track",
    "backing tracks",
    "minus one",
    "karafun",
)
CHANNEL_BOOST = 8

# YouTube auto-generates "<Artist> - Topic" channels for official studio
# releases; these are almost never karaoke tracks.
CHANNEL_NOISE_PATTERN = re.compile(r"-\s*topic$", re.IGNORECASE)
CHANNEL_NOISE_PENALTY = 10

MIN_DURATION_SECONDS = 60
MAX_DURATION_SECONDS = 900
SHORT_DURATION_PENALTY = 12
LONG_DURATION_PENALTY = 6

# Bracketed/parenthesised exact phrases, e.g. "(karaoke version)", "[karaoke]".
EXACT_PHRASE_PATTERN = re.compile(
    r"[\(\[]\s*"
    r"(karaoke(?:\s+version)?|instrumental(?:\s+version)?|backing\s*tracks?|no\s+vocals|minus\s+one|karafun)"
    r"\s*[\)\]]",
    re.IGNORECASE,
)
EXACT_PHRASE_BONUS = 5

LYRICS_KEYWORDS = ("with lyrics", "lyrics on screen")
LYRICS_BONUS = 3

HARD_PENALTY_KEYWORDS = (
    "interview",
    "tutorial",
    "how to",
    "lesson",
    "drum cover",
    "guitar cover",
    "piano cover",
    "bass cover",
    "violin cover",
    "unboxing",
    "vlog",
    "podcast",
    "live performance",
)
HARD_PENALTY_WEIGHT = 15

SCORE_MIN = -30
SCORE_MAX = 50


class KaraokeSearch:
    """Searches YouTube via yt-dlp and ranks results by karaoke-quality signals."""

    def __init__(self, binary_path=None, timeout=30, score_floor=-5):
        self.binary_path = binary_path or BINARY_PATH
        self.timeout = timeout
        self.score_floor = score_floor

    def search(self, query, max_results=10):
        """Search for karaoke versions of `query` (auto-appends "karaoke")."""
        return self._run_search(f"{query} karaoke", max_results)

    def search_raw(self, query, max_results=10):
        """Search YouTube with the query exactly as given, no karaoke bias."""
        return self._run_search(query, max_results)

    def _run_search(self, query, max_results):
        if not os.path.isfile(self.binary_path):
            raise FileNotFoundError(f"yt-dlp binary not found at {self.binary_path}")

        count = max(1, int(max_results))
        cmd = [
            self.binary_path,
            f"ytsearch{count}:{query}",
            "--dump-json",
            "--flat-playlist",
            "--no-warnings",
        ]

        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=self.timeout,
            )
        except subprocess.TimeoutExpired as exc:
            raise TimeoutError(f"yt-dlp search timed out after {self.timeout}s") from exc
        except OSError as exc:
            raise RuntimeError(f"failed to execute yt-dlp: {exc}") from exc

        if proc.returncode != 0:
            stderr = proc.stderr.strip() or "unknown yt-dlp error"
            raise RuntimeError(f"yt-dlp failed: {stderr}")

        entries = []
        for line in proc.stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue

        results = [self._to_result(entry) for entry in entries]
        results = [r for r in results if r["score"] >= self.score_floor]
        results.sort(key=lambda r: r["score"], reverse=True)
        return results

    def _to_result(self, entry):
        title = entry.get("title") or "Untitled"
        video_id = entry.get("id")
        url = entry.get("url") or entry.get("webpage_url")
        if not url and video_id:
            url = f"https://www.youtube.com/watch?v={video_id}"
        uploader = entry.get("uploader") or entry.get("channel") or "Unknown"
        duration = entry.get("duration")

        return {
            "video_id": video_id,
            "title": title,
            "url": url,
            "duration": self._format_duration(duration),
            "duration_seconds": self._safe_int(duration),
            "thumbnail": self._best_thumbnail(entry),
            "uploader": uploader,
            "view_count": entry.get("view_count"),
            "score": self._score(title, uploader, duration),
        }

    @staticmethod
    def _best_thumbnail(entry):
        thumbnails = entry.get("thumbnails") or []
        if thumbnails:
            return thumbnails[-1].get("url")
        return entry.get("thumbnail")

    @staticmethod
    def _safe_int(seconds):
        if seconds is None:
            return None
        try:
            return int(seconds)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _format_duration(seconds):
        if seconds is None:
            return None
        try:
            seconds = int(seconds)
        except (TypeError, ValueError):
            return None
        hours, remainder = divmod(seconds, 3600)
        minutes, secs = divmod(remainder, 60)
        if hours:
            return f"{hours}:{minutes:02d}:{secs:02d}"
        return f"{minutes}:{secs:02d}"

    @staticmethod
    def _score(title, uploader="", duration=None):
        text = title.lower()
        score = 0

        for keyword, weight in BOOST_KEYWORDS.items():
            if keyword in text:
                score += weight
        for keyword, weight in PENALTY_KEYWORDS.items():
            if re.search(rf"\b{re.escape(keyword)}\b", text):
                score -= weight

        if EXACT_PHRASE_PATTERN.search(text):
            score += EXACT_PHRASE_BONUS

        if any(keyword in text for keyword in LYRICS_KEYWORDS):
            score += LYRICS_BONUS

        for keyword in HARD_PENALTY_KEYWORDS:
            if re.search(rf"\b{re.escape(keyword)}\b", text):
                score -= HARD_PENALTY_WEIGHT

        channel = (uploader or "").lower()
        if CHANNEL_NOISE_PATTERN.search(channel):
            score -= CHANNEL_NOISE_PENALTY
        if any(keyword in channel for keyword in CHANNEL_BOOST_KEYWORDS):
            score += CHANNEL_BOOST

        if duration is not None:
            try:
                duration = float(duration)
            except (TypeError, ValueError):
                duration = None
        if duration is not None:
            if duration < MIN_DURATION_SECONDS:
                score -= SHORT_DURATION_PENALTY
            elif duration > MAX_DURATION_SECONDS:
                score -= LONG_DURATION_PENALTY

        return max(SCORE_MIN, min(SCORE_MAX, score))
