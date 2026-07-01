"""Song-identity search via ytmusicapi.

Resolves free text to a clean {artist, title, album} identity using YouTube
Music's catalog metadata, instead of guessing artist/title from a messy
video title/channel name (see search.py for the karaoke *video* ranking that
consumes the clean identity this module produces).
"""

from ytmusicapi import YTMusic

_ARTIST_SEPARATOR = ", "


class SongSearchError(Exception):
    """Raised when the ytmusicapi lookup fails (network, timeout, parsing)."""

    def __init__(self, message, status_code=502):
        super().__init__(message)
        self.status_code = status_code


def _clean_artist(entry):
    names = [a.get("name") for a in (entry.get("artists") or []) if a.get("name")]
    return _ARTIST_SEPARATOR.join(names) if names else "Unknown artist"


def _cover_art(entry):
    """Best-effort cover art straight from ytmusicapi's own search result -
    no extra network round trip per candidate, unlike Lyrica's metadata
    lookup, so this is safe to attach to every result in a search response."""
    thumbnails = entry.get("thumbnails") or []
    return thumbnails[-1].get("url", "") if thumbnails else ""


def _clean_result(entry):
    return {
        "artist": _clean_artist(entry),
        "title": entry.get("title") or "Untitled",
        "album": (entry.get("album") or {}).get("name"),
        "duration_seconds": entry.get("duration_seconds"),
        "ytmusic_video_id": entry.get("videoId"),
        "cover_art": _cover_art(entry),
    }


class SongSearch:
    """Looks up clean song identities (artist/title/album) via ytmusicapi."""

    def __init__(self, client_factory=YTMusic):
        self._client_factory = client_factory
        self._client = None

    def _get_client(self):
        if self._client is None:
            self._client = self._client_factory()
        return self._client

    def search(self, query, limit=10):
        try:
            client = self._get_client()
            raw_results = client.search(query=query, filter="songs", limit=limit)
        except Exception as exc:
            status_code = 504 if "timed out" in str(exc).lower() or "timeout" in str(exc).lower() else 502
            raise SongSearchError(f"ytmusicapi search failed: {exc}", status_code=status_code) from exc

        return [_clean_result(entry) for entry in raw_results if entry.get("videoId")][:limit]
