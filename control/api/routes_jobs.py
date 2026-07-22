"""Research job routes — create, inspect, lifecycle controls, dossier hierarchy."""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone
from typing import Annotated, Any

import psycopg
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from control.api.auth import require_bearer
from control.api.config_hash import current_config_hash
from control.api.deps import get_db
from graph.compiler import WorkflowCompileError, compile_workflow_yaml, persist_compiled_workflow
from lineage.edges import write_lineage_edge

router = APIRouter(prefix="/v1/research-jobs", tags=["jobs"])

DEFAULT_BUDGET: dict[str, Any] = {
    "max_queries": 10,
    "max_fetched_urls": 100,
    "per_domain_urls": 50,
    "browser_pages": 5,
    "media_items": 10,
    "max_bytes": 1048576,
    "deadline_minutes": 30,
    "max_attempts": 3,
    "llm_budget": {"max_calls": 10, "max_tokens": 10000, "max_usd": 0},
    "schema_version": "budget.v1",
}

DEFAULT_COLLECTION_SOURCE_CLASSES = ("open", "defended")
DOSSIER_EXPAND_KINDS = ("collection", "scoring", "decision")
VALIDATE_FANOUT_WORKFLOW_ID = "wf_validate_fanout"

VALIDATE_FANOUT_SUBGRAPH_YAML = """\
# VALIDATE fan-out sub-graph (control_plane_v4 §2 cp:validate, doc 07 nested graph)
workflow:
  id: wf_validate_fanout
  name: validate_fanout_subgraph
  version: "2026.07.21"

nodes:
  - id: validator
    kind: llm
    role: reason.primary
    input_schema: opportunity.v1
    output_schema: synthesis.v1
    prompt: prompts/validator.md
    cassette_kind: validate
    loop:
      max_iterations: 2
    verifier: claim_grounding

  - id: validation_gate
    kind: deterministic
    input_schema: synthesis.v1
    output_schema: synthesis.v1
    verifier: claim_grounding
    loop:
      max_iterations: 1

edges:
  - from: __start__
    to: validator
    edge_type: sequence

  - from: validator
    to: validation_gate
    edge_type: sequence

  - from: validation_gate
    to: __end__
    edge_type: sequence
"""


class CreateJobRequest(BaseModel):
    job_kind: str = Field(
        pattern="^(dossier|collection|scoring|validation|decision)$"
    )
    niche_id: str | None = None
    parent_job_id: str | None = Field(default=None, pattern="^job_[a-zA-Z0-9_-]+$")
    constraints_ref: str | None = None
    budget: dict[str, Any] | None = None
    job_id: str | None = Field(default=None, pattern="^job_[a-zA-Z0-9_-]+$")
    pain_point_id: str | None = Field(default=None, pattern="^pp_[a-zA-Z0-9_-]+$")
    opportunity_id: str | None = Field(default=None, pattern="^opp_[a-zA-Z0-9_-]+$")


class PainPointSpec(BaseModel):
    pain_point_id: str = Field(pattern="^pp_[a-zA-Z0-9_-]+$")
    record_ids: list[str] = Field(min_length=1)
    label: str | None = None


class ValidateFanoutRequest(BaseModel):
    pain_points: list[PainPointSpec] = Field(min_length=1)
    opportunity_id: str | None = Field(default=None, pattern="^opp_[a-zA-Z0-9_-]+$")


class ExpandDossierRequest(BaseModel):
    source_classes: list[str] | None = None


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace(
        "+00:00", "Z"
    )


def _job_row_to_dict(row: tuple[Any, ...], columns: list[str]) -> dict[str, Any]:
    data = dict(zip(columns, row, strict=True))
    for key in ("budget", "provenance"):
        if isinstance(data.get(key), str):
            data[key] = json.loads(data[key])
    for key in ("created_at", "updated_at", "as_of", "deadline_at"):
        value = data.get(key)
        if hasattr(value, "isoformat"):
            data[key] = value.isoformat().replace("+00:00", "Z")
    return data


