"""LLM signal classification — evidence.v1 → signal_raw (N23, LAW 1 probabilistic side)."""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import psycopg

from control.dispatcher import enqueue_ready_task
from db.repositories.constraints import insert_lineage_edge_idempotent
from db.repositories.persist_artifact import persist_artifact
from guards.runtime_guards import guard6_require_lineage_edge
from harness.gateway import GatewayMode, LLMGateway
from harness.node_executor import validate_packed_input
from signal_engine.normalize import NormalizeError, normalize_signal_raw, persist_signal_v1

REPO_ROOT = Path(__file__).resolve().parents[1]
PROMPT_PATH = REPO_ROOT / "prompts" / "signal_classify.md"

CLASSIFY_ROLE = "enrich.primary"
CASSETTE_KIND = "classify"
PROMPT_VERSION = "signal_classify-2026.07.21"
CODE_VERSION = "classify-1.0.0"
EVIDENCE_SCHEMA_VERSION = "evidence.v1"
SIGNAL_SCHEMA_VERSION = "signal.v1"
MAX_ITERATIONS = 2

CASSETTE_MODEL_ID = "qwen3-4b-q4"

REPAIR_LANE = "extract"
REPAIR_STREAM_NAME = "cp:signal:repair"

SIGNAL_TYPES = frozenset({"demand", "growth", "pain", "competition", "content"})
SOURCE_TIERS = frozenset({"open", "defended", "hostile"})
LAW1_FORBIDDEN_KEYS = frozenset(
    {
        "normalized_score",
        "score",
        "subscores",
        "opportunity_id",
        "final",
        "opp_confidence",
    }
)


class ClassifyError(Exception):
    """Signal classification failed validation."""


@dataclass(frozen=True)
class ClassifyExecutionResult:
    verdict: str
    attempts: int
    output: dict[str, Any] | None
    violations: tuple[str, ...]
    replayed: bool


@dataclass(frozen=True)
class ClassifySuccess:
    signal_raw: dict[str, Any]
    signal: dict[str, Any]
    artifact_id: str
    artifact_inserted: bool
    lineage_edge_inserted: bool
    attempts: int
    replayed: bool


@dataclass(frozen=True)
class ClassifyRepairRoute:
    verdict: str
    attempts: int
    violations: tuple[str, ...]
    repair_task_id: str
    repair_stream: str
    output: dict[str, Any] | None


def load_classify_prompt() -> str:
    if not PROMPT_PATH.is_file():
        raise FileNotFoundError(f"missing classify prompt {PROMPT_PATH}")
    return PROMPT_PATH.read_text(encoding="utf-8")


def build_replay_request(
    evidence: dict[str, Any],
    *,
    model_id: str = CASSETTE_MODEL_ID,
) -> dict[str, Any]:
    """Build cassette replay key fields for offline classify cassettes."""
    return {
        "record_id": evidence["record_id"],
        "prompt_version": PROMPT_VERSION,
        "model_id": model_id,
    }


def _validate_signal_raw(raw: dict[str, Any]) -> tuple[str, ...]:
    violations: list[str] = []
    forbidden = sorted(LAW1_FORBIDDEN_KEYS.intersection(raw.keys()))
    if forbidden:
        violations.append(
            "LAW 1: classify output must not contain scoring fields: "
            + ", ".join(forbidden)
        )
    if isinstance(raw.get("confidence"), (int, float)):
        violations.append("LAW 1: classify output must not contain numeric confidence")

    for key in ("niche_id", "signal_type", "source", "window", "raw_metric", "evidence_ids"):
        if key not in raw:
            violations.append(f"missing required field {key!r}")

    signal_type = raw.get("signal_type")
    if signal_type not in SIGNAL_TYPES:
        violations.append(f"invalid signal_type {signal_type!r}")

    source = raw.get("source")
    if not isinstance(source, dict):
        violations.append("source must be an object")
    elif source.get("tier") not in SOURCE_TIERS:
        violations.append(f"invalid source.tier {source.get('tier')!r}")

    window = raw.get("window")
    if not isinstance(window, dict) or "from" not in window or "to" not in window:
        violations.append("window must include from and to")

    raw_metric = raw.get("raw_metric")
    if not isinstance(raw_metric, dict):
        violations.append("raw_metric must be an object")
    else:
        for metric_key in ("name", "value", "unit", "sample_n"):
            if metric_key not in raw_metric:
                violations.append(f"raw_metric missing {metric_key!r}")
        sample_n = raw_metric.get("sample_n") if isinstance(raw_metric, dict) else None
        if not isinstance(sample_n, int) or sample_n < 1:
            violations.append("raw_metric.sample_n must be a positive integer")

    evidence_ids = raw.get("evidence_ids")
    if not isinstance(evidence_ids, list) or not evidence_ids:
        violations.append("evidence_ids must be a non-empty array")

    return tuple(violations)


