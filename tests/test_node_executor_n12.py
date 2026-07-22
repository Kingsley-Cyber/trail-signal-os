"""N12 node_executor — typed I/O, packed input, verifier ceiling, no hooks."""

from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

from fixtures.load import load_fixtures
from harness.gateway import GatewayMode, LLMGateway
from harness.litellm_adapter import CassetteNotFoundError
from harness.node_executor import (
    HookInjectionError,
    IterationCeilingError,
    Law1ViolationError,
    NodeDefinition,
    NodeKind,
    PackedInputError,
    VerifierResult,
    execute_node,
    hooks_are_stripped,
    reject_hook_injection,
    schema_validate_verifier,
    validate_packed_input,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
MODELS_PATH = REPO_ROOT / "config" / "models.yaml"


def _enrich_node(*, max_iterations: int = 2, verifier=None) -> NodeDefinition:
    return NodeDefinition(
        node_id="enrich_page",
        kind=NodeKind.LLM,
        role="enrich.primary",
        input_schema="page.v1",
        output_schema="evidence.v1",
        prompt="Extract evidence from the page artifact.",
        cassette_kind="enrich",
        max_iterations=max_iterations,
        verifier=verifier or schema_validate_verifier("evidence.v1"),
    )


class HooksStrippedTests(unittest.TestCase):
    def test_executor_source_has_no_hook_machinery(self) -> None:
        self.assertTrue(hooks_are_stripped())

    def test_reject_hook_injection_in_payload(self) -> None:
        with self.assertRaises(HookInjectionError):
            reject_hook_injection({"hooks": ["ignored"]})


class PackedInputTests(unittest.TestCase):
    def setUp(self) -> None:
        corpus = load_fixtures()
        self.page = dict(corpus.page_goldens["review_page.page.v1.json"])

    def test_accepts_schema_valid_page(self) -> None:
        validate_packed_input(self.page, "page.v1")

    def test_rejects_forbidden_context_keys(self) -> None:
        polluted = {**self.page, "transcript": "full run history"}
        with self.assertRaises(PackedInputError):
            validate_packed_input(polluted, "page.v1")

    def test_rejects_hooks_in_packed_input(self) -> None:
        polluted = {**self.page, "hooks": ["inject"]}
        with self.assertRaises(HookInjectionError):
            validate_packed_input(polluted, "page.v1")


class Law1Tests(unittest.TestCase):
    def test_llm_node_rejects_opportunity_output_schema(self) -> None:
        with self.assertRaises(Law1ViolationError):
            NodeDefinition(
                node_id="bad_scorer",
                kind=NodeKind.LLM,
                role="reason.primary",
                input_schema="signal.v1",
                output_schema="opportunity.v1",
                max_iterations=1,
                verifier=schema_validate_verifier("opportunity.v1"),
            )


class ExecuteTypedNodeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.gateway = LLMGateway(models_path=MODELS_PATH, mode=GatewayMode.REPLAY)
        corpus = load_fixtures()
        self.page = dict(corpus.page_goldens["review_page.page.v1.json"])
        cassette = corpus.cassettes["enrich"][0]
        self.replay_request = dict(cassette["request"])
        self.expected_output = dict(cassette["response"]["parsed"])

    def test_executes_llm_node_with_cassette_replay(self) -> None:
        node = _enrich_node()
        result = execute_node(
            node,
            self.page,
            gateway=self.gateway,
            replay_request=self.replay_request,
        )
        self.assertEqual(result.verdict, "pass")
        self.assertEqual(result.attempts, 1)
        self.assertTrue(result.replayed)
        self.assertEqual(result.output["record_id"], self.expected_output["record_id"])
        self.assertEqual(result.output["schema_version"], "evidence.v1")

    def test_executes_deterministic_node(self) -> None:
        corpus = load_fixtures()
        expected = dict(corpus.cassettes["enrich"][0]["response"]["parsed"])

        def passthrough(page: dict) -> dict:
            return dict(expected)

        node = NodeDefinition(
            node_id="deterministic_enrich",
            kind=NodeKind.DETERMINISTIC,
            input_schema="page.v1",
            output_schema="evidence.v1",
            max_iterations=1,
            verifier=schema_validate_verifier("evidence.v1"),
        )
        result = execute_node(
            node,
            self.page,
            deterministic_fn=passthrough,
        )
        self.assertEqual(result.verdict, "pass")
        self.assertIsNone(result.replayed)
        self.assertEqual(result.output["record_id"], expected["record_id"])

    def test_generate_strips_hooks_from_replay_request(self) -> None:
        node = _enrich_node()
        polluted_request = {**self.replay_request, "hooks": ["ignored"]}
        with self.assertRaises(HookInjectionError):
            execute_node(
                node,
                self.page,
                gateway=self.gateway,
                replay_request=polluted_request,
            )


class VerifierCeilingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.gateway = LLMGateway(models_path=MODELS_PATH, mode=GatewayMode.REPLAY)
        corpus = load_fixtures()
        self.page = dict(corpus.page_goldens["review_page.page.v1.json"])
        self.replay_request = dict(corpus.cassettes["enrich"][0]["request"])

    def test_stops_at_iteration_ceiling_when_verifier_never_passes(self) -> None:
        def always_fail(_output: dict, _packed: dict) -> VerifierResult:
            return VerifierResult(passed=False, violations=("forced failure",))

        node = _enrich_node(max_iterations=2, verifier=always_fail)
        result = execute_node(
            node,
            self.page,
            gateway=self.gateway,
            replay_request=self.replay_request,
        )
        self.assertEqual(result.verdict, "ceiling")
        self.assertEqual(result.attempts, 2)
        self.assertEqual(result.violations, ("forced failure",))
        self.assertIsNotNone(result.output)

    def test_repairs_once_then_passes(self) -> None:
        calls = {"count": 0}

        def fail_once(output: dict, _packed: dict) -> VerifierResult:
            calls["count"] += 1
            if calls["count"] == 1:
                return VerifierResult(passed=False, violations=("first attempt invalid",))
            return VerifierResult(passed=True)

        node = _enrich_node(max_iterations=2, verifier=fail_once)
        result = execute_node(
            node,
            self.page,
            gateway=self.gateway,
            replay_request=self.replay_request,
        )
        self.assertEqual(result.verdict, "pass")
        self.assertEqual(result.attempts, 2)


class IntegrationCheckNodeExecutor(unittest.TestCase):
    """N12 integration_check: execute typed node; packed input only; no hook injection."""

    def test_integration_check_node_executor(self) -> None:
        self.assertTrue(hooks_are_stripped())

        corpus = load_fixtures()
        page = dict(corpus.page_goldens["review_page.page.v1.json"])
        cassette = corpus.cassettes["enrich"][0]
        request = dict(cassette["request"])

        gateway = LLMGateway(models_path=MODELS_PATH, mode=GatewayMode.REPLAY)
        node = _enrich_node(max_iterations=2)

        with patch.object(httpx.Client, "post", side_effect=AssertionError("live LLM call attempted")):
            result = execute_node(
                node,
                page,
                gateway=gateway,
                replay_request=request,
            )
            self.assertEqual(result.verdict, "pass")
            self.assertTrue(result.replayed)

            with self.assertRaises(PackedInputError):
                execute_node(
                    node,
                    {**page, "shared_context": {"prior": "transcript"}},
                    gateway=gateway,
                    replay_request=request,
                )

            with self.assertRaises(HookInjectionError):
                execute_node(
                    node,
                    {**page, "hooks": ["inject"]},
                    gateway=gateway,
                    replay_request=request,
                )

            with self.assertRaises(CassetteNotFoundError):
                execute_node(
                    node,
                    page,
                    gateway=gateway,
                    replay_request={
                        "model_id": "qwen3-4b-q4",
                        "prompt_version": "missing",
                        "page_id": "pg_missing",
                    },
                )

        failing = _enrich_node(
            max_iterations=2,
            verifier=lambda _o, _p: VerifierResult(passed=False, violations=("ceiling",)),
        )
        ceiling = execute_node(
            failing,
            page,
            gateway=gateway,
            replay_request=request,
        )
        self.assertEqual(ceiling.verdict, "ceiling")
        self.assertEqual(ceiling.attempts, 2)


if __name__ == "__main__":
    unittest.main()
