"""Best-effort artist/title extraction for the direct-video-search fallback.

Used only when ytmusicapi's song search (song_search.py) can't resolve a
query, so we fall back to ranking raw karaoke videos (search.py) directly -
those videos have no structured artist/title metadata (confirmed via yt-dlp
--dump-json: `track`/`artist`/`album` are empty for these user-uploaded
karaoke videos, unlike Content-ID-matched official audio), only a title.

Real karaoke titles observed via yt-dlp searches split roughly evenly
between "<Artist> - <Song> (Karaoke ...)" and "<Song> - <Artist>
(Karaoke ...)", e.g.:
    "Queen - Bohemian Rhapsody (Karaoke Version)"        (artist first)
    "Let Her Go - Passenger (Karaoke Version)"           (song first)
    "Shape of You - Ed Sheeran Karaoke [No Guide Melody]" (song first)
There's no reliable signal in the title for which side is which, so instead
of guessing an order, parse_title_identity_candidates() returns both and
lets the caller (the lyrics-availability filter) try each until one
resolves.
"""

import re

_BRACKET_NOISE_PATTERN = re.compile(
    r"[\(\[【]\s*(karaoke(?:\s+version)?|instrumental(?:\s+version)?|no\s+vocals|minus\s+one|"
    r"no\s+guide\s+melody|with\s+guide\s+melody|backing\s*tracks?|piano\s+karaoke|"
    r"(?:female|male|original)\s+key(?:\s*-\s*piano\s+karaoke)?|lyrics\s+on\s+screen|with\s+lyrics|"
    r"official\s+(?:music\s+)?video(?:\s+remastered)?|official\s+audio|lyrics)"
    r"[^\)\]】]*[\)\]】]",
    re.IGNORECASE,
)
_BARE_NOISE_PATTERN = re.compile(r"\bkaraoke\b|\binstrumental\b", re.IGNORECASE)
_DECORATIVE_SYMBOL_PATTERN = re.compile(r"[♬★☆•▶►◆✦✧🎤🎶]")
_SEPARATOR_PATTERN = re.compile(r"\s*[-–—|]\s*")


def clean_title(raw_title):
    """Strip karaoke/instrumental noise phrases and decorative symbols."""
    text = raw_title or ""
    text = _DECORATIVE_SYMBOL_PATTERN.sub(" ", text)
    text = _BRACKET_NOISE_PATTERN.sub(" ", text)
    text = _BARE_NOISE_PATTERN.sub(" ", text)
    return re.sub(r"\s+", " ", text).strip(" -–—|")


def parse_title_identity_candidates(raw_title):
    """Return [(a, b), (b, a)] guesses for (artist, title), or [] if the
    cleaned title has no dash/pipe-style separator to split on."""
    cleaned = clean_title(raw_title)
    if not cleaned:
        return []

    parts = _SEPARATOR_PATTERN.split(cleaned, maxsplit=1)
    if len(parts) != 2 or not parts[0].strip() or not parts[1].strip():
        return []

    first, second = parts[0].strip(), parts[1].strip()
    return [(first, second), (second, first)]
