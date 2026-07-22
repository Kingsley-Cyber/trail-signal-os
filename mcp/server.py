"""MCP operator surface — thin wrappers over the control API (N19)."""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
from fastmcp import FastMCP

MCP_PORT = 8766
DEFAULT_HOST = "127.0.0.1"
DEFAULT_CONTROL_API_BASE_URL = "http://127.0.0.1:8100"
BUNDLE_DEFAULT_TOKENS = 6000
MCP_RESPONSE_CAP = 8000


@dataclass(frozen=True)
class McpSettings:
    host: str
    port: int
    control_api_base_url: str
    control_api_token: str


@dataclass
class IdempotencyCache:
    """In-process dedup for duplicate MCP tool invocations."""

    _entries: dict[str, Any] = field(default_factory=dict)

    def get(self, key: str) -> Any | None:
        return self._entries.get(key)

    def put(self, key: str, value: Any) -> None:
        self._entries[key] = value


class ControlApiError(RuntimeError):
    def __init__(self, status_code: int, detail: Any) -> None:
        super().__init__(f"control API {status_code}: {detail}")
        self.status_code = status_code
        self.detail = detail


class ControlApiClient:
    """HTTP client for control-api — MCP never touches Postgres or Redis."""

    def __init__(
        self,
        *,
        base_url: str,
        bearer_token: str,
        http_client: httpx.Client | None = None,
    ) -> None:
        self._owns_client = http_client is None
        self._client = http_client or httpx.Client(
            base_url=base_url.rstrip("/"),
            timeout=30.0,
        )
        self._bearer_token = bearer_token

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def _auth_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._bearer_token}"}

    def create_job(self, body: dict[str, Any]) -> dict[str, Any]:
        response = self._client.post(
            "/v1/research-jobs",
            json=body,
            headers=self._auth_headers(),
        )
        if response.status_code == 201:
            return response.json()
        if response.status_code == 409 and body.get("job_id"):
            return self.get_job(body["job_id"])
        self._raise_for_status(response)

    def get_job(self, job_id: str) -> dict[str, Any]:
        response = self._client.get(f"/v1/research-jobs/{job_id}")
        self._raise_for_status(response)
        return response.json()

    def list_tasks(self, job_id: str) -> dict[str, Any]:
        response = self._client.get(f"/v1/research-jobs/{job_id}/tasks")
        self._raise_for_status(response)
        return response.json()

    @staticmethod
    def _raise_for_status(response: httpx.Response) -> None:
        if response.is_success:
            return
        detail: Any
        try:
            payload = response.json()
            detail = payload.get("detail", payload)
        except json.JSONDecodeError:
            detail = response.text
        raise ControlApiError(response.status_code, detail)


def load_mcp_settings() -> McpSettings:
    env_file = Path(__file__).resolve().parents[1] / ".env"
    if env_file.is_file():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                continue
            key, value = stripped.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip())

    token = os.environ.get("CONTROL_API_TOKEN")
    if not token:
        raise RuntimeError(
            "CONTROL_API_TOKEN is required for MCP server (shared with control API)"
        )

    return McpSettings(
        host=os.environ.get("MCP_HOST", DEFAULT_HOST),
        port=int(os.environ.get("MCP_PORT", str(MCP_PORT))),
        control_api_base_url=os.environ.get(
            "CONTROL_API_BASE_URL", DEFAULT_CONTROL_API_BASE_URL
        ),
        control_api_token=token,
    )


