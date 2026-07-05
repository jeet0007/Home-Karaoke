import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from search.fallback_search import clean_title, parse_title_identity_candidates  # noqa: E402

# Real yt-dlp karaoke search results (ytsearch, --dump-json --flat-playlist)
# verified against "Let Her Go Passenger karaoke", "Bohemian Rhapsody Queen
# karaoke", "Shape of You Ed Sheeran karaoke", and "Rolling in the Deep Adele
# karaoke" - titles alternate between "<Artist> - <Song>" and
# "<Song> - <Artist>" ordering, confirming we can't assume one order.
REAL_TITLES_TO_EXPECTED_PAIR = {
    "Let Her Go  - Passenger (Karaoke Version)": {"Let Her Go", "Passenger"},
    "Queen - Bohemian Rhapsody (Karaoke Version)": {"Queen", "Bohemian Rhapsody"},
    "Karaoke♬ Bohemian Rhapsody - Queen 【No Guide Melody】 Instrumental": {"Bohemian Rhapsody", "Queen"},
    "Ed Sheeran - Shape Of You (Karaoke Version)": {"Ed Sheeran", "Shape Of You"},
    "Shape of You - Ed Sheeran Karaoke 【No Guide Melody】 Instrumental": {"Shape of You", "Ed Sheeran"},
    "Adele - Rolling In The Deep (Karaoke Version)": {"Adele", "Rolling In The Deep"},
    "Rolling in the Deep - Adele Karaoke【With Guide Melody】": {"Rolling in the Deep", "Adele"},
}


class CleanTitleTestCase(unittest.TestCase):
    def test_strips_bracketed_karaoke_noise(self):
        self.assertEqual(clean_title("Queen - Bohemian Rhapsody (Karaoke Version)"), "Queen - Bohemian Rhapsody")

    def test_strips_bare_karaoke_word_and_symbols(self):
        self.assertEqual(
            clean_title("Karaoke♬ Bohemian Rhapsody - Queen 【No Guide Melody】 Instrumental"),
            "Bohemian Rhapsody - Queen",
        )

    def test_strips_official_video_noise(self):
        self.assertEqual(clean_title("Queen – Bohemian Rhapsody (Official Video Remastered)"), "Queen – Bohemian Rhapsody")

    def test_empty_title_returns_empty(self):
        self.assertEqual(clean_title(""), "")
        self.assertEqual(clean_title(None), "")


class ParseTitleIdentityCandidatesTestCase(unittest.TestCase):
    def test_real_karaoke_titles_produce_both_orderings(self):
        for title, expected_pair in REAL_TITLES_TO_EXPECTED_PAIR.items():
            with self.subTest(title=title):
                candidates = parse_title_identity_candidates(title)
                self.assertEqual(len(candidates), 2)
                # order is ambiguous by design, but both real values must appear
                # as one of the two guesses, and the guesses must be swaps of
                # each other.
                self.assertEqual(set(candidates[0]), expected_pair)
                self.assertEqual(candidates[0], candidates[1][::-1])

    def test_no_separator_returns_no_candidates(self):
        self.assertEqual(parse_title_identity_candidates("Just A Plain Title With No Dash"), [])

    def test_empty_title_returns_no_candidates(self):
        self.assertEqual(parse_title_identity_candidates(""), [])

    def test_pipe_separator_supported(self):
        candidates = parse_title_identity_candidates("Passenger | Let Her Go (Official Video)")
        self.assertEqual(candidates, [("Passenger", "Let Her Go"), ("Let Her Go", "Passenger")])


if __name__ == "__main__":
    unittest.main()
