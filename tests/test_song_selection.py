import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from search.song_selection import (  # noqa: E402
    DURATION_MISMATCH_PENALTY,
    combined_score,
    duration_proximity_score,
    pick_best_candidate,
)


class DurationProximityScoreTestCase(unittest.TestCase):
    def test_near_exact_match_gets_top_bonus(self):
        self.assertEqual(duration_proximity_score(200, 200), 10)
        self.assertEqual(duration_proximity_score(203, 200), 10)

    def test_small_gap_gets_smaller_bonus(self):
        self.assertEqual(duration_proximity_score(206, 200), 6)

    def test_moderate_gap_gets_small_bonus(self):
        self.assertEqual(duration_proximity_score(212, 200), 3)

    def test_within_half_minute_is_neutral(self):
        self.assertEqual(duration_proximity_score(225, 200), 0)

    def test_within_a_minute_is_penalized(self):
        self.assertEqual(duration_proximity_score(250, 200), -6)

    def test_within_two_minutes_is_penalized_more(self):
        self.assertEqual(duration_proximity_score(300, 200), -15)

    def test_gross_mismatch_gets_the_full_penalty(self):
        self.assertEqual(duration_proximity_score(500, 200), DURATION_MISMATCH_PENALTY)

    def test_symmetric_around_target(self):
        self.assertEqual(duration_proximity_score(197, 200), duration_proximity_score(203, 200))

    def test_missing_candidate_duration_is_neutral(self):
        self.assertEqual(duration_proximity_score(None, 200), 0)

    def test_missing_target_duration_is_neutral(self):
        self.assertEqual(duration_proximity_score(200, None), 0)


class CombinedScoreTestCase(unittest.TestCase):
    def test_adds_karaoke_score_and_duration_bonus(self):
        candidate = {"score": 20, "duration_seconds": 203}
        self.assertEqual(combined_score(candidate, 200), 30)

    def test_missing_karaoke_score_defaults_to_zero(self):
        candidate = {"duration_seconds": 200}
        self.assertEqual(combined_score(candidate, 200), 10)


class PickBestCandidateTestCase(unittest.TestCase):
    def test_empty_candidates_returns_none(self):
        self.assertIsNone(pick_best_candidate([], 200))

    def test_picks_highest_combined_score(self):
        candidates = [
            {"video_id": "a", "score": 10, "duration_seconds": 200},
            {"video_id": "b", "score": 25, "duration_seconds": 200},
        ]

        best = pick_best_candidate(candidates, 200)

        self.assertEqual(best["video_id"], "b")
        self.assertEqual(best["combined_score"], 35)

    def test_duration_proximity_can_flip_the_ranking(self):
        # "b" out-ranks "a" on raw karaoke score alone, but its duration is
        # wildly off (a different edit/medley) while "a" matches almost
        # exactly - the closer-duration, lower-raw-score candidate should win.
        candidates = [
            {"video_id": "a", "score": 12, "duration_seconds": 201},
            {"video_id": "b", "score": 25, "duration_seconds": 500},
        ]

        best = pick_best_candidate(candidates, 200)

        self.assertEqual(best["video_id"], "a")

    def test_solid_score_lead_survives_a_modest_duration_gap(self):
        # "a" has a decent (12-point) karaoke-score lead over "b" and only a
        # neutral-tier duration gap (25s, still within the "no bonus/penalty"
        # band), while "b" gets the maximum possible duration bonus (a
        # near-exact match). Even in that worst case for "a", its score lead
        # is bigger than the largest duration swing it's giving up, so it
        # should still win - duration proximity is a tie-breaker, not the
        # primary signal, when the score gap is this large.
        candidates = [
            {"video_id": "a", "score": 22, "duration_seconds": 225},
            {"video_id": "b", "score": 10, "duration_seconds": 200},
        ]

        best = pick_best_candidate(candidates, 200)

        self.assertEqual(best["video_id"], "a")

    def test_ties_keep_the_first_seen_candidate(self):
        candidates = [
            {"video_id": "first", "score": 20, "duration_seconds": 200},
            {"video_id": "second", "score": 20, "duration_seconds": 200},
        ]

        best = pick_best_candidate(candidates, 200)

        self.assertEqual(best["video_id"], "first")

    def test_missing_durations_fall_back_to_raw_karaoke_score(self):
        candidates = [
            {"video_id": "a", "score": 10, "duration_seconds": None},
            {"video_id": "b", "score": 15, "duration_seconds": None},
        ]

        best = pick_best_candidate(candidates, None)

        self.assertEqual(best["video_id"], "b")


if __name__ == "__main__":
    unittest.main()
