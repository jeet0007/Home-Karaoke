"""Filters search candidates down to those with lyrics actually retrievable
via the Lyrica sidecar (lyrica_client.py).

Checks run concurrently over a thread pool - doing N sequential HTTP round
trips to Lyrica per search would multiply latency by N. Each check carries
its own timeout (via httpx, passed through lyrica_client.check_lyrics_available)
so a single slow/hanging candidate can't stall the whole batch.

Each check also asks Lyrica for a restricted, cheap mode by default (see
LYRICS_FILTER_CHECK_MODE below) - these are pre-selection checks over
candidates the user hasn't picked yet, so a quick, narrow check is preferred
over Lyrica's full sequential source chain.

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
import time
from concurrent.futures import ThreadPoolExecutor

import httpx

import lyrica_client

DEFAULT_CHECK_CAP = 15
DEFAULT_PER_CHECK_TIMEOUT = 6.0

# Lyrica's production deployment runs gunicorn with only 2 sync workers
# (sidecar/lyrica/gunicorn.config.py), and the local dev server
# (sidecar/lyrica/run.py) is single-threaded Werkzeug - so the backend can
# genuinely only process 1-2 requests in parallel regardless of how many we
# send at once. Sending 8 (or more) concurrently mostly just queues requests
# behind each other until they breach DEFAULT_PER_CHECK_TIMEOUT client-side,
# which then triggers a retry (see MAX_TIMEOUT_RETRIES) that adds yet another
# request - directly feeding the self-inflicted-429 problem below. 4 keeps
# some pipelining benefit (hides per-request network latency) without wildly
# oversubscribing a backend that can only truly run 1-2 at a time.
DEFAULT_MAX_WORKERS = int(os.environ.get("LYRICS_FILTER_MAX_WORKERS", "4"))

# Small delay between *submitting* consecutive checks to the thread pool, so
# a batch of `cap` candidates doesn't open a burst of simultaneous
# connections against Lyrica's own rate limiter (default "15 per minute" per
# IP, see sidecar/lyrica/src/app.py) and its 1-2-worker backend in the same
# instant. At the default 15-candidate cap this adds at most
# (cap - 1) * stagger seconds of wall-clock time up front - cheap compared to
# the multi-second timeouts/retries a request pile-up otherwise causes.
DEFAULT_SUBMIT_STAGGER_SECONDS = float(
    os.environ.get("LYRICS_FILTER_SUBMIT_STAGGER_SECONDS", "0.1")
)

# Which check mode the pre-selection filter uses by default:
#   "lrclib" (default) - Lyrica's pass=true&sequence=2 mode, restricted to
#     ONLY LRCLIB (fetcher id 2 - see fetch_controller.py FETCHER_MAP).
#     LRCLIB is two small JSON HTTP round trips (lrclib_fetcher.py) that fail
#     in well under a second when there's no match, and this mode never
#     touches Lyrica's YouTube fetcher, whose 3-layer cascade (ytmusicapi ->
#     youtube-transcript-api -> yt-dlp subtitles, each with its own
#     multi-second timeout - see sources/youtube_fetcher.py) is what actually
#     dominates wall-clock time under "fast" mode below.
#   "fast" - Lyrica's fast=true mode, racing LRCLIB + YouTube in parallel
#     (the previous default from perf/fast-lyrics-filter).
#
# Trade-off of the "lrclib" default: a candidate whose ONLY lyrics source is
# YouTube-transcript/NetEase/Megalobiz/Musixmatch/SimpMusic (i.e. not on
# LRCLIB) is now filtered out of search results at this stage, even though
# the post-selection get_lyrics_full()/get_lyrics() full multi-source fetch
# (unaffected by this filter) might still find it once the user actually
# picks that song. Judged acceptable because LRCLIB is Lyrica's largest,
# fastest, most cache-friendly source, and this filter's job is a rough
# "does this look like it has lyrics somewhere" signal over many unpicked
# candidates, not a recall guarantee - see PR description for the measured
# latency trade-off. Overridable via env var without a code change in case
# that trade-off needs retuning later.
LYRICS_FILTER_CHECK_MODE = os.environ.get("LYRICS_FILTER_CHECK_MODE", "lrclib").strip().lower()

# Fetcher id for LRCLIB per Lyrica's fetch_controller.py FETCHER_MAP.
LRCLIB_FETCHER_ID = "2"

# One bounded retry for a timeout specifically (not other error types - see
# module docstring): timeouts are the shape of error most likely to be a
# transient blip (momentary slow upstream source, brief rate limiting) and
# most likely to reoccur if we don't retry at all. Non-timeout errors
# (connection refused, HTTP 5xx, unparsable body) are far more likely to be
# either a real outage or a persistent bug, where a second attempt within
# the same request just spends latency without changing the outcome.
#
# Deliberately NOT extended to retry on HTTP 429 specifically: Lyrica's own
# rate-limit handler (sidecar/lyrica/src/app.py) tells clients to wait 35
# seconds before retrying, and this filter runs synchronously inside a
# user's search - a 35s wait on one candidate would stall the whole batch far
# longer than the latency problem this change is fixing. A 429 already falls
# through as a plain (non-timeout) error below: excluded without a retry, and
# counted toward DEGRADED_ERROR_RATIO so a burst of them still surfaces as
# "Lyrica is struggling" rather than silently eating candidates. Avoiding the
# 429 in the first place (see DEFAULT_MAX_WORKERS/DEFAULT_SUBMIT_STAGGER_SECONDS
# above) is the actual fix; retrying into a limit that's already tripped
# would only make things worse.
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


def _check_kwargs():
    """The lyrica_client.check_lyrics_available kwargs for the configured
    LYRICS_FILTER_CHECK_MODE (see module-level constant above)."""
    if LYRICS_FILTER_CHECK_MODE == "fast":
        return {"fast": True}
    return {"sequence": LRCLIB_FETCHER_ID}


def _check_lyrics_with_retry(artist, title, timeout):
    """Call lyrica_client.check_lyrics_available, retrying once if (and only
    if) the failure was a timeout. Re-raises LyricaUnavailableError if every
    attempt fails."""
    attempts = MAX_TIMEOUT_RETRIES + 1
    for attempt in range(attempts):
        try:
            return lyrica_client.check_lyrics_available(
                artist, title, timeout=timeout, **_check_kwargs()
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
    stagger=DEFAULT_SUBMIT_STAGGER_SECONDS,
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

    `stagger` seconds are slept between submitting consecutive checks to the
    thread pool (not between their completions), so a full batch doesn't
    open all of its connections to Lyrica in the same instant - see
    DEFAULT_SUBMIT_STAGGER_SECONDS above.
    """
    checked = candidates[:cap]
    if not checked:
        return [], False

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = []
        for i, candidate in enumerate(checked):
            if i:
                time.sleep(stagger)
            futures.append(executor.submit(_check_one, candidate, identity_fn, timeout))
        results = [future.result() for future in futures]

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
