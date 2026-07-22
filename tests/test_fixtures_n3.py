"""N3 fixtures — corpus load and schema validation (fixtures-load verifier)."""

from __future__ import annotations

import copy
import unittest
from dataclasses import replace
from pathlib import Path

from fixtures.load import (
    CAMPING_EXPECTED_CONFIDENCE,
    CAMPING_EXPECTED_SCORE,
    FIXTURES_ROOT,
    FixtureLoadError,
    load_fixtures,
    validate_fixture_corpus,
)

REPO_ROOT = Path(__file__).resolve().parents[1]


class FixtureTreeTests(unittest.TestCase):
    def test_required_directories_exist(self) -> None:
        for relative in (
            "pages/golden",
            "search",
            "cassettes/enrich",
            "cassettes/classify",
            "cassettes/explain",
            "niches/camping-fixture",
        ):
            path = FIXTURES_ROOT / relative
            self.assertTrue(path.is_dir(), f"missing directory {path}")


class FixtureLoadTests(unittest.TestCase):
    def test_load_fixtures_returns_populated_corpus(self) -> None:
        corpus = load_fixtures()
        self.assertEqual(len(corpus.page_goldens), 5)
        self.assertGreaterEqual(len(corpus.search_responses), 1)
        self.assertEqual(len(corpus.cassettes["enrich"]), 1)
        self.assertEqual(len(corpus.cassettes["classify"]), 1)
        self.assertEqual(len(corpus.cassettes["explain"]), 1)
        self.assertEqual(corpus.camping_expected["score"], CAMPING_EXPECTED_SCORE)
        self.assertEqual(corpus.camping_expected["confidence"], CAMPING_EXPECTED_CONFIDENCE)

    def test_page_sources_are_non_empty(self) -> None:
        corpus = load_fixtures()
        for name in (
            "article.html",
            "forum_thread.html",
            "marketplace_listing.html",
            "review_page.html",
            "youtube_transcript.vtt",
        ):
            path = corpus.pages_dir / name
            self.assertGreater(len(path.read_text(encoding="utf-8").strip()), 40, name)

    def test_camping_fixture_golden_numbers_are_fixed(self) -> None:
        corpus = load_fixtures()
        expected = corpus.camping_expected
        self.assertEqual(expected["score"], 0.72)
        self.assertEqual(expected["confidence"], 0.65)
        self.assertEqual(
            expected["subscores"],
            {
                "demand": 0.676,
                "growth": 0.583,
                "pain": 0.725,
                "competition": 0.57,
                "content": 0.59,
            },
        )

    def test_missing_search_file_raises(self) -> None:
        corpus = load_fixtures()
        broken = replace(corpus, search_responses={})
        with self.assertRaises(FixtureLoadError):
            validate_fixture_corpus(broken)

    def test_wrong_camping_score_raises(self) -> None:
        corpus = load_fixtures()
        broken_expected = copy.deepcopy(corpus.camping_expected)
        broken_expected["score"] = 0.71
        broken = replace(corpus, camping_expected=broken_expected)
        with self.assertRaises(FixtureLoadError):
            validate_fixture_corpus(broken)


class IntegrationCheckFixtures(unittest.TestCase):
    """Offline integration check for N3 fixtures-load verifier."""

    def test_fixture_corpus_loads_and_validates(self) -> None:
        corpus = load_fixtures()
        self.assertEqual(corpus.camping_signals["niche_id"], "camping-fixture")
        self.assertEqual(len(corpus.camping_signals["signals"]), 5)
        self.assertEqual(corpus.camping_expected["niche_id"], "camping-fixture")


if __name__ == "__main__":
    unittest.main()