def _estimate_tokens(value: Any) -> int:
    serialized = json.dumps(value, separators=(",", ":"), sort_keys=True)
    return max(1, len(serialized) // 4)


def _resolve_idempotency_key(
    *,
    tool_name: str,
    idempotency_key: str | None,
    payload: dict[str, Any],
) -> str:
    if idempotency_key:
        return idempotency_key
    if payload.get("job_id"):
        return f"{tool_name}:{payload['job_id']}"
    digest = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return f"{tool_name}:{digest}"


def _task_counters(tasks: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for task in tasks:
        state = str(task.get("state", "UNKNOWN"))
        counts[state] = counts.get(state, 0) + 1
    return counts


def _build_status_payload(job: dict[str, Any], tasks_payload: dict[str, Any]) -> dict[str, Any]:
    tasks = tasks_payload.get("tasks", [])
    task_counts = _task_counters(tasks)
    return {
        "job_id": job["job_id"],
        "status": job["status"],
        "job_kind": job["job_kind"],
        "niche_id": job.get("niche_id"),
        "config_hash": job.get("config_hash"),
        "budget": job.get("budget"),
        "total_tasks": len(tasks),
        "task_counts": task_counts,
        "created_at": job.get("created_at"),
        "updated_at": job.get("updated_at"),
    }


def _build_bundle_payload(
    job_id: str,
    *,
    max_tokens: int,
    records: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    capped_records = records or []
    token_total = sum(_estimate_tokens(record) for record in capped_records)
    if token_total > max_tokens:
        trimmed: list[dict[str, Any]] = []
        running = 0
        for record in capped_records:
            record_tokens = _estimate_tokens(record)
            if running + record_tokens > max_tokens:
                break
            trimmed.append(record)
            running += record_tokens
        capped_records = trimmed
        token_total = running

    excluded = max(0, len(records or []) - len(capped_records))
    manifest = {
        "included": len(capped_records),
        "excluded": excluded,
        "token_total": token_total,
        "max_tokens": max_tokens,
        "coverage_by_domain": {},
        "coverage_by_claim_type": {},
        "next_cursor": None,
    }
    payload = {
        "job_id": job_id,
        "records": capped_records,
        "manifest": manifest,
    }
    if _estimate_tokens(payload) > MCP_RESPONSE_CAP:
        raise ValueError(
            f"MCP response exceeds cap of {MCP_RESPONSE_CAP} tokens; narrow filters or use cursor"
        )
    return payload


def create_server(
    client: ControlApiClient,
    *,
    idempotency_cache: IdempotencyCache | None = None,
) -> FastMCP:
    cache = idempotency_cache or IdempotencyCache()
    mcp = FastMCP(
        "trail-signal-os",
        instructions=(
            "Operator MCP surface for trail-signal-os. Tools proxy the control API; "
            "they never access Postgres or Redis directly."
        ),
    )

    @mcp.tool(name="research.create_job")
    def research_create_job(
        job_kind: str,
        niche_id: str | None = None,
        budget: dict[str, Any] | None = None,
        job_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Create a research job via control-api."""
        payload = {
            "job_kind": job_kind,
            "niche_id": niche_id,
            "budget": budget,
            "job_id": job_id,
        }
        payload = {key: value for key, value in payload.items() if value is not None}
        cache_key = _resolve_idempotency_key(
            tool_name="research.create_job",
            idempotency_key=idempotency_key,
            payload=payload,
        )
        cached = cache.get(cache_key)
        if cached is not None:
            return cached

        result = client.create_job(payload)
        cache.put(cache_key, result)
        return result

    @mcp.tool(name="research.status")
    def research_status(job_id: str) -> dict[str, Any]:
        """Return job counters and manifests — never a content dump."""
        job = client.get_job(job_id)
        tasks_payload = client.list_tasks(job_id)
        return _build_status_payload(job, tasks_payload)

    @mcp.tool(name="evidence.bundle")
    def evidence_bundle(
        job_id: str,
        max_tokens: int = BUNDLE_DEFAULT_TOKENS,
        query: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Return a token-budgeted evidence bundle with manifest for a job."""
        if max_tokens < 1:
            raise ValueError("max_tokens must be >= 1")
        if max_tokens > MCP_RESPONSE_CAP:
            raise ValueError(f"max_tokens cannot exceed MCP cap {MCP_RESPONSE_CAP}")

        cache_key = _resolve_idempotency_key(
            tool_name="evidence.bundle",
            idempotency_key=idempotency_key,
            payload={"job_id": job_id, "max_tokens": max_tokens, "query": query},
        )
        cached = cache.get(cache_key)
        if cached is not None:
            return cached

        # Ensure the job exists before returning an empty bundle manifest.
        client.get_job(job_id)
        result = _build_bundle_payload(job_id, max_tokens=max_tokens)
        cache.put(cache_key, result)
        return result

    return mcp


def main() -> None:
    settings = load_mcp_settings()
    client = ControlApiClient(
        base_url=settings.control_api_base_url,
        bearer_token=settings.control_api_token,
    )
    server = create_server(client)
    try:
        server.run(transport="http", host=settings.host, port=settings.port)
    finally:
        client.close()


if __name__ == "__main__":
    main()
