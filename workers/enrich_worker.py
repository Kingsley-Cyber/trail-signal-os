"""Enrich worker — page.v1 → evidence.v1 via gateway cassette replay (N20)."""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import psycopg
from jsonschema import Draft202012Validator

from control.dispatcher import enqueue_ready_task
from db.repositories.constraints import insert_lineage_edge_idempotent
from db.repositories.persist_artifact import persist_artifact
from fixtures.load import SCHEMAS_DIR
from guards.runtime_guards import guard6_require_lineage_edge
from harness.gateway import GatewayMode, LLMGateway
from harness.node_executor import (
    NodeDefinition,
    NodeExecutionResult,
    NodeKind,
    execute_node,
    schema_validate_verifier,
    validate_packed_input,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
PROMPT_PATH = REPO_ROOT / "prompts" / "enrich_page.md"

ENRICH_ROLE = "enrich.primary"
CASSETTE_KIND = "enrich"
PROMPT_VERSION = "enrich_page-2026.07.21"
CODE_VERSION = "enrich_worker-1.0.0"
EVIDENCE_SCHEMA_VERSION = "evidence.v1"
MAX_ITERATIONS = 2

# Recorded in fixtures/cassettes/enrich; replay lookup keys on this model_id string.
CASSETTE_MODEL_ID = "qwen3-4b-q4"

REPAIR_LANE = "extract"
REPAIR_STREAM_NAME = "cp:extract:repair"
INDEX_LANE = "index"


@dataclass(frozen=True)
class EnrichSuccess:
    evidence: dict[str, Any]
    artifact_id: str
    artifact_inserted: bool
    lineage_edge_inserted: bool
    attempts: int
    replayed: bool


@dataclass(frozen=True)
class EnrichRepairRoute:
    verdict: str
    attempts: int
    violations: tuple[str, ...]
    repair_task_id: str
    repair_stream: str
    output: dict[str, Any] | None


def load_enrich_prompt() -> str:
    if not PROMPT_PATH.is_file():
        raise FileNotFoundError(f"missing enrich prompt {PROMPT_PATH}")
    return PROMPT_PATH.read_text(encoding="utf-8")


def _load_evidence_schema() -> dict[str, Any]:
    return json.loads((SCHEMAS_DIR / "evidence.v1.schema.json").read_text(encoding="utf-8"))


def validate_evidence_v1(evidence: dict[str, Any]) -> None:
    Draft202012Validator(_load_evidence_schema()).validate(evidence)


def build_replay_request(
    page: dict[str, Any],
    *,
    model_id: str = CASSETTE_MODEL_ID,
) -> dict[str, Any]:
    """Build cassette replay key fields for offline enrich cassettes."""
    return {
        "page_id": page["page_id"],
        "prompt_version": PROMPT_VERSION,
        "model_id": model_id,
    }


def build_node_definition(*, verifier=None) -> NodeDefinition:
    return NodeDefinition(
        node_id="enrich_page",
        kind=NodeKind.LLM,
        role=ENRICH_ROLE,
        input_schema="page.v1",
        output_schema=EVIDENCE_SCHEMA_VERSION,
        prompt=load_enrich_prompt(),
        cassette_kind=CASSETTE_KIND,
        max_iterations=MAX_ITERATIONS,
        verifier=verifier or schema_validate_verifier(EVIDENCE_SCHEMA_VERSION),
    )


def content_hash_for_evidence(evidence: dict[str, Any]) -> str:
    canonical = json.dumps(
        {
            "observation": evidence.get("observation"),
            "source": evidence.get("source"),
            "evidence_type": evidence.get("evidence_type"),
            "polarity": evidence.get("polarity"),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def finalize_evidence(
    raw: dict[str, Any],
    page: dict[str, Any],
    *,
    config_hash: str,
    created_at: str,
    model_id: str,
    enrich_task_id: str,
) -> dict[str, Any]:
    evidence = dict(raw)
    page_id = page["page_id"]
    derived_from = list(evidence.get("derived_from") or [])
    if page_id not in derived_from:
        derived_from.insert(0, page_id)
    evidence["derived_from"] = derived_from

    if not evidence.get("content_hash"):
        evidence["content_hash"] = content_hash_for_evidence(evidence)

    evidence["extraction"] = {
        "model_id": model_id,
        "prompt_version": PROMPT_VERSION,
        "role": ENRICH_ROLE,
        **(evidence.get("extraction") or {}),
    }
    evidence["provenance"] = {
        "model_id": model_id,
        "prompt_version": PROMPT_VERSION,
        "schema_version": EVIDENCE_SCHEMA_VERSION,
        "config_hash": config_hash,
        "created_at": created_at,
    }
    evidence["schema_version"] = EVIDENCE_SCHEMA_VERSION
    validate_evidence_v1(evidence)
    return evidence


def _lineage_edge_exists(
    conn: psycopg.Connection,
    *,
    record_id: str,
    page_id: str,
) -> bool:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT 1
            FROM lineage_edges
            WHERE child_kind = %s
              AND child_id = %s
              AND parent_kind = 'page'
              AND parent_id = %s
              AND relation = 'derived_from'
            """,
            (EVIDENCE_SCHEMA_VERSION, record_id, page_id),
        )
        return cur.fetchone() is not None


def persist_evidence_v1(
    conn: psycopg.Connection,
    evidence: dict[str, Any],
    *,
    page_id: str,
    enrich_task_id: str,
    storage_root: Path | None = None,
) -> tuple[str, bool, bool]:
    validate_evidence_v1(evidence)
    artifact_inserted = persist_artifact(
        conn,
        artifact_id=evidence["record_id"],
        content_hash=evidence["content_hash"],
        artifact_kind=EVIDENCE_SCHEMA_VERSION,
        payload=evidence,
        derived_from=list(evidence["derived_from"]),
        provenance=evidence["provenance"],
        created_by_task=enrich_task_id,
        schema_version=EVIDENCE_SCHEMA_VERSION,
        storage_root=storage_root,
    )
    edge_inserted = insert_lineage_edge_idempotent(
        conn,
        child_kind=EVIDENCE_SCHEMA_VERSION,
        child_id=evidence["record_id"],
        parent_kind="page",
        parent_id=page_id,
        relation="derived_from",
        version_tag=CODE_VERSION,
    )
    guard6_require_lineage_edge(
        parent_refs=evidence["derived_from"],
        lineage_edge_written=edge_inserted
        or _lineage_edge_exists(conn, record_id=evidence["record_id"], page_id=page_id),
    )
    return evidence["record_id"], artifact_inserted, edge_inserted


def enqueue_repair_task(
    conn: psycopg.Connection,
    *,
    job_id: str,
    page_id: str,
    enrich_task_id: str,
    violations: tuple[str, ...],
    config_hash: str,
    created_at: str,
) -> tuple[str, str]:
    """Route failed enrich to cp:extract:repair — never the index lane (doc 07 §2, doc 09 Gate 3)."""
    repair_task_id = f"tsk_repair_{uuid.uuid4().hex[:12]}"
    idempotency_key = f"sha256:{hashlib.sha256(f'{enrich_task_id}:repair'.encode()).hexdigest()}"
    payload_ref = json.dumps(
        {
            "page_id": page_id,
            "enrich_task_id": enrich_task_id,
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


def enqueue_index_task(
    conn: psycopg.Connection,
    *,
    job_id: str,
    record_id: str,
    enrich_task_id: str,
    config_hash: str,
    created_at: str,
) -> str:
    """Enqueue index lane task (N21 consumer). Not used on enrich repair path."""
    index_task_id = f"tsk_index_{uuid.uuid4().hex[:12]}"
    idempotency_key = f"sha256:{hashlib.sha256(f'{record_id}:index'.encode()).hexdigest()}"
    payload_ref = json.dumps({"record_id": record_id, "enrich_task_id": enrich_task_id}, sort_keys=True)
    provenance = {
        "schema_version": "task.v1",
        "config_hash": config_hash,
        "created_at": created_at,
        "code_version": CODE_VERSION,
    }
    enqueue_ready_task(
        conn,
        task_id=index_task_id,
        job_id=job_id,
        lane=INDEX_LANE,
        idempotency_key=idempotency_key,
        payload_ref=payload_ref,
        provenance=provenance,
    )
    return index_task_id


def enrich_page(
    page: dict[str, Any],
    *,
    gateway: LLMGateway | None = None,
    replay_request: dict[str, Any] | None = None,
    model_id: str = CASSETTE_MODEL_ID,
    verifier=None,
) -> NodeExecutionResult:
    """Run enrich loop (attempt + repair reprompt) via gateway cassette replay."""
    validate_packed_input(page, "page.v1")
    request = replay_request or build_replay_request(page, model_id=model_id)
    node = build_node_definition(verifier=verifier)
    return execute_node(
        node,
        page,
        gateway=gateway or LLMGateway(mode=GatewayMode.REPLAY),
        replay_request=request,
    )


def run_enrich_task(
    conn: psycopg.Connection,
    *,
    job_id: str,
    page: dict[str, Any],
    enrich_task_id: str,
    config_hash: str,
    created_at: str,
    gateway: LLMGateway | None = None,
    replay_request: dict[str, Any] | None = None,
    storage_root: Path | None = None,
    enqueue_index_on_success: bool = False,
) -> EnrichSuccess | EnrichRepairRoute:
    """Persist validated evidence with lineage, or route invalid output to repair — not index."""
    llm_gateway = gateway or LLMGateway(mode=GatewayMode.REPLAY)
    role_model_id = llm_gateway.resolve_role(ENRICH_ROLE).model_id
    execution = enrich_page(
        page,
        gateway=llm_gateway,
        replay_request=replay_request,
        model_id=role_model_id if llm_gateway.mode is GatewayMode.LIVE else CASSETTE_MODEL_ID,
    )

    if execution.verdict != "pass" or execution.output is None:
        repair_task_id, repair_stream = enqueue_repair_task(
            conn,
            job_id=job_id,
            page_id=page["page_id"],
            enrich_task_id=enrich_task_id,
            violations=execution.violations,
            config_hash=config_hash,
            created_at=created_at,
        )
        return EnrichRepairRoute(
            verdict=execution.verdict,
            attempts=execution.attempts,
            violations=execution.violations,
            repair_task_id=repair_task_id,
            repair_stream=repair_stream,
            output=execution.output,
        )

    evidence = finalize_evidence(
        execution.output,
        page,
        config_hash=config_hash,
        created_at=created_at,
        model_id=role_model_id,
        enrich_task_id=enrich_task_id,
    )
    artifact_id, artifact_inserted, edge_inserted = persist_evidence_v1(
        conn,
        evidence,
        page_id=page["page_id"],
        enrich_task_id=enrich_task_id,
        storage_root=storage_root,
    )
    if enqueue_index_on_success:
        enqueue_index_task(
            conn,
            job_id=job_id,
            record_id=evidence["record_id"],
            enrich_task_id=enrich_task_id,
            config_hash=config_hash,
            created_at=created_at,
        )
    return EnrichSuccess(
        evidence=evidence,
        artifact_id=artifact_id,
        artifact_inserted=artifact_inserted,
        lineage_edge_inserted=edge_inserted,
        attempts=execution.attempts,
        replayed=bool(execution.replayed),
    )
