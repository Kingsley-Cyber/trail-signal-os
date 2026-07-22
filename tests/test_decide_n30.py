"""N30 decide — constraint-fit re-rank (det) + rationale (llm, split)."""

from __future__ import annotations

import copy
import json
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

from fixtures.load import load_fixtures
from guards.schema_guards import guard5_reject_llm_score_provenance
from harness.gateway import GatewayMode, LLMGateway
from harness.litellm_adapter import (
    CassetteNotFoundError,
    LiteLLMAdapter,
    TransportMode,
    canonical_request_hash,
    load_models_config,
)
from signal_engine.decide import (
    CASSETTE_KIND,
    CASSETTE_MODEL_ID,
    CODE_VERSION,
    PROMPT_VERSION,
    DecideError,
    assert_law1_decide_rationale_output,
    attach_rationale,
    build_decision_v1,
    build_replay_request,
    compute_constraint_fit,
    decide_camping_fixture,
    decide_rationale,
    decide_split_verifier,
    finalize_rationale,
    load_constraints,
    rerank_opportunities,
    run_decide_task,
    select_decision_action,
    validate_decision_v1,
    validate_rationale_output,
)
from signal_engine.score import score_camping_fixture, validate_opportunity_v1

REPO_ROOT = Path(__file__).resolve().parents[1]
MODELS_PATH = REPO_ROOT / "config" / "models.yaml"
CONFIG_HASH = "sha256:" + ("a" * 64)
CREATED_AT = "2026-07-21T12:00:00Z"

CAMPING_PROFILE = {
    "nc-001": {
        "margin_potential": 4,
        "shipping_fit": 5,
        "community_reachability": 4,
    }
}

DECIDE_CASSETTE = {
    "cassette_kind": CASSETTE_KIND,
    "input_hash": "sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
    "recorded_at": CREATED_AT,
    "request": {
        "role": "reason.primary",
        "model_id": CASSETTE_MODEL_ID,
        "prompt_version": PROMPT_VERSION,
        "decision_id": "dec_placeholder",
    },
    "response": {
        "text": "Constraint-fit passes for the portable fan; controlled experiment band supports synthesis.",
        "parsed": {
            "text": (
                "Portable camping fan ranks first on constraint-fit (strong shipping profile and "
                "healthy margin potential) while the precomputed opportunity score remains in the "
                "controlled-experiment band — synthesis is the deterministic next step."
            ),
            "cited_record_ids": [],
        },
    },
}


def _gateway_with_decide_cassette(
    *,
    decision_id: str,
) -> LLMGateway:
    cassette = copy.deepcopy(DECIDE_CASSETTE)
    cassette["request"]["decision_id"] = decision_id
    cassette["input_hash"] = canonical_request_hash(CASSETTE_KIND, cassette["request"])
    adapter = LiteLLMAdapter(
        load_models_config(MODELS_PATH),
        mode=TransportMode.REPLAY,
    )
    request_key = (CASSETTE_KIND, tuple(sorted(cassette["request"].items())))
    adapter._cassettes._by_request[request_key] = cassette
    adapter._cassettes._by_hash[(CASSETTE_KIND, cassette["input_hash"])] = cassette
    return LLMGateway(adapter=adapter)


def _clone_opportunity(
    base: dict,
    *,
    opportunity_id: str,
    score: float,
    candidate_id: str = "nc-001",
    title: str,
) -> dict:
    payload = copy.deepcopy(base)
    payload["opportunity_id"] = opportunity_id
    payload["score"] = score
    payload["candidate"] = {
        **dict(payload["candidate"]),
        "candidate_id": candidate_id,
        "title": title,
    }
    validate_opportunity_v1(payload)
    return payload


class ConstraintsTests(unittest.TestCase):
    def test_load_constraints_includes_reranker(self) -> None:
        constraints = load_constraints()
        rerank = constraints["constraint_rerank"]
        self.assertEqual(rerank["version"], "cr-2026.07.21")
        self.assertAlmostEqual(
            sum(float(rerank["axis_weights"][key]) for key in rerank["axis_weights"]),
            1.0,
        )


