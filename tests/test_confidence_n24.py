"""N24 confidence — deterministic sample × tier × recency (LAW 1, no LLM)."""

from __future__ import annotations

import math
import unittest
from pathlib import Path

from fixtures.load import load_fixtures
from guards.static_lint import lint_import_purity
from signal_engine.classify import CASSETTE_MODEL_ID, finalize_signal_raw
from signal_engine.confidence import (
    CODE_VERSION,
    compute_signal_confidence,
    confidence_for_signal_raw,
)
from signal_engine.normalize import normalize_signal_raw, validate_signal_v1

REPO_ROOT = Path(__file__).resolve().parents[1]
CONFIG_HASH = "sha256:" + ("a" * 64)
CREATED_AT = "2026-07-21T12:00:00Z"
PAIN_COHORT = [0.20, 0.30, 0.40, 0.50, 0.61, 0.65]


class ConfidenceFormulaTests(unittest.TestCase):
    def test_thin_sample_produces_lower_confidence_than_rich_sample(self) -> None:
        thin = compute_signal_confidence(
            sample_n=1,
            signal_type="pain",
            source_tier="open",
            window_to=CREATED_AT,
            as_of=CREATED_AT,
        )
        rich = compute_signal_confidence(
            sample_n=200,
            signal_type="pain",
            source_tier="open",
            window_to=CREATED_AT,
            as_of=CREATED_AT,
        )
        self.assertLess(thin, rich)
        self.assertLessEqual(thin, 1.0)
        self.assertLessEqual(rich, 1.0)

    def test_hostile_tier_discounts_confidence(self) -> None:
        open_conf = compute_signal_confidence(
            sample_n=10,
            signal_type="content",
            source_tier="open",
            window_to=CREATED_AT,
            as_of=CREATED_AT,
        )
        hostile_conf = compute_signal_confidence(
            sample_n=10,
            signal_type="content",
            source_tier="hostile",
            window_to=CREATED_AT,
            as_of=CREATED_AT,
        )
        self.assertLess(hostile_conf, open_conf)

    def test_stale_window_reduces_recency_term(self) -> None:
        fresh = compute_signal_confidence(
            sample_n=10,
            signal_type="growth",
            source_tier="open",
            window_to=CREATED_AT,
            as_of=CREATED_AT,
        )
        stale = compute_signal_confidence(
            sample_n=10,
            signal_type="growth",
            source_tier="open",
            window_to="2026-01-01T00:00:00Z",
            as_of=CREATED_AT,
        )
        self.assertLess(stale, fresh)

    def test_result_is_clamped_to_unit_interval(self) -> None:
        confidence = compute_signal_confidence(
            sample_n=10_000,
            signal_type="pain",
            source_tier="open",
            window_to=CREATED_AT,
            as_of=CREATED_AT,
        )
        self.assertGreaterEqual(confidence, 0.0)
        self.assertLessEqual(confidence, 1.0)


class NormalizeConfidenceIntegrationTests(unittest.TestCase):
    def test_normalize_sets_confidence_from_signal_raw(self) -> None:
        corpus = load_fixtures()
        signal_raw = dict(corpus.cassettes["classify"][0]["response"]["parsed"])
        expected = confidence_for_signal_raw(signal_raw, as_of=CREATED_AT)
        signal = normalize_signal_raw(
            signal_raw,
            cohort_raw_values=PAIN_COHORT,
            config_hash=CONFIG_HASH,
            created_at=CREATED_AT,
        )
        validate_signal_v1(signal)
        self.assertAlmostEqual(signal["confidence"], expected)
        self.assertGreater(signal["confidence"], 0.0)


class ImportPurityTests(unittest.TestCase):
    def test_confidence_module_is_import_pure(self) -> None:
        source = (REPO_ROOT / "signal_engine" / "confidence.py").read_text(encoding="utf-8")
        lint_import_purity(source, path=Path("signal_engine/confidence.py"))


class IntegrationCheckConfidence(unittest.TestCase):
    """N24 integration_check: deterministic confidence."""

    def test_integration_check_confidence(self) -> None:
        corpus = load_fixtures()
        cassette = corpus.cassettes["classify"][0]
        evidence = dict(corpus.cassettes["enrich"][0]["response"]["parsed"])
        signal_raw = finalize_signal_raw(
            dict(cassette["response"]["parsed"]),
            evidence,
            model_id=CASSETTE_MODEL_ID,
            classify_task_id="tsk_gate4_confidence",
        )

        first = confidence_for_signal_raw(signal_raw, as_of=CREATED_AT)
        second = confidence_for_signal_raw(signal_raw, as_of=CREATED_AT)
        self.assertAlmostEqual(first, second)

        signal = normalize_signal_raw(
            signal_raw,
            cohort_raw_values=PAIN_COHORT,
            config_hash=CONFIG_HASH,
            created_at=CREATED_AT,
        )
        validate_signal_v1(signal)
        self.assertAlmostEqual(signal["confidence"], first)
        self.assertEqual(CODE_VERSION, "confidence-1.0.0")

        confidence_source = (REPO_ROOT / "signal_engine" / "confidence.py").read_text(
            encoding="utf-8",
        )
        lint_import_purity(confidence_source, path=Path("signal_engine/confidence.py"))


if __name__ == "__main__":
    unittest.main()