def validate_signal_raw(raw: dict[str, Any]) -> None:
    violations = _validate_signal_raw(raw)
    if violations:
        raise ClassifyError("; ".join(violations))


def finalize_signal_raw(
    raw: dict[str, Any],
    evidence: dict[str, Any],
    *,
    model_id: str,
    classify_task_id: str,
) -> dict[str, Any]:
    signal_raw = dict(raw)
    record_id = evidence["record_id"]
    evidence_ids = list(signal_raw.get("evidence_ids") or [])
    if record_id not in evidence_ids:
        evidence_ids.insert(0, record_id)
    signal_raw["evidence_ids"] = evidence_ids

    if not signal_raw.get("source") and evidence.get("source"):
        domain = evidence["source"].get("domain", "unknown")
        signal_raw.setdefault("source", {"domain": domain, "tier": "open"})

    signal_raw["extraction"] = {
        "model_id": model_id,
        "prompt_version": PROMPT_VERSION,
        "role": CLASSIFY_ROLE,
        **(signal_raw.get("extraction") or {}),
    }
    validate_signal_raw(signal_raw)
    return signal_raw


def _build_messages(
    evidence: dict[str, Any],
    *,
    prior_output: dict[str, Any] | None = None,
    violations: tuple[str, ...] = (),
) -> list[dict[str, Any]]:
    prompt = load_classify_prompt()
    user_content = {
        "node_id": "signal_classify",
        "input_schema": EVIDENCE_SCHEMA_VERSION,
        "output_schema": "signal_raw",
        "packed_input": evidence,
    }
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": prompt},
        {"role": "user", "content": json.dumps(user_content, sort_keys=True)},
    ]
    if prior_output is not None and violations:
        messages.append(
            {"role": "assistant", "content": json.dumps(prior_output, sort_keys=True)}
        )
        messages.append(
            {
                "role": "user",
                "content": "Repair the output. Verifier violations:\n"
                + "\n".join(f"- {item}" for item in violations),
            }
        )
    return messages


def _extract_llm_output(result_parsed: dict[str, Any] | None, text: str) -> dict[str, Any]:
    if isinstance(result_parsed, dict):
        return dict(result_parsed)
    payload = json.loads(text)
    if not isinstance(payload, dict):
        raise ClassifyError("LLM output must be a JSON object")
    return payload


def classify_evidence(
    evidence: dict[str, Any],
    *,
    gateway: LLMGateway | None = None,
    replay_request: dict[str, Any] | None = None,
    model_id: str = CASSETTE_MODEL_ID,
    max_iterations: int = MAX_ITERATIONS,
) -> ClassifyExecutionResult:
    """Run classify loop (attempt + repair reprompt) via gateway cassette replay."""
    validate_packed_input(evidence, EVIDENCE_SCHEMA_VERSION)
    request = replay_request or build_replay_request(evidence, model_id=model_id)
    llm_gateway = gateway or LLMGateway(mode=GatewayMode.REPLAY)

    last_violations: tuple[str, ...] = ()
    last_output: dict[str, Any] | None = None
    replayed = False

    for attempt in range(1, max_iterations + 1):
        messages = _build_messages(
            evidence,
            prior_output=last_output if attempt > 1 else None,
            violations=last_violations if attempt > 1 else (),
        )
        completion = llm_gateway.generate(
            CLASSIFY_ROLE,
            messages,
            cassette_kind=CASSETTE_KIND,
            replay_request=request,
        )
        replayed = completion.replayed
        candidate = _extract_llm_output(completion.parsed, completion.text)
        violations = _validate_signal_raw(candidate)
        if not violations:
            return ClassifyExecutionResult(
                verdict="pass",
                attempts=attempt,
                output=candidate,
                violations=(),
                replayed=replayed,
            )
        last_output = candidate
        last_violations = violations

    return ClassifyExecutionResult(
        verdict="ceiling",
        attempts=max_iterations,
        output=last_output,
        violations=last_violations,
        replayed=replayed,
    )


