"""LiteLLM / OpenAI-compatible transport with offline cassette replay (N11)."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

import httpx
import yaml

from fixtures.load import FIXTURES_ROOT, load_fixtures

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MODELS_PATH = REPO_ROOT / "config" / "models.yaml"


class TransportMode(str, Enum):
    REPLAY = "replay"
    LIVE = "live"


class GatewayError(Exception):
    """Base gateway transport error."""


class CassetteNotFoundError(GatewayError):
    """Replay-only mode could not locate a matching cassette."""


class RoleNotFoundError(GatewayError):
    """Unknown gateway role."""


class RoleDisabledError(GatewayError):
    """Role exists in models.yaml but is disabled."""


class LiveCallForbiddenError(GatewayError):
    """Live network call attempted while replay-only mode is active."""


@dataclass(frozen=True)
class RoleConfig:
    name: str
    endpoint: str
    model_id: str
    ctx_window: int = 32768
    max_out: int = 1500
    supports_schema: str = "client_only"
    transport: str = "openai_compatible"
    enabled: bool = True
    cost_per_mtok: dict[str, float] | None = None
    tps_estimate: int | None = None


@dataclass(frozen=True)
class ModelsConfig:
    version: str
    roles: dict[str, RoleConfig]

    def resolve(self, role: str) -> RoleConfig:
        try:
            config = self.roles[role]
        except KeyError as exc:
            raise RoleNotFoundError(f"unknown gateway role {role!r}") from exc
        if not config.enabled:
            raise RoleDisabledError(f"gateway role {role!r} is disabled in models.yaml")
        return config


@dataclass(frozen=True)
class CompletionResult:
    role: str
    model_id: str
    text: str
    parsed: dict[str, Any] | None
    input_hash: str
    cassette_kind: str | None
    replayed: bool
    usage: dict[str, int]


@dataclass(frozen=True)
class EmbeddingResult:
    role: str
    model_id: str
    vectors: list[list[float]]
    replayed: bool


def load_models_config(path: Path | None = None) -> ModelsConfig:
    models_path = (path or DEFAULT_MODELS_PATH).resolve()
    if not models_path.is_file():
        raise FileNotFoundError(f"missing models config {models_path}")
    payload = yaml.safe_load(models_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{models_path} must contain a YAML mapping")
    roles_raw = payload.get("roles")
    if not isinstance(roles_raw, dict) or not roles_raw:
        raise ValueError(f"{models_path} must define a non-empty roles mapping")

    roles: dict[str, RoleConfig] = {}
    for role_name, role_payload in roles_raw.items():
        if not isinstance(role_payload, dict):
            raise ValueError(f"{models_path} role {role_name!r} must be a mapping")
        endpoint = role_payload.get("endpoint")
        model_id = role_payload.get("model_id")
        if not endpoint or not model_id:
            raise ValueError(f"{models_path} role {role_name!r} requires endpoint and model_id")
        roles[role_name] = RoleConfig(
            name=role_name,
            endpoint=str(endpoint),
            model_id=str(model_id),
            ctx_window=int(role_payload.get("ctx_window", 32768)),
            max_out=int(role_payload.get("max_out", 1500)),
            supports_schema=str(role_payload.get("supports_schema", "client_only")),
            transport=str(role_payload.get("transport", "openai_compatible")),
            enabled=bool(role_payload.get("enabled", True)),
            cost_per_mtok=_coerce_cost(role_payload.get("cost_per_mtok")),
            tps_estimate=(
                int(role_payload["tps_estimate"])
                if role_payload.get("tps_estimate") is not None
                else None
            ),
        )
    return ModelsConfig(version=str(payload.get("version", "unknown")), roles=roles)


def _coerce_cost(raw: Any) -> dict[str, float] | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ValueError("cost_per_mtok must be a mapping")
    return {str(key): float(value) for key, value in raw.items()}


def canonical_request_hash(cassette_kind: str, request: dict[str, Any]) -> str:
    payload = {"cassette_kind": cassette_kind, "request": request}
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


class CassetteReplayStore:
    """Index fixture cassettes for deterministic replay."""

    def __init__(self, fixtures_root: Path | None = None) -> None:
        corpus = load_fixtures(fixtures_root)
        self._by_hash: dict[tuple[str, str], dict[str, Any]] = {}
        self._by_request: dict[tuple[str, tuple[tuple[str, Any], ...]], dict[str, Any]] = {}
        for kind, entries in corpus.cassettes.items():
            for entry in entries:
                input_hash = str(entry["input_hash"])
                request = entry.get("request", {})
                if not isinstance(request, dict):
                    continue
                self._by_hash[(kind, input_hash)] = entry
                request_key = (kind, _freeze_mapping(request))
                self._by_request[request_key] = entry

    def get(self, cassette_kind: str, *, input_hash: str | None = None, request: dict[str, Any] | None = None) -> dict[str, Any]:
        if input_hash is not None:
            match = self._by_hash.get((cassette_kind, input_hash))
            if match is not None:
                return match
        if request is not None:
            match = self._by_request.get((cassette_kind, _freeze_mapping(request)))
            if match is not None:
                return match
        raise CassetteNotFoundError(
            f"no cassette for kind={cassette_kind!r} "
            f"input_hash={input_hash!r} request={request!r}"
        )


def _freeze_mapping(payload: dict[str, Any]) -> tuple[tuple[str, Any], ...]:
    return tuple(sorted(payload.items(), key=lambda item: item[0]))


class LiteLLMAdapter:
    """Role-bound transport with replay-only gate mode and optional live calls."""

    def __init__(
        self,
        config: ModelsConfig,
        *,
        mode: TransportMode = TransportMode.REPLAY,
        fixtures_root: Path | None = None,
        http_client: httpx.Client | None = None,
    ) -> None:
        self._config = config
        self._mode = mode
        self._fixtures_root = fixtures_root or FIXTURES_ROOT
        self._cassettes = CassetteReplayStore(self._fixtures_root)
        self._http = http_client

    @property
    def mode(self) -> TransportMode:
        return self._mode

    def chat_completion(
        self,
        *,
        role: str,
        messages: list[dict[str, Any]],
        cassette_kind: str | None = None,
        replay_request: dict[str, Any] | None = None,
        max_out: int | None = None,
        timeout: float = 120.0,
        json_schema: dict[str, Any] | None = None,
    ) -> CompletionResult:
        role_cfg = self._config.resolve(role)
        request_fields = _build_replay_request(role_cfg, replay_request)
        kind = cassette_kind or _infer_cassette_kind(replay_request)
        input_hash = canonical_request_hash(kind, request_fields) if kind else ""

        if self._mode is TransportMode.REPLAY:
            if kind is None:
                raise CassetteNotFoundError(
                    "replay mode requires cassette_kind or replay_request with cassette_kind"
                )
            cassette = self._cassettes.get(kind, request=request_fields)
            response = cassette["response"]
            text = str(response.get("text", ""))
            parsed = response.get("parsed")
            parsed_dict = parsed if isinstance(parsed, dict) else None
            return CompletionResult(
                role=role,
                model_id=role_cfg.model_id,
                text=text,
                parsed=parsed_dict,
                input_hash=str(cassette["input_hash"]),
                cassette_kind=kind,
                replayed=True,
                usage={"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
            )

        return self._live_chat_completion(
            role_cfg=role_cfg,
            messages=messages,
            max_out=max_out or role_cfg.max_out,
            timeout=timeout,
            json_schema=json_schema,
            input_hash=input_hash,
            cassette_kind=kind,
        )

    def embed(
        self,
        *,
        role: str,
        texts: list[str],
        cassette_kind: str | None = None,
        replay_request: dict[str, Any] | None = None,
        timeout: float = 120.0,
    ) -> EmbeddingResult:
        role_cfg = self._config.resolve(role)
        if self._mode is TransportMode.REPLAY:
            if cassette_kind is None:
                raise CassetteNotFoundError("replay mode embed requires cassette_kind")
            request_fields = _build_replay_request(role_cfg, replay_request)
            cassette = self._cassettes.get(cassette_kind, request=request_fields)
            response = cassette["response"]
            vectors = response.get("vectors")
            if not isinstance(vectors, list):
                raise CassetteNotFoundError(
                    f"cassette {cassette.get('input_hash')} missing embed vectors"
                )
            return EmbeddingResult(
                role=role,
                model_id=role_cfg.model_id,
                vectors=vectors,
                replayed=True,
            )

        return self._live_embed(role_cfg=role_cfg, texts=texts, timeout=timeout)

    def health(self, role: str, *, timeout: float = 5.0) -> bool:
        role_cfg = self._config.resolve(role)
        if self._mode is TransportMode.REPLAY:
            return True
        client = self._client()
        base = role_cfg.endpoint.rstrip("/")
        for path in ("/models", "/health", ""):
            try:
                response = client.get(f"{base}{path}", timeout=timeout)
            except httpx.HTTPError:
                continue
            if response.status_code < 500:
                return True
        return False

    def _live_chat_completion(
        self,
        *,
        role_cfg: RoleConfig,
        messages: list[dict[str, Any]],
        max_out: int,
        timeout: float,
        json_schema: dict[str, Any] | None,
        input_hash: str,
        cassette_kind: str | None,
    ) -> CompletionResult:
        if role_cfg.transport == "litellm":
            text, usage = _litellm_chat(
                role_cfg=role_cfg,
                messages=messages,
                max_out=max_out,
                timeout=timeout,
                json_schema=json_schema,
            )
        else:
            text, usage = _openai_compatible_chat(
                client=self._client(),
                role_cfg=role_cfg,
                messages=messages,
                max_out=max_out,
                timeout=timeout,
                json_schema=json_schema,
            )
        return CompletionResult(
            role=role_cfg.name,
            model_id=role_cfg.model_id,
            text=text,
            parsed=_try_parse_json(text),
            input_hash=input_hash,
            cassette_kind=cassette_kind,
            replayed=False,
            usage=usage,
        )

    def _live_embed(
        self,
        *,
        role_cfg: RoleConfig,
        texts: list[str],
        timeout: float,
    ) -> EmbeddingResult:
        if role_cfg.transport == "litellm":
            vectors = _litellm_embed(role_cfg=role_cfg, texts=texts, timeout=timeout)
        else:
            vectors = _openai_compatible_embed(
                client=self._client(),
                role_cfg=role_cfg,
                texts=texts,
                timeout=timeout,
            )
        return EmbeddingResult(
            role=role_cfg.name,
            model_id=role_cfg.model_id,
            vectors=vectors,
            replayed=False,
        )

    def _client(self) -> httpx.Client:
        if self._http is None:
            self._http = httpx.Client()
        return self._http

    def close(self) -> None:
        if self._http is not None:
            self._http.close()
            self._http = None


def _build_replay_request(role_cfg: RoleConfig, replay_request: dict[str, Any] | None) -> dict[str, Any]:
    request_fields: dict[str, Any] = {"role": role_cfg.name, "model_id": role_cfg.model_id}
    if replay_request:
        for key, value in replay_request.items():
            if key == "hooks":
                continue
            request_fields[key] = value
    return request_fields


def _infer_cassette_kind(replay_request: dict[str, Any] | None) -> str | None:
    if not replay_request:
        return None
    kind = replay_request.get("cassette_kind")
    return str(kind) if kind else None


def _try_parse_json(text: str) -> dict[str, Any] | None:
    stripped = text.strip()
    if not stripped.startswith("{"):
        return None
    try:
        parsed = json.loads(stripped)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _openai_compatible_chat(
    *,
    client: httpx.Client,
    role_cfg: RoleConfig,
    messages: list[dict[str, Any]],
    max_out: int,
    timeout: float,
    json_schema: dict[str, Any] | None,
) -> tuple[str, dict[str, int]]:
    payload: dict[str, Any] = {
        "model": role_cfg.model_id,
        "messages": messages,
        "max_tokens": max_out,
    }
    if json_schema is not None:
        payload["response_format"] = {"type": "json_schema", "json_schema": json_schema}
    url = f"{role_cfg.endpoint.rstrip('/')}/chat/completions"
    response = client.post(url, json=payload, timeout=timeout)
    response.raise_for_status()
    body = response.json()
    choices = body.get("choices") or []
    if not choices:
        raise GatewayError(f"{url} returned no choices")
    message = choices[0].get("message") or {}
    text = str(message.get("content", ""))
    usage_raw = body.get("usage") or {}
    usage = {
        "prompt_tokens": int(usage_raw.get("prompt_tokens", 0)),
        "completion_tokens": int(usage_raw.get("completion_tokens", 0)),
        "total_tokens": int(usage_raw.get("total_tokens", 0)),
    }
    return text, usage


def _openai_compatible_embed(
    *,
    client: httpx.Client,
    role_cfg: RoleConfig,
    texts: list[str],
    timeout: float,
) -> list[list[float]]:
    payload = {"model": role_cfg.model_id, "input": texts}
    url = f"{role_cfg.endpoint.rstrip('/')}/embeddings"
    response = client.post(url, json=payload, timeout=timeout)
    response.raise_for_status()
    body = response.json()
    data = body.get("data") or []
    vectors: list[list[float]] = []
    for item in sorted(data, key=lambda row: row.get("index", 0)):
        embedding = item.get("embedding")
        if not isinstance(embedding, list):
            raise GatewayError(f"{url} returned invalid embedding payload")
        vectors.append([float(value) for value in embedding])
    return vectors


def _litellm_chat(
    *,
    role_cfg: RoleConfig,
    messages: list[dict[str, Any]],
    max_out: int,
    timeout: float,
    json_schema: dict[str, Any] | None,
) -> tuple[str, dict[str, int]]:
    try:
        import litellm
    except ImportError as exc:
        raise GatewayError(
            "role transport=litellm requires the litellm package to be installed"
        ) from exc

    kwargs: dict[str, Any] = {
        "model": role_cfg.model_id,
        "messages": messages,
        "max_tokens": max_out,
        "api_base": role_cfg.endpoint.rstrip("/"),
        "timeout": timeout,
    }
    if json_schema is not None:
        kwargs["response_format"] = {"type": "json_schema", "json_schema": json_schema}
    response = litellm.completion(**kwargs)
    text = response.choices[0].message.content or ""
    usage_raw = getattr(response, "usage", None) or {}
    usage = {
        "prompt_tokens": int(getattr(usage_raw, "prompt_tokens", 0) or 0),
        "completion_tokens": int(getattr(usage_raw, "completion_tokens", 0) or 0),
        "total_tokens": int(getattr(usage_raw, "total_tokens", 0) or 0),
    }
    return str(text), usage


def _litellm_embed(*, role_cfg: RoleConfig, texts: list[str], timeout: float) -> list[list[float]]:
    try:
        import litellm
    except ImportError as exc:
        raise GatewayError(
            "role transport=litellm requires the litellm package to be installed"
        ) from exc

    response = litellm.embedding(
        model=role_cfg.model_id,
        input=texts,
        api_base=role_cfg.endpoint.rstrip("/"),
        timeout=timeout,
    )
    data = response.data
    vectors: list[list[float]] = []
    for item in sorted(data, key=lambda row: row.get("index", 0)):
        vectors.append([float(value) for value in item["embedding"]])
    return vectors
