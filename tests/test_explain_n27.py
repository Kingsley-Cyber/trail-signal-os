"""N27 explain — narrates precomputed scores; never computes them (LAW 1, g5)."""

from __future__ import annotations

import copy
import json
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

from fixtures.load import load_fixtures
from guards.exceptions import GuardViolation
from guards.schema_guards import guard5_reject_llm_score_provenance
from harness.gateway import GatewayMode, LLMGateway
from harness.litellm_adapter import CassetteNotFoundError
from signal_engine.explain import (
    CASSETTE_KIND,
    CASSETTE_MODEL_ID,
    PROMPT_VERSION,
    ExplainError,
    assert_law1_explain_output,
    attach_explanation,
    build_evidence_store,
    build_replay_request,
    explain_camping_fixture,
    explain_opportunity,
    finalize_explanation,
    load_explain_prompt,
    run_explain_task,
    validate_explanation_output,
)
from signal_engine.score import score_camping_fixture, validate_opportunity_v1

REPO_ROOT = Path(__file__).resolve().parents[1]
MODELS_PATH = REPO_ROOT / "config" / "models.yaml"
POISON_G05 = (
    REPO_ROOT / "tests" / "fault_injection" / "poison" / "g05_opportunity_model_id.json"
)


def _camping_evidence_store() -> list[dict]:
    corpus = load_fixtures()
    items = [dict(corpus.cassettes["enrich"][0]["response"]["parsed"])]
    for record_id in ("ev_camping_pain_1108", "ev_camping_pain_1155"):
        items.append(
            {
                "record_id": record_id,
                "observation": "Additional pain-theme evidence for camping fan complaints.",
                "schema_version": "evidence.v1",
            }
        )
    return items


class PromptTests(unittest.TestCase):
    def test_prompt_loads_and_forbids_scoring(self) -> None:
        prompt = load_explain_prompt()
        self.assertIn("LAW 1", prompt)
        self.assertIn("Do **not** output `score`", prompt)


class ReplayRequestTests(unittest.TestCase):
    def test_build_replay_request_matches_explain_cassette(self) -> None:
        corpus = load_fixtures()
        opportunity = score_camping_fixture()
        request = build_replay_request(opportunity)
        cassette_request = dict(corpus.cassettes["explain"][0]["request"])
        self.assertEqual(request, cassette_request)


class Law1ValidationTests(unittest.TestCase):
    def test_rejects_scoring_fields_in_explanation_output(self) -> None:
        with self.assertRaises(ExplainError) as ctx:
            validate_explanation_output({"text": "ok", "score": 0.9})
        self.assertIn("score", str(ctx.exception))

    def test_rejects_unknown_cited_record_id(self) -> None:
        with self.assertRaises(ExplainError) as ctx:
            validate_explanation_output(
                {"text": "grounded", "cited_record_ids": ["ev_missing"]},
                evidence_store={},
            )
        self.assertIn("ev_missing", str(ctx.exception))

    def test_assert_law1_explain_output_accepts_prose_only(self) -> None:
        assert_law1_explain_output(
            {
                "text": "Strong pain density with moderate confidence.",
                "cited_record_ids": [],
            }
        )


class AttachExplanationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.opportunity = score_camping_fixture()
        self.explanation = {
            "text": "Strong complaint density meets solid demand.",
            "cited_record_ids": ["ev_camping_pain_1042"],
            "provenance": {
                "model_id": CASSETTE_MODEL_ID,
                "prompt_version": PROMPT_VERSION,
            },
        }

    def test_attach_explanation_preserves_score_fields(self) -> None:
        updated = attach_explanation(self.opportunity, self.explanation)
        self.assertEqual(updated["score"], self.opportunity["score"])
        self.assertEqual(updated["subscores"], self.opportunity["subscores"])
        self.assertEqual(updated["confidence"], self.opportunity["confidence"])
        self.assertEqual(updated["provenance"], self.opportunity["provenance"])
        self.assertEqual(updated["explanation"]["text"], self.explanation["text"])
        validate_opportunity_v1(updated)
        guard5_reject_llm_score_provenance(updated)

    def test_attach_explanation_rejects_score_provenance_poison(self) -> None:
        poison = copy.deepcopy(self.opportunity)
        poison["provenance"] = dict(poison["provenance"])
        poison["provenance"]["model_id"] = CASSETTE_MODEL_ID
        with self.assertRaises(GuardViolation):
            attach_explanation(poison, self.explanation)