_JOB_COLUMNS = [
    "job_id",
    "parent_job_id",
    "job_kind",
    "niche_id",
    "constraints_ref",
    "status",
    "config_hash",
    "budget",
    "as_of",
    "ttl_seconds",
    "deadline_at",
    "provenance",
    "created_at",
    "updated_at",
]


def _fetch_job(conn: psycopg.Connection, job_id: str) -> dict[str, Any] | None:
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT {", ".join(_JOB_COLUMNS)}
            FROM research_jobs
            WHERE job_id = %s
            """,
            (job_id,),
        )
        row = cur.fetchone()
    if row is None:
        return None
    return _job_row_to_dict(row, _JOB_COLUMNS)


def _fetch_children(conn: psycopg.Connection, parent_job_id: str) -> list[dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT {", ".join(_JOB_COLUMNS)}
            FROM research_jobs
            WHERE parent_job_id = %s
            ORDER BY created_at, job_id
            """,
            (parent_job_id,),
        )
        rows = cur.fetchall()
    return [_job_row_to_dict(row, _JOB_COLUMNS) for row in rows]


def _job_provenance(
    *,
    config_hash: str,
    created_at: str,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    provenance: dict[str, Any] = {
        "schema_version": "job.v1",
        "config_hash": config_hash,
        "created_at": created_at,
    }
    if extra:
        provenance.update(extra)
    return provenance


def _stable_child_job_id(parent_job_id: str, job_kind: str, suffix: str) -> str:
    digest = hashlib.sha256(f"{parent_job_id}|{job_kind}|{suffix}".encode()).hexdigest()[:12]
    return f"job_{digest}"


def _insert_research_job(
    conn: psycopg.Connection,
    *,
    job_id: str,
    job_kind: str,
    niche_id: str | None,
    parent_job_id: str | None,
    constraints_ref: str | None,
    budget: dict[str, Any],
    config_hash: str,
    provenance: dict[str, Any],
    status_value: str = "CREATED",
) -> dict[str, Any]:
    conn.execute(
        """
        INSERT INTO research_jobs (
            job_id,
            parent_job_id,
            job_kind,
            niche_id,
            constraints_ref,
            status,
            config_hash,
            budget,
            provenance
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s::jsonb)
        """,
        (
            job_id,
            parent_job_id,
            job_kind,
            niche_id,
            constraints_ref,
            status_value,
            config_hash,
            json.dumps(budget),
            json.dumps(provenance),
        ),
    )
    if parent_job_id is not None:
        write_lineage_edge(
            conn,
            child_kind="job",
            child_id=job_id,
            parent_kind="job",
            parent_id=parent_job_id,
            relation="spawned_from",
        )
    job = _fetch_job(conn, job_id)
    assert job is not None
    return job


def ensure_validate_fanout_subgraph(conn: psycopg.Connection) -> str:
    """Compile and persist the VALIDATE fan-out workflow once; return workflow_id."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT workflow_id FROM workflow_defs WHERE workflow_id = %s",
            (VALIDATE_FANOUT_WORKFLOW_ID,),
        )
        if cur.fetchone() is not None:
            return VALIDATE_FANOUT_WORKFLOW_ID

    try:
        compiled = compile_workflow_yaml(VALIDATE_FANOUT_SUBGRAPH_YAML)
    except WorkflowCompileError as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"validate fan-out sub-graph compile failed: {exc}",
        ) from exc
    persist_compiled_workflow(conn, compiled)
    return compiled.definition.workflow_id


def _create_workflow_run(
    conn: psycopg.Connection,
    *,
    job_id: str,
    workflow_id: str,
) -> str:
    run_id = f"run_{uuid.uuid4().hex[:12]}"
    conn.execute(
        """
        INSERT INTO workflow_runs (run_id, workflow_id, job_id, status)
        VALUES (%s, %s, %s, 'CREATED')
        """,
        (run_id, workflow_id, job_id),
    )
    write_lineage_edge(
        conn,
        child_kind="workflow_run",
        child_id=run_id,
        parent_kind="job",
        parent_id=job_id,
        relation="executes_subgraph",
    )
    return run_id


def shortlist_pain_points_from_opportunity(opportunity: dict[str, Any]) -> list[PainPointSpec]:
    """Derive pain-point fan-out specs from opportunity explanation citations."""
    explanation = opportunity.get("explanation")
    if not isinstance(explanation, dict):
        return []
    cited = explanation.get("cited_record_ids")
    if not isinstance(cited, list):
        return []
    pain_records = [
        record_id
        for record_id in cited
        if isinstance(record_id, str) and record_id.startswith("ev_") and "pain" in record_id
    ]
    if not pain_records:
        pain_records = [record_id for record_id in cited if isinstance(record_id, str)]
    if not pain_records:
        return []
    specs: list[PainPointSpec] = []
    for index, record_id in enumerate(pain_records, start=1):
        specs.append(
            PainPointSpec(
                pain_point_id=f"pp_{index:03d}",
                record_ids=[record_id],
                label=record_id,
            )
        )
    return specs


def expand_dossier_job(
    conn: psycopg.Connection,
    dossier_job_id: str,
    *,
    source_classes: tuple[str, ...] = DEFAULT_COLLECTION_SOURCE_CLASSES,
) -> dict[str, Any]:
    """Expand a dossier into collection, scoring, and decision child jobs."""
    dossier = _fetch_job(conn, dossier_job_id)
    if dossier is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="job not found")
    if dossier["job_kind"] != "dossier":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="only dossier jobs can be expanded",
        )

    existing = _fetch_children(conn, dossier_job_id)
    if existing:
        return {
            "job_id": dossier_job_id,
            "expanded": False,
            "children": existing,
        }

    config_hash = current_config_hash()
    created_at = _utc_now_iso()
    budget = dossier.get("budget") or DEFAULT_BUDGET
    niche_id = dossier.get("niche_id")
    constraints_ref = dossier.get("constraints_ref")
    children: list[dict[str, Any]] = []

    for source_class in source_classes:
        child_id = _stable_child_job_id(dossier_job_id, "collection", source_class)
        children.append(
            _insert_research_job(
                conn,
                job_id=child_id,
                job_kind="collection",
                niche_id=niche_id,
                parent_job_id=dossier_job_id,
                constraints_ref=constraints_ref,
                budget=budget,
                config_hash=config_hash,
                provenance=_job_provenance(
                    config_hash=config_hash,
                    created_at=created_at,
                    extra={"source_class": source_class},
                ),
            )
        )

    for job_kind in ("scoring", "decision"):
        child_id = _stable_child_job_id(dossier_job_id, job_kind, job_kind)
        children.append(
            _insert_research_job(
                conn,
                job_id=child_id,
                job_kind=job_kind,
                niche_id=niche_id,
                parent_job_id=dossier_job_id,
                constraints_ref=constraints_ref,
                budget=budget,
                config_hash=config_hash,
                provenance=_job_provenance(
                    config_hash=config_hash,
                    created_at=created_at,
                ),
            )
        )

    conn.execute(
        """
        UPDATE research_jobs
        SET status = 'PLANNING', updated_at = NOW()
        WHERE job_id = %s
        """,
        (dossier_job_id,),
    )

    return {
        "job_id": dossier_job_id,
        "expanded": True,
        "children": children,
    }


def validate_fanout(
    conn: psycopg.Connection,
    dossier_job_id: str,
    pain_points: list[PainPointSpec],
    *,
    opportunity_id: str | None = None,
) -> dict[str, Any]:
    """Fan out VALIDATE child jobs — one nested sub-graph run per shortlisted pain point."""
    dossier = _fetch_job(conn, dossier_job_id)
    if dossier is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="job not found")
    if dossier["job_kind"] != "dossier":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="validate fan-out requires a dossier job",
        )

    workflow_id = ensure_validate_fanout_subgraph(conn)
    config_hash = current_config_hash()
    created_at = _utc_now_iso()
    budget = dossier.get("budget") or DEFAULT_BUDGET
    niche_id = dossier.get("niche_id")
    constraints_ref = dossier.get("constraints_ref")

    validation_jobs: list[dict[str, Any]] = []
    workflow_runs: list[dict[str, Any]] = []

    for pain_point in pain_points:
        child_id = _stable_child_job_id(
            dossier_job_id,
            "validation",
            pain_point.pain_point_id,
        )
        existing_job = _fetch_job(conn, child_id)
        if existing_job is None:
            provenance = _job_provenance(
                config_hash=config_hash,
                created_at=created_at,
                extra={
                    "pain_point_id": pain_point.pain_point_id,
                    "pain_record_ids": pain_point.record_ids,
                    "validate_subgraph": workflow_id,
                },
            )
            if opportunity_id is not None:
                provenance["opportunity_id"] = opportunity_id
            if pain_point.label is not None:
                provenance["pain_label"] = pain_point.label
            job = _insert_research_job(
                conn,
                job_id=child_id,
                job_kind="validation",
                niche_id=niche_id,
                parent_job_id=dossier_job_id,
                constraints_ref=constraints_ref,
                budget=budget,
                config_hash=config_hash,
                provenance=provenance,
            )
            for record_id in pain_point.record_ids:
                write_lineage_edge(
                    conn,
                    child_kind="job",
                    child_id=child_id,
                    parent_kind="evidence",
                    parent_id=record_id,
                    relation="validates_pain",
                )
        else:
            job = existing_job

        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT run_id
                FROM workflow_runs
                WHERE job_id = %s AND workflow_id = %s
                ORDER BY started_at
                LIMIT 1
                """,
                (child_id, workflow_id),
            )
            existing_run = cur.fetchone()
        run_id = existing_run[0] if existing_run else _create_workflow_run(
            conn,
            job_id=child_id,
            workflow_id=workflow_id,
        )
        validation_jobs.append(job)
        workflow_runs.append(
            {
                "run_id": run_id,
                "workflow_id": workflow_id,
                "job_id": child_id,
                "pain_point_id": pain_point.pain_point_id,
            }
        )

    return {
        "job_id": dossier_job_id,
        "workflow_id": workflow_id,
        "validation_jobs": validation_jobs,
        "workflow_runs": workflow_runs,
    }