class ConstraintFitTests(unittest.TestCase):
    def test_camping_fixture_passes_constraint_fit(self) -> None:
        opportunity = score_camping_fixture()
        fit = compute_constraint_fit(
            opportunity,
            candidate_profiles=CAMPING_PROFILE,
        )
        self.assertTrue(fit.passes)
        self.assertGreaterEqual(fit.fit_score, 0.55)


class RerankTests(unittest.TestCase):
    def test_rerank_prefers_constraint_fit_over_raw_score(self) -> None:
        base = score_camping_fixture()
        high_score = _clone_opportunity(
            base,
            opportunity_id="opp_high_score",
            score=0.80,
            candidate_id="nc-002",
            title="Bulky high-score candidate",
        )
        better_fit = _clone_opportunity(
            base,
            opportunity_id="opp_better_fit",
            score=0.72,
            candidate_id="nc-001",
            title="Portable camping fan",
        )
        profiles = {
            **CAMPING_PROFILE,
            "nc-002": {
                "margin_potential": 1,
                "shipping_fit": 1,
                "community_reachability": 1,
            },
        }
        rerank = rerank_opportunities(
            [high_score, better_fit],
            candidate_profiles=profiles,
        )
        self.assertEqual(rerank.ranked[0].opportunity["opportunity_id"], "opp_better_fit")
        self.assertEqual(rerank.ranked[0].constraint_rank, 1)
        self.assertEqual(rerank.ranked[1].constraint_rank, 2)


class ActionSelectionTests(unittest.TestCase):
    def test_camping_fixture_selects_synthesize(self) -> None:
        opportunity = score_camping_fixture()
        rerank = rerank_opportunities([opportunity], candidate_profiles=CAMPING_PROFILE)
        action, args, selected = select_decision_action(rerank)
        self.assertEqual(action, "synthesize")
        self.assertIn("synthesis_id", args)
        self.assertEqual(selected.constraint_rank, 1)


class Law1ValidationTests(unittest.TestCase):
    def test_rejects_action_in_rationale_output(self) -> None:
        with self.assertRaises(DecideError) as ctx:
            validate_rationale_output(
                {"text": "ok", "action": "synthesize"},
            )
        self.assertIn("action", str(ctx.exception))

    def test_decide_split_verifier_blocks_rank_fields(self) -> None:
        verify = decide_split_verifier()
        result = verify({"text": "ok", "constraint_rank": 1}, {})
        self.assertFalse(result.passed)


class CassetteReplayTests(unittest.TestCase):
    def setUp(self) -> None:
        self.opportunity = score_camping_fixture()
        self.rerank = rerank_opportunities(
            [self.opportunity],
            candidate_profiles=CAMPING_PROFILE,
        )
        self.action, self.args, self.selected = select_decision_action(self.rerank)
        self.decision = build_decision_v1(
            rerank=self.rerank,
            action=self.action,
            args=self.args,
            selected=self.selected,
            config_hash=CONFIG_HASH,
            created_at=CREATED_AT,
        )
        self.gateway = _gateway_with_decide_cassette(decision_id=self.decision["decision_id"])

    def test_replays_decide_cassette_without_live_call(self) -> None:
        execution = decide_rationale(
            self.decision,
            self.rerank,
            action=self.action,
            args=self.args,
            selected=self.selected,
            gateway=self.gateway,
            replay_request=build_replay_request(self.decision),
        )
        self.assertEqual(execution.verdict, "pass")
        self.assertTrue(execution.replayed)
        assert execution.output is not None
        assert_law1_decide_rationale_output(execution.output)

    def test_missing_cassette_fails_without_live_call(self) -> None:
        gateway = LLMGateway(models_path=MODELS_PATH, mode=GatewayMode.REPLAY)
        with patch.object(httpx.Client, "post", side_effect=AssertionError("live LLM call attempted")):
            with self.assertRaises(CassetteNotFoundError):
                decide_rationale(
                    self.decision,
                    self.rerank,
                    action=self.action,
                    args=self.args,
                    selected=self.selected,
                    gateway=gateway,
                    replay_request={
                        "role": "reason.primary",
                        "model_id": CASSETTE_MODEL_ID,
                        "prompt_version": PROMPT_VERSION,
                        "decision_id": "dec_missing",
                    },
                )


