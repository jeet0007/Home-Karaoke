"""Filters search candidates down to those with lyrics actually retrievable
via the Lyrica sidecar (lyrica_client.py).

Checks run concurrently over a thread pool - doing N sequential HTTP round
trips to Lyrica per search would multiply latency by N. Each check carries
its own timeout (via httpx, passed through lyrica_client.check_lyrics_available)
so a single slow/hanging candidate can't stall the whole batch.

Only the first `cap` candidates are checked at all (default 15) to keep
response times bounded regardless of how many candidates the underlying
search returned; anything beyond the cap is dropped, not silently kept.

Three outcomes per candidate, from lyrica_client.check_lyrics_available:
  - lyrics confirmed available -> kept
  - Lyrica explicitly reports no match -> dropped (the point of this filter)
  - Lyrica errored/unreachable -> kept anyway (fail open), since a Lyrica
    outage filtering every result down to zero would be worse than an
    unfiltered result set. If EVERY candidate in a batch errors, that's a
    strong signal Lyrica itself is down/misconfigured (vs. these particular
    songs lacking lyrics) - callers surface that via the `degraded` flag.
"""

from concurrent.futures import ThreadPoolExecutor

import lyrica_client

DEFAULT_CHECK_CAP = 15
DEFAULT_PER_CHECK_TIMEOUT = 6.0
DEFAULT_MAX_WORKERS = 8


def _check_one(candidate, identity_fn, timeout):
    identities = identity_fn(candidate)
    if isinstance(identities, tuple):
        identities = [identities]

    saw_error = False
    for artist, title in identities:
        if not artist or not title:
            continue
        try:
            if lyrica_client.check_lyrics_available(artist, title, timeout=timeout):
                return candidate, "has_lyrics", (artist, title)
        except lyrica_client.LyricaUnavailableError:
            saw_error = True

    return candidate, ("error" if saw_error else "no_lyrics"), None


def filter_candidates_by_lyrics(
    candidates,
    identity_fn,
    cap=DEFAULT_CHECK_CAP,
    timeout=DEFAULT_PER_CHECK_TIMEOUT,
    max_workers=DEFAULT_MAX_WORKERS,
):
    """Return (kept, degraded).

    `identity_fn(candidate)` must return either a single (artist, title)
    tuple, or a list of such tuples to try in order (used by the fallback
    video path, where title-parsing can't always tell artist and title
    apart - see fallback_search.py). A candidate is kept as soon as any one
    of its identities resolves to confirmed lyrics.

    Candidates kept via the "has lyrics" outcome get a `_resolved_identity`
    key of (artist, title) attached (a shallow-copied dict) recording which
    guess actually worked, since fallback candidates don't otherwise carry
    a clean artist/title. Candidates beyond `cap` are dropped, not returned
    unchecked.

    `degraded` is True only when every checked candidate errored out -
    treated as "Lyrica itself is unreachable", not "nothing has lyrics".
    """
    checked = candidates[:cap]
    if not checked:
        return [], False

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        results = list(executor.map(lambda c: _check_one(c, identity_fn, timeout), checked))

    kept = []
    for candidate, status, identity in results:
        if status == "no_lyrics":
            continue
        if status == "has_lyrics" and identity:
            candidate = {**candidate, "_resolved_identity": identity}
        kept.append(candidate)

    degraded = all(status == "error" for _, status, _ in results)
    return kept, degraded