@router.post("", status_code=status.HTTP_201_CREATED, dependencies=[Depends(require_bearer)])
def create_research_job(
    body: CreateJobRequest,
    conn: Annotated[psycopg.Connection, Depends(get_db)],
) -> dict[str, Any]:
    job_id = body.job_id or f"job_{uuid.uuid4().hex[:12]}"
    budget = body.budget or DEFAULT_BUDGET
    config_hash = current_config_hash()
    created_at = _utc_now_iso()
    extra: dict[str, Any] = {}
    if body.pain_point_id is not None:
        extra["pain_point_id"] = body.pain_point_id
    if body.opportunity_id is not None:
        extra["opportunity_id"] = body.opportunity_id
    provenance = _job_provenance(config_hash=config_hash, created_at=created_at, extra=extra or None)

    if body.parent_job_id is not None and _fetch_job(conn, body.parent_job_id) is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"parent job not found: {body.parent_job_id}",
        )

    try:
        return _insert_research_job(
            conn,
            job_id=job_id,
            job_kind=body.job_kind,
            niche_id=body.niche_id,
            parent_job_id=body.parent_job_id,
            constraints_ref=body.constraints_ref,
            budget=budget,
            config_hash=config_hash,
            provenance=provenance,
        )
    except psycopg.errors.UniqueViolation as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"job already exists: {job_id}",
        ) from exc


