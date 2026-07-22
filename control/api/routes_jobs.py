"""Research job routes — create, inspect, lifecycle controls."""

from __future__ import annotations

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


class CreateJobRequest(BaseModel):
    job_kind: str = Field(
        pattern="^(dossier|collection|scoring|validation|decision)$"
    )
    niche_id: str | None = None
    budget: dict[str, Any] | None = None
    job_id: str | None = Field(default=None, pattern="^job_[a-zA-Z0-9_-]+$")


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace(
        "+00:00", "Z"
    )


def _job_row_to_dict(row: tuple[Any, ...], columns: list[str]) -> dict[str, Any]:
    data = dict(zip(columns, row, strict=True))
    for key in ("budget", "provenance"):
        if isinstance(data.get(key), str):
            data[key] = json.loads(data[key])
    return data


def _fetch_job(conn: psycopg.Connection, job_id: str) -> dict[str, Any] | None:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT job_id, job_kind, niche_id, status, config_hash, budget, provenance,
                   created_at, updated_at
            FROM research_jobs
            WHERE job_id = %s
            """,
            (job_id,),
        )
        row = cur.fetchone()
    if row is None:
        return None
    return _job_row_to_dict(
        row,
        [
            "job_id",
            "job_kind",
            "niche_id",
            "status",
            "config_hash",
            "budget",
            "provenance",
            "created_at",
            "updated_at",
        ],
    )


@router.post("", status_code=status.HTTP_201_CREATED, dependencies=[Depends(require_bearer)])
def create_research_job(
    body: CreateJobRequest,
    conn: Annotated[psycopg.Connection, Depends(get_db)],
) -> dict[str, Any]:
    job_id = body.job_id or f"job_{uuid.uuid4().hex[:12]}"
    budget = body.budget or DEFAULT_BUDGET
    config_hash = current_config_hash()
    created_at = _utc_now_iso()
    provenance = {
        "schema_version": "job.v1",
        "config_hash": config_hash,
        "created_at": created_at,
    }
    try:
        conn.execute(
            """
            INSERT INTO research_jobs (
                job_id, job_kind, niche_id, status, config_hash, budget, provenance
            )
            VALUES (%s, %s, %s, 'CREATED', %s, %s::jsonb, %s::jsonb)
            """,
            (
                job_id,
                body.job_kind,
                body.niche_id,
                config_hash,
                json.dumps(budget),
                json.dumps(provenance),
            ),
        )
    except psycopg.errors.UniqueViolation as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"job already exists: {job_id}",
        ) from exc
    job = _fetch_job(conn, job_id)
    assert job is not None
    return job


@router.get("/{job_id}")
def get_research_job(
    job_id: str,
    conn: Annotated[psycopg.Connection, Depends(get_db)],
) -> dict[str, Any]:
    job = _fetch_job(conn, job_id)
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="job not found")
    return job


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
