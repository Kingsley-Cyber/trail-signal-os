"""N25 coverage gate — admits or scores-with-gaps (LAW 1, deterministic)."""

from __future__ import annotations

import copy
import unittest
from pathlib import Path

from fixtures.load import load_fixtures
from guards.static_lint import lint_import_purity
from signal_engine.coverage import (
    CODE_VERSION,
    MIN_CELL_CONFIDENCE,
    CoverageGrid,
    build_grid_from_signals,
    evaluate_coverage_gate,
    find_coverage_gaps,
    hard_required_met,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
AS_OF = "2026-07-21T12:00:00Z"
DEADLINE = "2026-07-22T12:00:00Z"
BEFORE_DEADLINE = "2026-07-21T18:00:00Z"
AFTER_DEADLINE = "2026-07-23T12:00:00Z"


class CoverageGridTests(unittest.TestCase):
    def test_upsert_keeps_best_confidence_per_cell(self) -> None:
        grid = CoverageGrid(niche_id="niche-a")
        low = {
            "signal_id": "sig_a_demand_low",
            "signal_type": "demand",
            "source": {"domain": "a.example", "tier": "open"},
            "confidence": 0.35,
        }
        high = {
            "signal_id": "sig_a_demand_high",
            "signal_type": "demand",
            "source": {"domain": "b.example", "tier": "open"},
            "confidence": 0.82,
        }
        self.assertTrue(grid.upsert_signal(low, updated_at=AS_OF))
        self.assertTrue(grid.upsert_signal(high, updated_at=AS_OF))
        self.assertFalse(grid.upsert_signal(low, updated_at=AS_OF))
        cell = grid.get_cell("demand", "open")
        assert cell is not None
        self.assertAlmostEqual(cell.best_confidence, 0.82)
        self.assertEqual(cell.contributing_signal_ids, ("sig_a_demand_high",))

    def test_upsert_merges_equal_confidence_signal_ids(self) -> None:
        grid = CoverageGrid(niche_id="niche-a")
        first = {
            "signal_id": "sig_b",
            "signal_type": "pain",
            "source": {"domain": "a.example", "tier": "defended"},
            "confidence": 0.55,
        }
        second = {
            "signal_id": "sig_a",
            "signal_type": "pain",
            "source": {"domain": "b.example", "tier": "defended"},
            "confidence": 0.55,
        }
        grid.upsert_signal(first, updated_at=AS_OF)
        self.assertTrue(grid.upsert_signal(second, updated_at=AS_OF))
        cell = grid.get_cell("pain", "defended")
        assert cell is not None
        self.assertEqual(cell.contributing_signal_ids, ("sig_a", "sig_b"))


class CoverageGateRuleTests(unittest.TestCase):
    def _minimal_hard_met_signals(self) -> list[dict]:
        return [
            {
                "signal_id": "sig_demand",
                "niche_id": "niche-test",
                "signal_type": "demand",
                "source": {"domain": "open.example", "tier": "open"},
                "confidence": 0.55,
            },
            {
                "signal_id": "sig_pain",
                "niche_id": "niche-test",
                "signal_type": "pain",
                "source": {"domain": "pain.example", "tier": "defended"},
                "confidence": 0.50,
            },
        ]

    def test_hard_not_met_before_deadline_waits(self) -> None:
        grid = CoverageGrid(niche_id="niche-test")
        grid.upsert_signal(self._minimal_hard_met_signals()[0], updated_at=AS_OF)
        result = evaluate_coverage_gate(
            grid,
            as_of=BEFORE_DEADLINE,
            dossier_deadline_at=DEADLINE,
        )
        self.assertFalse(result.admitted)
        self.assertFalse(result.scores_with_gaps)
        self.assertFalse(result.hard_required_met)
        self.assertEqual(result.coverage_gaps, ())

    def test_hard_not_met_at_deadline_blocks_scoring(self) -> None:
        grid = CoverageGrid(niche_id="niche-test")
        grid.upsert_signal(self._minimal_hard_met_signals()[0], updated_at=AS_OF)
        result = evaluate_coverage_gate(
            grid,
            as_of=AFTER_DEADLINE,
            dossier_deadline_at=DEADLINE,
        )
        self.assertFalse(result.admitted)
        self.assertFalse(result.scores_with_gaps)
        self.assertTrue(result.deadline_reached)
        self.assertGreater(len(result.coverage_gaps), 0)

    def test_hard_met_with_soft_gap_scores_with_gaps(self) -> None:
        grid = build_grid_from_signals(
            niche_id="niche-test",
            signals=self._minimal_hard_met_signals(),
            updated_at=AS_OF,
        )
        result = evaluate_coverage_gate(grid, as_of=AS_OF)
        self.assertFalse(result.admitted)
        self.assertTrue(result.scores_with_gaps)
        self.assertTrue(result.hard_required_met)
        self.assertEqual(
            {gap["signal_type"] for gap in result.coverage_gaps},
            {"growth", "competition", "content"},
        )

    def test_full_grid_admits(self) -> None:
        signals = self._minimal_hard_met_signals() + [
            {
                "signal_id": "sig_growth",
                "niche_id": "niche-test",
                "signal_type": "growth",
                "source": {"domain": "yt.example", "tier": "open"},
                "confidence": 0.45,
            },
            {
                "signal_id": "sig_competition",
                "niche_id": "niche-test",
                "signal_type": "competition",
                "source": {"domain": "market.example", "tier": "defended"},
                "confidence": 0.42,
            },
            {
                "signal_id": "sig_content",
                "niche_id": "niche-test",
                "signal_type": "content",
                "source": {"domain": "tiktok.example", "tier": "hostile"},
                "confidence": 0.41,
            },
        ]
        grid = build_grid_from_signals(
            niche_id="niche-test",
            signals=signals,
            updated_at=AS_OF,
        )
        result = evaluate_coverage_gate(grid, as_of=AS_OF)
        self.assertTrue(result.admitted)
        self.assertFalse(result.scores_with_gaps)
        self.assertEqual(result.coverage_gaps, ())
        self.assertFalse(find_coverage_gaps(grid))


class ImportPurityTests(unittest.TestCase):
    def test_coverage_module_is_import_pure(self) -> None:
        source = (REPO_ROOT / "signal_engine" / "coverage.py").read_text(encoding="utf-8")
        lint_import_purity(source, path=Path("signal_engine/coverage.py"))


class IntegrationCheckCoverage(unittest.TestCase):
    """N25 integration_check: gate admits, else scores-with-gaps."""

    def test_integration_check_coverage(self) -> None:
        corpus = load_fixtures()
        camping = corpus.camping_signals
        signals = camping["signals"]

        grid = build_grid_from_signals(
            niche_id=camping["niche_id"],
            signals=signals,
            updated_at=AS_OF,
        )
        self.assertTrue(hard_required_met(grid))
        admitted = evaluate_coverage_gate(grid, as_of=AS_OF)
        self.assertTrue(admitted.admitted)
        self.assertFalse(admitted.scores_with_gaps)
        self.assertEqual(admitted.coverage_gaps, ())

        partial_signals = copy.deepcopy(signals)
        partial_signals = [item for item in partial_signals if item["signal_type"] != "growth"]
        partial_grid = build_grid_from_signals(
            niche_id=camping["niche_id"],
            signals=partial_signals,
            updated_at=AS_OF,
        )
        with_gaps = evaluate_coverage_gate(partial_grid, as_of=AS_OF)
        self.assertFalse(with_gaps.admitted)
        self.assertTrue(with_gaps.scores_with_gaps)
        self.assertIn(
            "growth",
            {gap["signal_type"] for gap in with_gaps.coverage_gaps},
        )

        first = evaluate_coverage_gate(partial_grid, as_of=AS_OF)
        second = evaluate_coverage_gate(partial_grid, as_of=AS_OF)
        self.assertEqual(first, second)
        self.assertEqual(CODE_VERSION, "coverage-1.0.0")
        self.assertEqual(MIN_CELL_CONFIDENCE, 0.40)

        coverage_source = (REPO_ROOT / "signal_engine" / "coverage.py").read_text(
            encoding="utf-8",
        )
        lint_import_purity(coverage_source, path=Path("signal_engine/coverage.py"))


if __name__ == "__main__":
    unittest.main()