@router.get("/{job_id}")
def get_research_job(
    job_id: str,
    conn: Annotated[psycopg.Connection, Depends(get_db)],
) -> dict[str, Any]:
    job = _fetch_job(conn, job_id)
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="job not found")
    return job


@router.get("/{job_id}/children")
def list_job_children(
    job_id: str,
    conn: Annotated[psycopg.Connection, Depends(get_db)],
) -> dict[str, Any]:
    if _fetch_job(conn, job_id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="job not found")
    children = _fetch_children(conn, job_id)
    return {"job_id": job_id, "children": children}


@router.post("/{job_id}/expand-dossier", dependencies=[Depends(require_bearer)])
def expand_dossier(
    job_id: str,
    conn: Annotated[psycopg.Connection, Depends(get_db)],
    body: ExpandDossierRequest | None = None,
) -> dict[str, Any]:
    source_classes = DEFAULT_COLLECTION_SOURCE_CLASSES
    if body is not None and body.source_classes:
        source_classes = tuple(body.source_classes)
    return expand_dossier_job(conn, job_id, source_classes=source_classes)


@router.post("/{job_id}/validate-fanout", dependencies=[Depends(require_bearer)])
def validate_fanout_route(
    job_id: str,
    body: ValidateFanoutRequest,
    conn: Annotated[psycopg.Connection, Depends(get_db)],
) -> dict[str, Any]:
    return validate_fanout(
        conn,
        job_id,
        body.pain_points,
        opportunity_id=body.opportunity_id,
    )


