"""Filters search candidates down to those with lyrics actually retrievable
via the Lyrica sidecar (lyrica_client.py).

Checks run concurrently over a thread pool - doing N sequential HTTP round
trips to Lyrica per search would multiply latency by N. Each check carries
its own timeout (via httpx, passed through lyrica_client.check_lyrics_available)
so a single slow/hanging candidate can't stall the whole batch.

Each check also asks Lyrica for its fast=true mode by default (see
FAST_LYRICS_CHECK below) - these are pre-selection checks over candidates
the user hasn't picked yet, so a quick LRCLIB+YouTube race is preferred over
Lyrica's full sequential source chain.

Only the first `cap` candidates are checked at all (default 15) to keep
response times bounded regardless of how many candidates the underlying
search returned; anything beyond the cap is dropped, not silently kept.

Three outcomes per candidate, from lyrica_client.check_lyrics_available:
  - lyrics confirmed available -> kept
  - Lyrica explicitly reports no match -> dropped (the point of this filter)
  - Lyrica errored/unreachable (network failure, timeout, unparsable
    response) -> ALSO dropped. This product's requirement is strict: "no
    lyrics available" and "couldn't confirm lyrics are available" must both
    exclude a candidate, since a user who opens the player expects lyrics to
    actually be there. A per-candidate error gets one bounded retry first
    (see MAX_TIMEOUT_RETRIES) if the error was specifically a timeout, to
    absorb a transient blip without paying the cost of a second full outage
    round trip on every other error type.

    We deliberately do NOT fail a whole batch open just because one
    candidate's check errored - that conflates "this one song's check had a
    hiccup" with "Lyrica itself is down." Instead, a `degraded` flag is
    raised at the batch level when a large fraction of checks in the same
    batch error (see DEGRADED_ERROR_RATIO below), which is the actual signal
    that something systemic (Lyrica down/misconfigured/rate-limiting) is
    going on rather than one flaky candidate.
"""

import os
from concurrent.futures import ThreadPoolExecutor

import httpx

import lyrica_client

DEFAULT_CHECK_CAP = 15
DEFAULT_PER_CHECK_TIMEOUT = 6.0
DEFAULT_MAX_WORKERS = 8

# These are pre-selection checks over candidates the user hasn't picked yet
# (and may never pick) - Lyrica's fast=true mode races only its two fastest,
# most reliable sources (LRCLIB + YouTube) in parallel, instead of walking
# its full 6-source sequential chain. Trade-off: a candidate whose ONLY
# lyrics source is one of the four skipped ones (NetEase, Megalobiz,
# Musixmatch, SimpMusic) can be filtered out here even though the full
# post-selection fetch (get_lyrics_full/get_lyrics, unaffected by this flag)
# might have found it. That's judged an acceptable trade for search-time
# latency/reliability - see PR description. Overridable via env var without
# a code change in case that trade-off needs retuning later.
FAST_LYRICS_CHECK = os.environ.get("LYRICS_FILTER_FAST_CHECK", "true").lower() != "false"

# One bounded retry for a timeout specifically (not other error types - see
# module docstring): timeouts are the shape of error most likely to be a
# transient blip (momentary slow upstream source, brief rate limiting) and
# most likely to reoccur if we don't retry at all. Non-timeout errors
# (connection refused, HTTP 5xx, unparsable body) are far more likely to be
# either a real outage or a persistent bug, where a second attempt within
# the same request just spends latency without changing the outcome.
MAX_TIMEOUT_RETRIES = 1

# Fraction of *checked* candidates that must error before we call the batch
# "degraded" (Lyrica itself likely down/misconfigured) rather than treating
# each error as that one candidate's problem. 0.7 is chosen to require a
# clear majority-plus: candidates in a batch hit Lyrica independently (often
# different songs, sometimes different upstream lyrics sources), so a batch
# where most of them fail together is far more consistent with a shared
# failure point (Lyrica process down, network path broken, rate limited)
# than with several unrelated songs coincidentally erroring at once. A
# single stray error (or even a few, in a batch of 15) stays well under this
# ratio and is handled as an ordinary per-candidate exclusion instead.
DEGRADED_ERROR_RATIO = 0.7


def _check_lyrics_with_retry(artist, title, timeout):
    """Call lyrica_client.check_lyrics_available, retrying once if (and only
    if) the failure was a timeout. Re-raises LyricaUnavailableError if every
    attempt fails."""
    attempts = MAX_TIMEOUT_RETRIES + 1
    for attempt in range(attempts):
        try:
            return lyrica_client.check_lyrics_available(
                artist, title, timeout=timeout, fast=FAST_LYRICS_CHECK
            )
        except lyrica_client.LyricaUnavailableError as exc:
            is_timeout = isinstance(exc.__cause__, httpx.TimeoutException)
            if attempt < attempts - 1 and is_timeout:
                continue
            raise


def _check_one(candidate, identity_fn, timeout):
    identities = identity_fn(candidate)
    if isinstance(identities, tuple):
        identities = [identities]

    saw_error = False
    for artist, title in identities:
        if not artist or not title:
            continue
        try:
            if _check_lyrics_with_retry(artist, title, timeout):
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

    A candidate is excluded whenever we don't have confirmed lyrics for it -
    that includes both a confirmed "no lyrics" from Lyrica AND a check that
    errored out (network/timeout/parse failure) after its retry budget is
    spent. Unverified is not the same as verified, and this product's
    requirement is strict about lyrics actually being available.

    `degraded` is True when at least DEGRADED_ERROR_RATIO of the checked
    candidates errored out - treated as "Lyrica itself is unreachable"
    (systemic), not "these particular songs lack lyrics." Below that ratio,
    errors are assumed to be per-candidate noise and are simply excluded
    without a batch-level warning.
    """
    checked = candidates[:cap]
    if not checked:
        return [], False

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        results = list(executor.map(lambda c: _check_one(c, identity_fn, timeout), checked))

    kept = []
    error_count = 0
    for candidate, status, identity in results:
        if status == "no_lyrics":
            continue
        if status == "error":
            error_count += 1
            continue
        if status == "has_lyrics" and identity:
            candidate = {**candidate, "_resolved_identity": identity}
        kept.append(candidate)

    degraded = (error_count / len(checked)) >= DEGRADED_ERROR_RATIO
    return kept, degraded
