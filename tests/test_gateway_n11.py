"""N11 gateway — roles from models.yaml, hooks stripped, cassette replay."""

from __future__ import annotations

import inspect
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx
import yaml

from fixtures.load import load_fixtures
from harness.gateway import (
    GatewayMode,
    LLMGateway,
    complete,
    generate_signature_has_no_hooks,
    hooks_are_stripped,
    load_models_config,
)
from harness.litellm_adapter import CassetteNotFoundError, LiteLLMAdapter, TransportMode

REPO_ROOT = Path(__file__).resolve().parents[1]
MODELS_PATH = REPO_ROOT / "config" / "models.yaml"


class ModelsConfigTests(unittest.TestCase):
    def test_roles_loaded_from_models_yaml(self) -> None:
        config = load_models_config(MODELS_PATH)
        self.assertEqual(config.version, "m-2026.07.21")
        self.assertIn("enrich.primary", config.roles)
        self.assertIn("reason.primary", config.roles)
        self.assertIn("embed.primary", config.roles)

        enrich = config.resolve("enrich.primary")
        self.assertEqual(enrich.endpoint, "http://127.0.0.1:11434/v1")
        self.assertEqual(enrich.model_id, "qwen3-4b-instruct-q4")
        self.assertEqual(enrich.max_out, 1500)

        judge = config.roles["judge"]
        self.assertFalse(judge.enabled)
        self.assertEqual(judge.transport, "litellm")

    def test_gateway_resolves_roles_from_yaml(self) -> None:
        gateway = LLMGateway(models_path=MODELS_PATH, mode=GatewayMode.REPLAY)
        role = gateway.resolve_role("enrich.primary")
        self.assertEqual(role.model_id, "qwen3-4b-instruct-q4")
        with self.assertRaises(Exception):
            gateway.resolve_role("missing.role")


class HooksStrippedTests(unittest.TestCase):
    def test_gateway_source_has_no_hook_machinery(self) -> None:
        self.assertTrue(hooks_are_stripped())
        self.assertTrue(generate_signature_has_no_hooks())

    def test_generate_strips_hooks_from_messages_and_replay_request(self) -> None:
        gateway = LLMGateway(models_path=MODELS_PATH, mode=GatewayMode.REPLAY)
        corpus = load_fixtures()
        cassette = corpus.cassettes["enrich"][0]
        request = dict(cassette["request"])

        with patch.object(gateway._adapter, "chat_completion", wraps=gateway._adapter.chat_completion) as mocked:
            gateway.generate(
                "enrich.primary",
                [{"role": "user", "content": "extract evidence", "hooks": ["ignored"]}],
                cassette_kind="enrich",
                replay_request={**request, "hooks": ["ignored"]},
            )
            _, kwargs = mocked.call_args
            self.assertNotIn("hooks", kwargs["messages"][0])
            self.assertNotIn("hooks", kwargs["replay_request"])


