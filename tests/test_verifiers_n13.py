"""N13 verifiers — catalog from doc 07 §4."""

from __future__ import annotations

import unittest
from dataclasses import dataclass
from typing import Any
from unittest.mock import MagicMock

from graph.verifiers import (
    CATALOG_VERIFIER_NAMES,
    VERIFIER_CATALOG,
    claim_grounding,
    decision_valid,
    get_verifier_factory,
    list_verifiers,
    novelty_floor,
    plan_validates,
    quorum_met,
    sample_judge,
    schema_validate,
)
from graph.verifiers.sample_judge import _in_sample

CONFIG_HASH = "sha256:" + ("a" * 64)
CONTENT_HASH = "sha256:" + ("b" * 64)
CREATED_AT = "2026-07-21T12:00:00Z"

BUDGET = {
    "max_queries": 30,
    "max_fetched_urls": 2000,
    "per_domain_urls": 300,
    "browser_pages": 60,
    "media_items": 150,
    "max_bytes": 5368709120,
    "deadline_minutes": 45,
    "max_attempts": 4,
    "llm_budget": {"max_calls": 500, "max_tokens": 2000000, "max_usd": 0},
    "schema_version": "budget.v1",
}

EVIDENCE = {
    "record_id": "ev_01JTEST",
    "source": {"url": "https://example.com/article", "domain": "example.com"},
    "evidence_type": "behavior",
    "polarity": "supporting",
    "observation": "Users report repeated workaround behavior.",
    "retrieved_at": "2026-07-21",
    "independence_group": "example.com:article",
    "confidence": "medium",
    "derived_from": ["pg_01JTEST"],
    "content_hash": CONTENT_HASH,
    "extraction": {
        "model_id": "qwen3-4b-q4",
        "prompt_version": "enrich_page-2026.07.21",
    },
    "provenance": {
        "model_id": "qwen3-4b-q4",
        "prompt_version": "enrich_page-2026.07.21",
        "schema_version": "evidence.v1",
        "config_hash": CONFIG_HASH,
        "created_at": CREATED_AT,
    },
    "schema_version": "evidence.v1",
}

DECISION = {
    "decision_id": "dec_01JTEST",
    "action": "expand",
    "args": {"niche_id": "camping", "max_queries": 10},
    "rationale": {
        "text": "Coverage gaps remain in pain signals.",
        "provenance": {
            "model_id": "qwen3-4b-q4",
            "prompt_version": "planner-2026.07.21",
        },
    },
    "cited_manifest_hash": CONFIG_HASH,
    "derived_from": ["opp_01JTEST"],
    "provenance": {
        "code_version": "decide-1.0.0",
        "schema_version": "decision.v1",
        "config_hash": CONFIG_HASH,
        "created_at": CREATED_AT,
    },
    "schema_version": "decision.v1",
}


class CatalogTests(unittest.TestCase):
    def test_catalog_matches_doc_07_section_4(self) -> None:
        expected = (
            "schema_validate",
            "plan_validates",
            "claim_grounding",
            "quorum_met",
            "novelty_floor",
            "decision_valid",
            "sample_judge",
        )
        self.assertEqual(CATALOG_VERIFIER_NAMES, expected)
        self.assertEqual(list_verifiers(), expected)
        self.assertEqual(len(VERIFIER_CATALOG), 7)

    def test_get_verifier_factory_resolves_each_catalog_entry(self) -> None:
        for name in CATALOG_VERIFIER_NAMES:
            factory = get_verifier_factory(name)
            self.assertTrue(callable(factory), name)


class SchemaValidateTests(unittest.TestCase):
    def test_passes_valid_evidence(self) -> None:
        verify = schema_validate("evidence.v1")
        result = verify(EVIDENCE, {})
        self.assertTrue(result.passed)
        self.assertEqual(result.violations, ())

    def test_fails_invalid_evidence(self) -> None:
        verify = schema_validate("evidence.v1")
        bad = {**EVIDENCE, "record_id": "not-an-ev-id"}
        result = verify(bad, {})
        self.assertFalse(result.passed)
        self.assertTrue(result.violations)


