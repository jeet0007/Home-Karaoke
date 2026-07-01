"""Auto-picks the single best karaoke video for a known song identity.

karaoke_search.py already ranks videos by karaoke-quality signals (title/
channel keywords, hard penalties for covers/tutorials/etc - see search.py's
KaraokeSearch._score). That ranking has no idea which *specific recording*
of the song a video is timed to, though - two "Karaoke Version" videos can
score identically on content signals while one is cut to a 3:30 radio edit
and the other to a 6:00 extended/live version. Duration proximity against
the song's known duration (from Lyrica's metadata) is the cheapest signal
available to disambiguate that, so this module folds a duration bonus/
penalty into KaraokeSearch's score to produce one final combined score, and
the caller (see /select-song in app.py) uses whichever candidate comes out on
top - never a set of alternatives, since the product no longer lets the user
pick a video at all.

Weighting
---------
KaraokeSearch scores span roughly -30..50 (see search.py's SCORE_MIN/MAX),
but the practical gap between the best and next-best candidate *for the same
query* is usually much narrower - tens of points at most, from a handful of
keyword/channel boosts and hard penalties stacking. Duration proximity is
folded in on a deliberately smaller scale (+10..-25) so that:

  - Among candidates with similar karaoke-quality scores, the closer-duration
    one wins (a few points of duration bonus is enough to break a near-tie).
  - A gross duration mismatch (more than 120s off - almost certainly a
    different edit, a medley, or a looped/extended upload, not just a
    different intro length) costs enough (-25) that only a large
    karaoke-score lead can still win against a closer-duration alternative,
    matching the product requirement that big mismatches "usually lose"
    rather than being an absolute disqualifier.
  - A missing duration on either side (candidate or target) contributes
    nothing (0) rather than penalizing - we don't punish a candidate for data
    we simply don't have.

Tiers are intentionally coarse (a handful of buckets, not a continuous
function of the gap) since duration here is a secondary/tie-breaking signal,
not the primary ranking axis - a step function is easier to reason about and
tune than a formula that implies false precision.
"""

# (max_diff_seconds, bonus) pairs, checked in order; the first tier whose
# max_diff the gap falls within wins. Anything past the last tier's max_diff
# falls through to DURATION_MISMATCH_PENALTY below.
DURATION_SCORE_TIERS = (
    (3, 10),
    (8, 6),
    (15, 3),
    (30, 0),
    (60, -6),
    (120, -15),
)
DURATION_MISMATCH_PENALTY = -25


def duration_proximity_score(candidate_duration_s, target_duration_s):
    """Return the duration bonus/penalty for one candidate, given the song's
    known target duration (seconds). 0 if either duration is unknown."""
    if candidate_duration_s is None or target_duration_s is None:
        return 0

    diff = abs(candidate_duration_s - target_duration_s)
    for max_diff, bonus in DURATION_SCORE_TIERS:
        if diff <= max_diff:
            return bonus
    return DURATION_MISMATCH_PENALTY


def combined_score(candidate, target_duration_s):
    """KaraokeSearch's own ranking score plus the duration-proximity
    bonus/penalty for this one candidate."""
    return candidate.get("score", 0) + duration_proximity_score(
        candidate.get("duration_seconds"), target_duration_s
    )


def pick_best_candidate(candidates, target_duration_s):
    """Return the single best-ranked candidate (by combined_score), with a
    `combined_score` key attached, or None if `candidates` is empty.

    Ties keep whichever candidate was seen first - candidates arrive
    pre-sorted best-karaoke-score-first from KaraokeSearch.search(), so a tie
    on combined_score keeps the one KaraokeSearch itself ranked higher.
    """
    if not candidates:
        return None

    best_candidate = None
    best_score = None
    for candidate in candidates:
        score = combined_score(candidate, target_duration_s)
        if best_score is None or score > best_score:
            best_candidate = candidate
            best_score = score

    return {**best_candidate, "combined_score": best_score}
