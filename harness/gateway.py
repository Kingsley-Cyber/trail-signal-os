"""LLM gateway — role-bound model access with offline cassette replay (N11)."""

from __future__ import annotations

import ast
import inspect
import os
from enum import Enum
from pathlib import Path
from typing import Any

from harness.litellm_adapter import (
    CassetteNotFoundError,
    CompletionResult,
    EmbeddingResult,
    LiteLLMAdapter,
    ModelsConfig,
    TransportMode,
    load_models_config,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MODELS_PATH = REPO_ROOT / "config" / "models.yaml"

# Agent Zero plugin hooks are intentionally not carried over (ADR-001).
_HOOK_MARKERS = frozenset(
    {
        "register_hook",
        "HookRegistry",
        "plugin_hooks",
        "run_hooks",
        "hook_manager",
    }
)


class GatewayMode(str, Enum):
    REPLAY = "replay"
    LIVE = "live"


def _mode_from_env() -> GatewayMode:
    raw = os.environ.get("LLM_GATEWAY_MODE", GatewayMode.REPLAY.value).strip().lower()
    if raw == GatewayMode.LIVE.value:
        return GatewayMode.LIVE
    return GatewayMode.REPLAY


class LLMGateway:
    """Model-agnostic gateway. Roles resolve from config/models.yaml only."""

    def __init__(
        self,
        *,
        models_path: Path | None = None,
        mode: GatewayMode | None = None,
        fixtures_root: Path | None = None,
        adapter: LiteLLMAdapter | None = None,
    ) -> None:
        self._models_path = (models_path or DEFAULT_MODELS_PATH).resolve()
        self._config = load_models_config(self._models_path)
        transport_mode = TransportMode(mode or _mode_from_env())
        self._adapter = adapter or LiteLLMAdapter(
            self._config,
            mode=transport_mode,
            fixtures_root=fixtures_root,
        )

    @property
    def config(self) -> ModelsConfig:
        return self._config

    @property
    def mode(self) -> GatewayMode:
        return GatewayMode(self._adapter.mode.value)

    def resolve_role(self, role: str):
        return self._config.resolve(role)

    def generate(
        self,
        role: str,
        messages: list[dict[str, Any]],
        *,
        json_schema: dict[str, Any] | None = None,
        max_out: int | None = None,
        timeout: float = 120.0,
        cassette_kind: str | None = None,
        replay_request: dict[str, Any] | None = None,
    ) -> CompletionResult:
        cleaned_messages = _strip_hooks_from_messages(messages)
        cleaned_replay = _strip_hooks_from_mapping(replay_request)
        role_cfg = self._config.resolve(role)
        return self._adapter.chat_completion(
            role=role,
            messages=cleaned_messages,
            cassette_kind=cassette_kind,
            replay_request=cleaned_replay,
            max_out=max_out or role_cfg.max_out,
            timeout=timeout,
            json_schema=json_schema,
        )

    def complete(
        self,
        role: str,
        messages: list[dict[str, Any]],
        **kwargs: Any,
    ) -> CompletionResult:
        return self.generate(role, messages, **kwargs)

    def embed(
        self,
        role: str,
        texts: list[str],
        *,
        cassette_kind: str | None = None,
        replay_request: dict[str, Any] | None = None,
        timeout: float = 120.0,
    ) -> EmbeddingResult:
        cleaned_replay = _strip_hooks_from_mapping(replay_request)
        return self._adapter.embed(
            role=role,
            texts=texts,
            cassette_kind=cassette_kind,
            replay_request=cleaned_replay,
            timeout=timeout,
        )

    def count_tokens(self, role: str, text: str) -> int:
        self._config.resolve(role)
        if not text:
            return 0
        try:
            import tiktoken
        except ImportError:
            return max(1, len(text) // 4)
        try:
            encoder = tiktoken.get_encoding("cl100k_base")
        except Exception:
            return max(1, len(text) // 4)
        return len(encoder.encode(text))

    def health(self, role: str, *, timeout: float = 5.0) -> bool:
        return self._adapter.health(role, timeout=timeout)

    def close(self) -> None:
        self._adapter.close()


_default_gateway: LLMGateway | None = None


def get_gateway() -> LLMGateway:
    global _default_gateway
    if _default_gateway is None:
        _default_gateway = LLMGateway()
    return _default_gateway


def complete(role: str, messages: list[dict[str, Any]], **kwargs: Any) -> CompletionResult:
    return get_gateway().complete(role, messages, **kwargs)


def generate(role: str, messages: list[dict[str, Any]], **kwargs: Any) -> CompletionResult:
    return get_gateway().generate(role, messages, **kwargs)


def embed(role: str, texts: list[str], **kwargs: Any) -> EmbeddingResult:
    return get_gateway().embed(role, texts, **kwargs)


def count_tokens(role: str, text: str) -> int:
    return get_gateway().count_tokens(role, text)


def health(role: str, *, timeout: float = 5.0) -> bool:
    return get_gateway().health(role, timeout=timeout)


def hooks_are_stripped() -> bool:
    """Return True when gateway source defines no Agent Zero hook machinery."""
    source = Path(__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if node.name in _HOOK_MARKERS:
                return False
    return True


def generate_signature_has_no_hooks() -> bool:
    signature = inspect.signature(LLMGateway.generate)
    return "hooks" not in signature.parameters


def _strip_hooks_from_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    cleaned: list[dict[str, Any]] = []
    for message in messages:
        if not isinstance(message, dict):
            cleaned.append(message)
            continue
        item = dict(message)
        item.pop("hooks", None)
        cleaned.append(item)
    return cleaned


def _strip_hooks_from_mapping(payload: dict[str, Any] | None) -> dict[str, Any] | None:
    if payload is None:
        return None
    cleaned = dict(payload)
    cleaned.pop("hooks", None)
    return cleaned


__all__ = [
    "CassetteNotFoundError",
    "CompletionResult",
    "EmbeddingResult",
    "GatewayMode",
    "LLMGateway",
    "complete",
    "count_tokens",
    "embed",
    "generate",
    "generate_signature_has_no_hooks",
    "get_gateway",
    "health",
    "hooks_are_stripped",
    "load_models_config",
]
