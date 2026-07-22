"""Insert READY task and outbox row in one Postgres transaction."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone

import psycopg

from control.dispatcher.streams import resolve_stream_name


@dataclass(frozen=True)
class OutboxEnqueueResult:
    task_id: str
    event_id: int
    stream_name: str
    payload: dict


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


def enqueue_ready_task(
    conn: psycopg.Connection,
    *,
    task_id: str,
    job_id: str,
    lane: str,
    idempotency_key: str,
    payload_ref: str,
    provenance: dict,
    priority: int = 2,
    attempt: int = 1,
    traceparent: str | None = None,
    created_at: str | None = None,
) -> OutboxEnqueueResult:
    """Atomically insert a READY task and its outbox event (guard 4 runtime)."""
    created = created_at or datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    stream_name = resolve_stream_name(lane, priority)
    payload = _stream_payload(
        task_id=task_id,
        job_id=job_id,
        lane=lane,
        priority=priority,
        attempt=attempt,
        idempotency_key=idempotency_key,
        payload_ref=payload_ref,
        traceparent=traceparent,
        created_at=created,
    )
    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO tasks (
                    task_id,
                    job_id,
                    lane,
                    priority,
                    attempt,
                    state,
                    idempotency_key,
                    payload_ref,
                    traceparent,
                    provenance
                )
                VALUES (%s, %s, %s, %s, %s, 'READY', %s, %s, %s, %s::jsonb)
                RETURNING task_id
                """,
                (
                    task_id,
                    job_id,
                    lane,
                    priority,
                    attempt,
                    idempotency_key,
                    payload_ref,
                    traceparent,
                    json.dumps(provenance),
                ),
            )
            cur.execute(
                """
                INSERT INTO outbox_events (task_id, stream_name, payload)
                VALUES (%s, %s, %s::jsonb)
                RETURNING event_id
                """,
                (task_id, stream_name, json.dumps(payload)),
            )
            row = cur.fetchone()
    if row is None:
        raise RuntimeError("outbox insert did not return event_id")
    event_id = row[0]
    return OutboxEnqueueResult(
        task_id=task_id,
        event_id=event_id,
        stream_name=stream_name,
        payload=payload,
    )
