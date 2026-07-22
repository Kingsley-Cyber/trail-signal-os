"""N26 score — deterministic geo-mean + interactions (LAW 1, no LLM)."""

from __future__ import annotations

import copy
import json
import unittest
from pathlib import Path

from fixtures.load import (
    CAMPING_EXPECTED_CONFIDENCE,
    CAMPING_EXPECTED_SCORE,
    load_fixtures,
)
from guards.runtime_guards import guard12_assert_score_reproducible
from guards.static_lint import lint_import_purity
from signal_engine.score import (
    CODE_VERSION,
    ScoreError,
    build_opportunity_v1,
    compute_score_result,
    load_weights,
    score,
    score_camping_fixture,
    shrink_toward_neutral,
    validate_opportunity_v1,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
AS_OF = "2026-07-21T12:00:00Z"
CONFIG_HASH = "sha256:" + ("a" * 64)


class ShrinkFormulaTests(unittest.TestCase):
    def test_shrink_matches_doc_08_worked_example(self) -> None:
        self.assertAlmostEqual(shrink_toward_neutral(0.72, 0.80), 0.676)
        self.assertAlmostEqual(shrink_toward_neutral(0.65, 0.55), 0.5825)
        self.assertAlmostEqual(shrink_toward_neutral(0.80, 0.75), 0.725)
        self.assertAlmostEqual(shrink_toward_neutral(0.40, 0.70), 0.430)
        self.assertAlmostEqual(shrink_toward_neutral(0.68, 0.50), 0.590)


class WeightsTests(unittest.TestCase):
    def test_load_weights_has_expected_version(self) -> None:
        weights = load_weights()
        self.assertEqual(weights["version"], "w-2026.07.21")
        axis_sum = sum(float(weights["axis_weights"][key]) for key in weights["axis_weights"])
        self.assertAlmostEqual(axis_sum, 1.0)


class CampingFixtureScoreTests(unittest.TestCase):
    def test_camping_fixture_matches_golden(self) -> None:
        corpus = load_fixtures()
        weights = load_weights()
        result = score(corpus.camping_signals, weights, as_of=AS_OF)
        expected = corpus.camping_expected

        self.assertEqual(result.score, CAMPING_EXPECTED_SCORE)
        self.assertEqual(result.confidence, CAMPING_EXPECTED_CONFIDENCE)
        self.assertEqual(result.subscores, expected["subscores"])
        self.assertFalse(result.hostile_dependent)
        self.assertEqual(result.coverage_gaps, ())

    def test_score_camping_fixture_builds_valid_opportunity(self) -> None:
        opportunity = score_camping_fixture(config_hash=CONFIG_HASH)
        validate_opportunity_v1(opportunity)
        golden = json.loads(
            (REPO_ROOT / "fixtures" / "niches" / "camping-fixture" / "expected_opportunity.json").read_text(
                encoding="utf-8",
            ),
        )
        self.assertEqual(opportunity["score"], golden["score"])
        self.assertEqual(opportunity["confidence"], golden["confidence"])
        self.assertEqual(opportunity["subscores"], golden["subscores"])
        self.assertEqual(opportunity["scored_from"], golden["scored_from"])


class ReproducibilityTests(unittest.TestCase):
    def test_score_is_identical_across_repeated_runs(self) -> None:
        corpus = load_fixtures()
        weights = load_weights()

        def score_once() -> float:
            return score(corpus.camping_signals, weights, as_of=AS_OF).score

        guard12_assert_score_reproducible(
            score_once,
            expected=CAMPING_EXPECTED_SCORE,
        )

    def test_compute_score_result_is_stable(self) -> None:
        corpus = load_fixtures()
        weights = load_weights()
        first = compute_score_result(
            corpus.camping_signals,
            weights,
            niche_id="camping-fixture",
            as_of=AS_OF,
        )
        second = compute_score_result(
            corpus.camping_signals,
            weights,
            niche_id="camping-fixture",
            as_of=AS_OF,
        )
        self.assertEqual(first, second)


class DeadNicheTests(unittest.TestCase):
    def test_low_demand_scores_below_camping_fixture(self) -> None:
        corpus = load_fixtures()
        weights = load_weights()
        camping = score(corpus.camping_signals, weights, as_of=AS_OF)

        dead_signals = copy.deepcopy(corpus.camping_signals)
        for signal in dead_signals["signals"]:
            if signal["signal_type"] == "demand":
                signal["normalized_score"] = 0.15

        dead = score(dead_signals, weights, as_of=AS_OF)
        self.assertLess(dead.score, camping.score)
        self.assertLess(dead.subscores["demand"], camping.subscores["demand"])


class CoverageGateIntegrationTests(unittest.TestCase):
    def test_hard_required_missing_blocks_scoring(self) -> None:
        corpus = load_fixtures()
        weights = load_weights()
        partial = copy.deepcopy(corpus.camping_signals)
        partial["signals"] = [
            signal for signal in partial["signals"] if signal["signal_type"] != "pain"
        ]
        with self.assertRaises(ScoreError):
            score(partial, weights, as_of=AS_OF)


class ImportPurityTests(unittest.TestCase):
    def test_score_module_is_import_pure(self) -> None:
        source = (REPO_ROOT / "signal_engine" / "score.py").read_text(encoding="utf-8")
        lint_import_purity(source, path=Path("signal_engine/score.py"))


class IntegrationCheckScore(unittest.TestCase):
    """N26 integration_check: reproducible (g12); camping-fixture → 0.72."""

    def test_integration_check_score(self) -> None:
        corpus = load_fixtures()
        weights = load_weights()
        expected = corpus.camping_expected

        result = score(corpus.camping_signals, weights, as_of=AS_OF)
        self.assertEqual(result.score, 0.72)
        self.assertEqual(result.confidence, 0.65)

        guard12_assert_score_reproducible(
            lambda: score(corpus.camping_signals, weights, as_of=AS_OF).score,
            expected=0.72,
        )

        opportunity = build_opportunity_v1(
            opportunity_id=str(expected["opportunity_id"]),
            niche_id=str(corpus.camping_signals["niche_id"]),
            candidate=dict(expected["candidate"]),
            score_result=result,
            weights=weights,
            config_hash=CONFIG_HASH,
            as_of=AS_OF,
            generating_queries=list(expected["generating_queries"]),
            created_at=str(expected["provenance"]["created_at"]),
        )
        validate_opportunity_v1(opportunity)
        self.assertEqual(opportunity["score"], expected["score"])
        self.assertEqual(opportunity["subscores"], expected["subscores"])
        self.assertEqual(opportunity["confidence"], expected["confidence"])
        self.assertEqual(CODE_VERSION, "score-1.0.0")

        score_source = (REPO_ROOT / "signal_engine" / "score.py").read_text(encoding="utf-8")
        lint_import_purity(score_source, path=Path("signal_engine/score.py"))


if __name__ == "__main__":
    unittest.main()