class CassetteReplayTests(unittest.TestCase):
    def setUp(self) -> None:
        self.gateway = LLMGateway(models_path=MODELS_PATH, mode=GatewayMode.REPLAY)
        corpus = load_fixtures()
        self.opportunity = score_camping_fixture()
        self.replay_request = dict(corpus.cassettes["explain"][0]["request"])
        self.evidence_items = _camping_evidence_store()

    def test_replays_explain_cassette(self) -> None:
        execution = explain_opportunity(
            self.opportunity,
            self.evidence_items,
            gateway=self.gateway,
            replay_request=self.replay_request,
        )
        self.assertEqual(execution.verdict, "pass")
        self.assertTrue(execution.replayed)
        assert execution.output is not None
        assert_law1_explain_output(execution.output)
        cassette = load_fixtures().cassettes["explain"][0]["response"]["parsed"]
        self.assertEqual(execution.output["text"], cassette["text"])
        self.assertEqual(execution.output["cited_record_ids"], cassette["cited_record_ids"])

    def test_missing_cassette_fails_without_live_call(self) -> None:
        with patch.object(httpx.Client, "post", side_effect=AssertionError("live LLM call attempted")):
            with self.assertRaises(CassetteNotFoundError):
                explain_opportunity(
                    self.opportunity,
                    self.evidence_items,
                    gateway=self.gateway,
                    replay_request={
                        "role": "enrich.primary",
                        "model_id": CASSETTE_MODEL_ID,
                        "prompt_version": PROMPT_VERSION,
                        "opportunity_id": "opp_missing",
                    },
                )

    def test_run_explain_task_attaches_explanation_without_score_mutation(self) -> None:
        result = run_explain_task(
            self.opportunity,
            self.evidence_items,
            gateway=self.gateway,
            replay_request=self.replay_request,
        )
        self.assertTrue(result.replayed)
        self.assertEqual(result.opportunity["score"], self.opportunity["score"])
        self.assertEqual(result.opportunity["subscores"], self.opportunity["subscores"])
        self.assertEqual(result.opportunity["confidence"], self.opportunity["confidence"])
        self.assertIn("explanation", result.opportunity)
        guard5_reject_llm_score_provenance(result.opportunity)

    def test_explain_camping_fixture_end_to_end(self) -> None:
        result = explain_camping_fixture(gateway=self.gateway)
        self.assertEqual(result.opportunity["score"], 0.72)
        self.assertEqual(result.opportunity["confidence"], 0.65)
        self.assertIn("battery life", result.explanation["text"].lower())


class FinalizeExplanationTests(unittest.TestCase):
    def test_finalize_adds_provenance(self) -> None:
        store = build_evidence_store(_camping_evidence_store())
        explanation = finalize_explanation(
            {
                "text": "Pain themes are well supported.",
                "cited_record_ids": ["ev_camping_pain_1042"],
            },
            model_id=CASSETTE_MODEL_ID,
            evidence_store=store,
        )
        self.assertEqual(explanation["provenance"]["prompt_version"], PROMPT_VERSION)
        self.assertEqual(explanation["provenance"]["model_id"], CASSETTE_MODEL_ID)


class IntegrationCheckExplain(unittest.TestCase):
    """N27 integration_check: explains, never scores (g5)."""

    def test_integration_check_explain(self) -> None:
        corpus = load_fixtures()
        gateway = LLMGateway(models_path=MODELS_PATH, mode=GatewayMode.REPLAY)
        opportunity = score_camping_fixture()
        cassette = corpus.cassettes["explain"][0]
        request = dict(cassette["request"])
        expected = dict(cassette["response"]["parsed"])

        execution = explain_opportunity(
            opportunity,
            _camping_evidence_store(),
            gateway=gateway,
            replay_request=request,
        )
        self.assertEqual(execution.verdict, "pass")
        self.assertTrue(execution.replayed)
        assert execution.output is not None
        assert_law1_explain_output(execution.output)
        self.assertNotIn("score", execution.output)
        self.assertNotIn("subscores", execution.output)
        self.assertEqual(execution.output["text"], expected["text"])

        explanation = finalize_explanation(
            execution.output,
            model_id=CASSETTE_MODEL_ID,
            evidence_store=build_evidence_store(_camping_evidence_store()),
        )
        updated = attach_explanation(opportunity, explanation)
        self.assertEqual(updated["score"], 0.72)
        self.assertEqual(updated["confidence"], 0.65)
        guard5_reject_llm_score_provenance(updated)

        poison = json.loads(POISON_G05.read_text(encoding="utf-8"))
        with self.assertRaises(GuardViolation):
            guard5_reject_llm_score_provenance(poison)

        with patch.object(httpx.Client, "post", side_effect=AssertionError("live LLM call attempted")):
            with self.assertRaises(CassetteNotFoundError):
                explain_opportunity(
                    opportunity,
                    _camping_evidence_store(),
                    gateway=gateway,
                    replay_request={
                        "role": "enrich.primary",
                        "model_id": CASSETTE_MODEL_ID,
                        "prompt_version": PROMPT_VERSION,
                        "opportunity_id": "opp_missing",
                    },
                )


if __name__ == "__main__":
    unittest.main()