class PlanValidatesTests(unittest.TestCase):
    def test_passes_plan_within_budget_and_allowlist(self) -> None:
        verify = plan_validates()
        plan = {
            "plan_id": "pln_01JTEST",
            "queries": [
                {"query_spec_id": "qsp_01", "text": "camping fan workaround", "platform": "web"},
            ],
            "schema_version": "plan.v1",
        }
        result = verify(plan, {"budget": BUDGET})
        self.assertTrue(result.passed)

    def test_rejects_query_over_budget(self) -> None:
        verify = plan_validates()
        plan = {
            "plan_id": "pln_01JTEST",
            "queries": [
                {"query_spec_id": f"qsp_{index}", "text": f"query {index}", "platform": "web"}
                for index in range(BUDGET["max_queries"] + 1)
            ],
        }
        result = verify(plan, {"budget": BUDGET})
        self.assertFalse(result.passed)
        self.assertTrue(any("max_queries" in item for item in result.violations))

    def test_rejects_disallowed_platform(self) -> None:
        verify = plan_validates(platform_allowlist=frozenset({"web"}))
        plan = {
            "plan_id": "pln_01JTEST",
            "queries": [{"query_spec_id": "qsp_01", "text": "reddit thread", "platform": "reddit"}],
        }
        result = verify(plan, {"budget": BUDGET})
        self.assertFalse(result.passed)
        self.assertTrue(any("allowlist" in item for item in result.violations))


class ClaimGroundingTests(unittest.TestCase):
    def test_passes_grounded_claim(self) -> None:
        grounded = {**EVIDENCE, "metric_value": 42}
        verify = claim_grounding()
        synthesis = {
            "synthesis_id": "syn_01JTEST",
            "claims": [
                {
                    "claim_id": "clm_01",
                    "text": "Users mention 42 workaround mentions.",
                    "cited_record_ids": ["ev_01JTEST"],
                    "numbers": [{"value": 42, "record_id": "ev_01JTEST"}],
                }
            ],
        }
        result = verify(synthesis, {"evidence_store": {"ev_01JTEST": grounded}})
        self.assertTrue(result.passed)

    def test_rejects_unknown_record_id(self) -> None:
        verify = claim_grounding()
        synthesis = {
            "synthesis_id": "syn_01JTEST",
            "claims": [
                {
                    "claim_id": "clm_01",
                    "text": "Unsupported claim.",
                    "cited_record_ids": ["ev_MISSING"],
                }
            ],
        }
        result = verify(synthesis, {"evidence_store": {"ev_01JTEST": EVIDENCE}})
        self.assertFalse(result.passed)
        self.assertTrue(any("unknown record_id" in item for item in result.violations))

    def test_rejects_number_mismatch(self) -> None:
        grounded = {**EVIDENCE, "metric_value": 42}
        verify = claim_grounding()
        synthesis = {
            "synthesis_id": "syn_01JTEST",
            "claims": [
                {
                    "claim_id": "clm_01",
                    "text": "Users mention 99 workaround mentions.",
                    "cited_record_ids": ["ev_01JTEST"],
                    "numbers": [{"value": 99, "record_id": "ev_01JTEST"}],
                }
            ],
        }
        result = verify(synthesis, {"evidence_store": {"ev_01JTEST": grounded}})
        self.assertFalse(result.passed)
        self.assertTrue(any("!=" in item for item in result.violations))


class QuorumMetTests(unittest.TestCase):
    def test_passes_when_counts_meet_thresholds(self) -> None:
        verify = quorum_met()
        result = verify(
            {"quorum_counts": {"validated_records": 105, "domains": 12}},
            {},
        )
        self.assertTrue(result.passed)

    def test_fails_when_records_below_threshold(self) -> None:
        verify = quorum_met(min_records=100, min_domains=10)
        result = verify(
            {"quorum_counts": {"validated_records": 50, "domains": 12}},
            {},
        )
        self.assertFalse(result.passed)
        self.assertTrue(any("validated_records" in item for item in result.violations))

    def test_reads_thresholds_from_packed_input(self) -> None:
        verify = quorum_met()
        result = verify(
            {},
            {
                "quorum": {
                    "validated_records": 120,
                    "domains": 11,
                    "min_records": 100,
                    "min_domains": 10,
                }
            },
        )
        self.assertTrue(result.passed)