def enqueue_repair_task(
    conn: psycopg.Connection,
    *,
    job_id: str,
    record_id: str,
    classify_task_id: str,
    violations: tuple[str, ...],
    config_hash: str,
    created_at: str,
) -> tuple[str, str]:
    """Route failed classify to cp:signal:repair — never normalize invalid raw."""
    repair_task_id = f"tsk_signal_repair_{uuid.uuid4().hex[:12]}"
    idempotency_key = f"sha256:{hashlib.sha256(f'{classify_task_id}:signal_repair'.encode()).hexdigest()}"
    payload_ref = json.dumps(
        {
            "record_id": record_id,
            "classify_task_id": classify_task_id,
            "violations": list(violations),
        },
        sort_keys=True,
    )
    provenance = {
        "schema_version": "task.v1",
        "config_hash": config_hash,
        "created_at": created_at,
        "code_version": CODE_VERSION,
    }
    result = enqueue_ready_task(
        conn,
        task_id=repair_task_id,
        job_id=job_id,
        lane=REPAIR_LANE,
        idempotency_key=idempotency_key,
        payload_ref=payload_ref,
        provenance=provenance,
        priority=3,
    )
    if result.stream_name != REPAIR_STREAM_NAME:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE outbox_events
                SET stream_name = %s
                WHERE event_id = %s
                """,
                (REPAIR_STREAM_NAME, result.event_id),
            )
    return repair_task_id, REPAIR_STREAM_NAME


def run_classify_normalize_task(
    conn: psycopg.Connection,
    *,
    job_id: str,
    evidence: dict[str, Any],
    classify_task_id: str,
    config_hash: str,
    created_at: str,
    cohort_raw_values: list[float],
    gateway: LLMGateway | None = None,
    replay_request: dict[str, Any] | None = None,
    storage_root: Path | None = None,
    observed_at: str | None = None,
) -> ClassifySuccess | ClassifyRepairRoute:
    """Classify via LLM cassette, normalize deterministically, persist signal.v1 + lineage."""
    llm_gateway = gateway or LLMGateway(mode=GatewayMode.REPLAY)
    role_model_id = llm_gateway.resolve_role(CLASSIFY_ROLE).model_id
    execution = classify_evidence(
        evidence,
        gateway=llm_gateway,
        replay_request=replay_request,
        model_id=role_model_id if llm_gateway.mode is GatewayMode.LIVE else CASSETTE_MODEL_ID,
    )

    if execution.verdict != "pass" or execution.output is None:
        repair_task_id, repair_stream = enqueue_repair_task(
            conn,
            job_id=job_id,
            record_id=evidence["record_id"],
            classify_task_id=classify_task_id,
            violations=execution.violations,
            config_hash=config_hash,
            created_at=created_at,
        )
        return ClassifyRepairRoute(
            verdict=execution.verdict,
            attempts=execution.attempts,
            violations=execution.violations,
            repair_task_id=repair_task_id,
            repair_stream=repair_stream,
            output=execution.output,
        )

    signal_raw = finalize_signal_raw(
        execution.output,
        evidence,
        model_id=role_model_id,
        classify_task_id=classify_task_id,
    )
    try:
        signal = normalize_signal_raw(
            signal_raw,
            cohort_raw_values=cohort_raw_values,
            config_hash=config_hash,
            created_at=created_at,
            observed_at=observed_at or created_at,
        )
    except NormalizeError as exc:
        raise NormalizeError(
            f"normalize failed after valid classify (bug, not retry): {exc}"
        ) from exc

    artifact_id, artifact_inserted, edge_inserted = persist_signal_v1(
        conn,
        signal,
        evidence_ids=list(signal_raw["evidence_ids"]),
        classify_task_id=classify_task_id,
        storage_root=storage_root,
    )
    return ClassifySuccess(
        signal_raw=signal_raw,
        signal=signal,
        artifact_id=artifact_id,
        artifact_inserted=artifact_inserted,
        lineage_edge_inserted=edge_inserted,
        attempts=execution.attempts,
        replayed=bool(execution.replayed),
    )


def assert_law1_classify_output(raw: dict[str, Any]) -> None:
    """Prove LAW 1 split: classify emits labels/raw only, never scores."""
    validate_signal_raw(raw)


__all__ = [
    "CASSETTE_KIND",
    "CASSETTE_MODEL_ID",
    "CLASSIFY_ROLE",
    "ClassifyError",
    "ClassifyExecutionResult",
    "ClassifyRepairRoute",
    "ClassifySuccess",
    "PROMPT_VERSION",
    "assert_law1_classify_output",
    "build_replay_request",
    "classify_evidence",
    "finalize_signal_raw",
    "load_classify_prompt",
    "run_classify_normalize_task",
    "validate_signal_raw",
]