@router.get("/{job_id}/tasks")
def list_job_tasks(
    job_id: str,
    conn: Annotated[psycopg.Connection, Depends(get_db)],
) -> dict[str, Any]:
    if _fetch_job(conn, job_id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="job not found")
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT task_id, lane, state, priority, attempt, created_at, updated_at
            FROM tasks
            WHERE job_id = %s
            ORDER BY created_at
            """,
            (job_id,),
        )
        rows = cur.fetchall()
    tasks = [
        {
            "task_id": row[0],
            "lane": row[1],
            "state": row[2],
            "priority": row[3],
            "attempt": row[4],
            "created_at": row[5].isoformat().replace("+00:00", "Z"),
            "updated_at": row[6].isoformat().replace("+00:00", "Z"),
        }
        for row in rows
    ]
    return {"job_id": job_id, "tasks": tasks}


def _set_job_status(
    conn: psycopg.Connection,
    job_id: str,
    status_value: str,
) -> dict[str, Any]:
    job = _fetch_job(conn, job_id)
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="job not found")
    conn.execute(
        """
        UPDATE research_jobs
        SET status = %s, updated_at = NOW()
        WHERE job_id = %s
        """,
        (status_value, job_id),
    )
    updated = _fetch_job(conn, job_id)
    assert updated is not None
    return updated


@router.post("/{job_id}/pause", dependencies=[Depends(require_bearer)])
def pause_research_job(
    job_id: str,
    conn: Annotated[psycopg.Connection, Depends(get_db)],
) -> dict[str, Any]:
    return _set_job_status(conn, job_id, "PAUSED")


@router.post("/{job_id}/resume", dependencies=[Depends(require_bearer)])
def resume_research_job(
    job_id: str,
    conn: Annotated[psycopg.Connection, Depends(get_db)],
) -> dict[str, Any]:
    return _set_job_status(conn, job_id, "DISCOVERING")


@router.post("/{job_id}/cancel", dependencies=[Depends(require_bearer)])
def cancel_research_job(
    job_id: str,
    conn: Annotated[psycopg.Connection, Depends(get_db)],
) -> dict[str, Any]:
    return _set_job_status(conn, job_id, "CANCEL_REQUESTED")