class NoveltyFloorTests(unittest.TestCase):
    def test_passes_when_novelty_meets_floor(self) -> None:
        verify = novelty_floor(floor_pct=0.05)
        result = verify(
            {"expand_counts": {"entities": 110, "claims": 220, "domains": 9}},
            {"novelty": {"baseline": {"entities": 100, "claims": 200, "domains": 8}}},
        )
        self.assertTrue(result.passed)

    def test_fails_when_novelty_below_floor(self) -> None:
        verify = novelty_floor(floor_pct=0.05)
        result = verify(
            {"expand_counts": {"entities": 101, "claims": 201, "domains": 8}},
            {"novelty": {"baseline": {"entities": 100, "claims": 200, "domains": 8}}},
        )
        self.assertFalse(result.passed)
        self.assertTrue(any("below floor" in item for item in result.violations))


class DecisionValidTests(unittest.TestCase):
    def test_passes_valid_decision_with_manifest_hash(self) -> None:
        verify = decision_valid()
        result = verify(DECISION, {"manifest_hash": CONFIG_HASH})
        self.assertTrue(result.passed)

    def test_rejects_manifest_hash_mismatch(self) -> None:
        verify = decision_valid()
        result = verify(DECISION, {"manifest_hash": "sha256:" + ("d" * 64)})
        self.assertFalse(result.passed)
        self.assertTrue(any("manifest_hash" in item for item in result.violations))

    def test_rejects_missing_action_args(self) -> None:
        verify = decision_valid()
        bad = {
            **DECISION,
            "action": "synthesize",
            "args": {"niche_id": "camping"},
        }
        result = verify(bad, {"manifest_hash": CONFIG_HASH})
        self.assertFalse(result.passed)
        self.assertTrue(any("synthesis_id" in item for item in result.violations))


@dataclass
class _FakeCompletion:
    parsed: dict[str, Any] | None
    text: str


class SampleJudgeTests(unittest.TestCase):
    def test_skips_out_of_sample_records(self) -> None:
        verify = sample_judge(sample_rate_pct=0, gateway=MagicMock())
        result = verify(EVIDENCE, {})
        self.assertTrue(result.passed)

    def test_calls_judge_for_in_sample_records(self) -> None:
        gateway = MagicMock()
        gateway.generate.return_value = _FakeCompletion(
            parsed={"pass": False, "violations": ["unsupported inference"]},
            text='{"pass": false, "violations": ["unsupported inference"]}',
        )
        verify = sample_judge(sample_rate_pct=100, gateway=gateway)
        result = verify(EVIDENCE, {})
        self.assertFalse(result.passed)
        gateway.generate.assert_called_once()
        self.assertIn("unsupported inference", result.violations[0])

    def test_in_sample_selection_is_deterministic(self) -> None:
        first = _in_sample("ev_deterministic_sample", 2)
        second = _in_sample("ev_deterministic_sample", 2)
        self.assertEqual(first, second)


class IntegrationCheckVerifiers(unittest.TestCase):
    """Offline integration check for N13 verifier-catalog."""

    _TEST_CLASSES = (
        CatalogTests,
        SchemaValidateTests,
        PlanValidatesTests,
        ClaimGroundingTests,
        QuorumMetTests,
        NoveltyFloorTests,
        DecisionValidTests,
        SampleJudgeTests,
    )

    def test_each_catalog_verifier_has_unit_tests(self) -> None:
        covered: set[str] = set()
        for test_cls in self._TEST_CLASSES:
            if test_cls is CatalogTests:
                continue
            prefix = test_cls.__name__.removesuffix("Tests")
            key = {
                "SchemaValidate": "schema_validate",
                "PlanValidates": "plan_validates",
                "ClaimGrounding": "claim_grounding",
                "QuorumMet": "quorum_met",
                "NoveltyFloor": "novelty_floor",
                "DecisionValid": "decision_valid",
                "SampleJudge": "sample_judge",
            }[prefix]
            covered.add(key)
        self.assertEqual(covered, set(CATALOG_VERIFIER_NAMES))


if __name__ == "__main__":
    unittest.main()