class RunDecideTaskTests(unittest.TestCase):
    def test_run_decide_task_keeps_deterministic_action(self) -> None:
        opportunity = score_camping_fixture()
        gateway = _gateway_with_decide_cassette(decision_id="dec_pending")
        rerank = rerank_opportunities([opportunity], candidate_profiles=CAMPING_PROFILE)
        action, args, selected = select_decision_action(rerank)
        decision = build_decision_v1(
            rerank=rerank,
            action=action,
            args=args,
            selected=selected,
            config_hash=CONFIG_HASH,
            created_at=CREATED_AT,
        )
        gateway = _gateway_with_decide_cassette(decision_id=decision["decision_id"])
        result = run_decide_task(
            [opportunity],
            candidate_profiles=CAMPING_PROFILE,
            config_hash=CONFIG_HASH,
            created_at=CREATED_AT,
            gateway=gateway,
            replay_request=build_replay_request(decision),
        )
        self.assertEqual(result.decision["action"], "synthesize")
        self.assertEqual(result.decision["constraint_rank"], 1)
        self.assertNotIn("score", result.decision["rationale"])
        validate_decision_v1(result.decision)
        guard5_reject_llm_score_provenance(opportunity)


class IntegrationCheckDecide(unittest.TestCase):
    """N30 integration_check: constraint-fit re-rank (det) + rationale (llm, split)."""

    def test_integration_check_decide(self) -> None:
        opportunity = score_camping_fixture()
        rerank = rerank_opportunities([opportunity], candidate_profiles=CAMPING_PROFILE)
        self.assertEqual(rerank.ranked[0].opportunity["opportunity_id"], "opp_camping_fixture")

        high_score = _clone_opportunity(
            opportunity,
            opportunity_id="opp_heavy",
            score=0.80,
            candidate_id="nc-002",
            title="Heavy candidate",
        )
        profiles = {
            **CAMPING_PROFILE,
            "nc-002": {
                "margin_potential": 1,
                "shipping_fit": 1,
                "community_reachability": 1,
            },
        }
        multi = rerank_opportunities([high_score, opportunity], candidate_profiles=profiles)
        self.assertEqual(multi.ranked[0].opportunity["opportunity_id"], "opp_camping_fixture")

        action, args, selected = select_decision_action(rerank)
        decision_shell = build_decision_v1(
            rerank=rerank,
            action=action,
            args=args,
            selected=selected,
            config_hash=CONFIG_HASH,
            created_at=CREATED_AT,
        )
        gateway = _gateway_with_decide_cassette(decision_id=decision_shell["decision_id"])
        result = run_decide_task(
            [opportunity],
            candidate_profiles=CAMPING_PROFILE,
            config_hash=CONFIG_HASH,
            created_at=CREATED_AT,
            gateway=gateway,
            replay_request=build_replay_request(decision_shell),
        )
        self.assertTrue(result.replayed)
        self.assertEqual(result.decision["action"], "synthesize")
        self.assertEqual(result.decision["constraint_rank"], 1)
        assert_law1_decide_rationale_output(
            {
                "text": result.decision["rationale"]["text"],
                "cited_record_ids": result.decision["rationale"].get("cited_record_ids", []),
            }
        )
        validate_decision_v1(result.decision)
        guard5_reject_llm_score_provenance(opportunity)

        verify = decide_split_verifier()
        poison = verify(
            {
                "text": "Attempted override",
                "action": "stop",
            },
            {},
        )
        self.assertFalse(poison.passed)

        with patch.object(httpx.Client, "post", side_effect=AssertionError("live LLM call attempted")):
            with self.assertRaises(CassetteNotFoundError):
                decide_camping_fixture(
                    gateway=LLMGateway(models_path=MODELS_PATH, mode=GatewayMode.REPLAY),
                    replay_request={
                        "role": "reason.primary",
                        "model_id": CASSETTE_MODEL_ID,
                        "prompt_version": PROMPT_VERSION,
                        "decision_id": "dec_missing",
                    },
                )


if __name__ == "__main__":
    unittest.main()
