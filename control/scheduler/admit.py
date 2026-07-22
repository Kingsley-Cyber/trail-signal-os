"""Promote eligible tasks to READY and enqueue outbox rows."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone

import psycopg

from control.dispatcher.streams import resolve_stream_name
from control.scheduler.backpressure import (
    BackpressureGate,
    BackpressureState,
    fetch_admission_allowed,
    measure_backpressure,
)
from control.scheduler.budgets import check_lane_budget
from control.scheduler.concurrency import check_lane_concurrency
from control.scheduler.dependencies import fetch_admission_candidates
from control.scheduler.fairness import select_fair_batch


@dataclass(frozen=True)
class AdmissionResult:
    task_id: str
    admitted: bool
    reason: str
    event_id: int | None = None
    stream_name: str | None = None


@dataclass(frozen=True)
class AdmissionTickResult:
    examined: int
    admitted: list[AdmissionResult]
    denied: list[AdmissionResult]


def _dependencies_satisfied(conn: psycopg.Connection, task_id: str) -> bool:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT NOT EXISTS (
                SELECT 1
                FROM task_dependencies d
                JOIN tasks dep ON dep.task_id = d.depends_on_task_id
                WHERE d.task_id = %s
                  AND dep.state <> 'SUCCEEDED'
            )
            """,
            (task_id,),
        )
        row = cur.fetchone()
    assert row is not None
    return bool(row[0])


def _stream_payload(
    *,
    task_id: str,
    job_id: str,
    lane: str,
    priority: int,
    attempt: int,
    idempotency_key: str,
    payload_ref: str,
    traceparent: str | None,
    created_at: str,
) -> dict:
    payload = {
        "task_id": task_id,
        "job_id": job_id,
        "lane": lane,
        "priority": priority,
        "attempt": attempt,
        "idempotency_key": idempotency_key,
        "payload_ref": payload_ref,
        "created_at": created_at,
    }
    if traceparent is not None:
        payload["traceparent"] = traceparent
    return payload


def admit_task(
    conn: psycopg.Connection,
    *,
    task_id: str,
    backpressure: BackpressureState | None = None,
    backpressure_gate: BackpressureGate | None = None,
) -> AdmissionResult:
    """Admit one task under budget and backpressure gates."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT
                t.task_id,
                t.job_id,
                t.lane,
                t.priority,
                t.attempt,
                t.state,
                t.idempotency_key,
                t.payload_ref,
                t.traceparent,
                t.created_at
            FROM tasks t
            WHERE t.task_id = %s
            FOR UPDATE
            """,
            (task_id,),
        )
        row = cur.fetchone()

    if row is None:
        return AdmissionResult(task_id=task_id, admitted=False, reason="task_not_found")

    (
        _task_id,
        job_id,
        lane,
        priority,
        attempt,
        state,
        idempotency_key,
        payload_ref,
        traceparent,
        created_at,
    ) = row

    if state not in ("PENDING", "RETRY_WAIT"):
        return AdmissionResult(task_id=task_id, admitted=False, reason=f"state_{state.lower()}")

    if state == "PENDING" and not _dependencies_satisfied(conn, task_id):
        return AdmissionResult(task_id=task_id, admitted=False, reason="dependencies_unsatisfied")

    if not fetch_admission_allowed(
        conn,
        lane=lane,
        backpressure=backpressure,
        gate=backpressure_gate,
    ):
        return AdmissionResult(task_id=task_id, admitted=False, reason="backpressure_paused")

    budget = check_lane_budget(conn, job_id=job_id, lane=lane)
    if not budget.allowed:
        return AdmissionResult(task_id=task_id, admitted=False, reason=budget.reason)

    concurrency = check_lane_concurrency(conn, lane=lane)
    if not concurrency.allowed:
        return AdmissionResult(task_id=task_id, admitted=False, reason=concurrency.reason)

    next_attempt = attempt + 1 if state == "RETRY_WAIT" else attempt
    created = (
        created_at.isoformat()
        if hasattr(created_at, "isoformat")
        else str(created_at)
    )
    stream_name = resolve_stream_name(lane, priority)
    payload = _stream_payload(
        task_id=task_id,
        job_id=job_id,
        lane=lane,
        priority=priority,
        attempt=next_attempt,
        idempotency_key=idempotency_key,
        payload_ref=payload_ref,
        traceparent=traceparent,
        created_at=created,
    )

    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE tasks
                SET
                    state = 'READY',
                    attempt = %s,
                    retry_at = NULL,
                    updated_at = NOW()
                WHERE task_id = %s
                  AND state IN ('PENDING', 'RETRY_WAIT')
                RETURNING task_id
                """,
                (next_attempt, task_id),
            )
            if cur.fetchone() is None:
                return AdmissionResult(
                    task_id=task_id,
                    admitted=False,
                    reason="state_changed",
                )
            cur.execute(
                """
                INSERT INTO outbox_events (task_id, stream_name, payload)
                VALUES (%s, %s, %s::jsonb)
                RETURNING event_id
                """,
                (task_id, stream_name, json.dumps(payload)),
            )
            event_row = cur.fetchone()

    if event_row is None:
        raise RuntimeError("outbox insert did not return event_id")
    return AdmissionResult(
        task_id=task_id,
        admitted=True,
        reason="admitted",
        event_id=event_row[0],
        stream_name=stream_name,
    )


def run_admission_tick(
    conn: psycopg.Connection,
    *,
    batch_limit: int = 32,
    lane: str | None = None,
    backpressure_gate: BackpressureGate | None = None,
) -> AdmissionTickResult:
    """Resolve dependencies, apply fairness, and admit READY work under budgets."""
    gate = backpressure_gate if backpressure_gate is not None else BackpressureGate()
    backpressure = measure_backpressure(conn, gate=gate)
    candidates = fetch_admission_candidates(conn, lane=lane)
    selected = select_fair_batch(candidates, batch_limit=batch_limit)

    admitted: list[AdmissionResult] = []
    denied: list[AdmissionResult] = []
    for candidate in selected:
        result = admit_task(
            conn,
            task_id=candidate.task_id,
            backpressure=backpressure,
            backpressure_gate=gate,
        )
        if result.admitted:
            admitted.append(result)
        else:
            denied.append(result)

    return AdmissionTickResult(
        examined=len(selected),
        admitted=admitted,
        denied=denied,
    )


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()