class CassetteReplayTests(unittest.TestCase):
    def setUp(self) -> None:
        self.gateway = LLMGateway(models_path=MODELS_PATH, mode=GatewayMode.REPLAY)

    def test_replays_enrich_cassette(self) -> None:
        corpus = load_fixtures()
        cassette = corpus.cassettes["enrich"][0]
        request = dict(cassette["request"])

        result = self.gateway.generate(
            request["role"],
            [{"role": "user", "content": "offline enrich fixture"}],
            cassette_kind="enrich",
            replay_request=request,
        )

        self.assertTrue(result.replayed)
        self.assertEqual(result.input_hash, cassette["input_hash"])
        self.assertEqual(result.text, cassette["response"]["text"])
        self.assertEqual(result.parsed, cassette["response"]["parsed"])

    def test_replays_classify_and_explain_cassettes(self) -> None:
        corpus = load_fixtures()
        for kind in ("classify", "explain"):
            cassette = corpus.cassettes[kind][0]
            request = dict(cassette["request"])
            result = self.gateway.generate(
                request["role"],
                [{"role": "user", "content": f"offline {kind} fixture"}],
                cassette_kind=kind,
                replay_request=request,
            )
            self.assertTrue(result.replayed)
            self.assertEqual(result.input_hash, cassette["input_hash"])
            self.assertEqual(result.text, cassette["response"]["text"])

    def test_missing_cassette_fails_without_live_call(self) -> None:
        with patch("httpx.Client.post") as post_mock:
            with self.assertRaises(CassetteNotFoundError):
                self.gateway.generate(
                    "enrich.primary",
                    [{"role": "user", "content": "unknown request"}],
                    cassette_kind="enrich",
                    replay_request={
                        "model_id": "qwen3-4b-q4",
                        "prompt_version": "does-not-exist",
                        "page_id": "pg_missing",
                    },
                )
            post_mock.assert_not_called()

    def test_replay_mode_health_does_not_use_network(self) -> None:
        with patch("httpx.Client.get") as get_mock:
            self.assertTrue(self.gateway.health("enrich.primary"))
            get_mock.assert_not_called()

    def test_count_tokens_uses_heuristic_without_tiktoken(self) -> None:
        with patch.dict("sys.modules", {"tiktoken": None}):
            count = self.gateway.count_tokens("enrich.primary", "abcd" * 10)
        self.assertEqual(count, 10)


class ModuleExportsTests(unittest.TestCase):
    def test_complete_alias_delegates_to_generate(self) -> None:
        gateway = LLMGateway(models_path=MODELS_PATH, mode=GatewayMode.REPLAY)
        corpus = load_fixtures()
        request = dict(corpus.cassettes["enrich"][0]["request"])
        with patch.object(gateway, "generate", wraps=gateway.generate) as mocked:
            gateway.complete(
                request["role"],
                [{"role": "user", "content": "alias path"}],
                cassette_kind="enrich",
                replay_request=request,
            )
            mocked.assert_called_once()

    def test_module_level_complete_uses_default_gateway(self) -> None:
        corpus = load_fixtures()
        request = dict(corpus.cassettes["explain"][0]["request"])
        result = complete(
            request["role"],
            [{"role": "user", "content": "module alias"}],
            cassette_kind="explain",
            replay_request=request,
        )
        self.assertTrue(result.replayed)


class IntegrationCheckGateway(unittest.TestCase):
    """N11 integration_check: roles from models.yaml; hooks stripped; cassette replay."""

    def test_integration_check_gateway(self) -> None:
        models = yaml.safe_load(MODELS_PATH.read_text(encoding="utf-8"))
        self.assertIn("enrich.primary", models["roles"])
        self.assertTrue(hooks_are_stripped())
        self.assertTrue(generate_signature_has_no_hooks())

        adapter = LiteLLMAdapter(
            load_models_config(MODELS_PATH),
            mode=TransportMode.REPLAY,
        )
        corpus = load_fixtures()
        for kind in ("enrich", "classify", "explain"):
            cassette = corpus.cassettes[kind][0]
            request = dict(cassette["request"])
            result = adapter.chat_completion(
                role=request["role"],
                messages=[{"role": "user", "content": "integration replay"}],
                cassette_kind=kind,
                replay_request=request,
            )
            self.assertTrue(result.replayed)
            self.assertEqual(result.input_hash, cassette["input_hash"])

        with patch.object(httpx.Client, "post", side_effect=AssertionError("live LLM call attempted")):
            with self.assertRaises(CassetteNotFoundError):
                adapter.chat_completion(
                    role="enrich.primary",
                    messages=[{"role": "user", "content": "missing"}],
                    cassette_kind="enrich",
                    replay_request={
                        "model_id": "qwen3-4b-q4",
                        "prompt_version": "missing",
                        "page_id": "pg_missing",
                    },
                )


if __name__ == "__main__":
    unittest.main()
