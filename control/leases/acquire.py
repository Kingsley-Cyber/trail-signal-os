"""Acquire a Postgres task lease and bump the fencing generation."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

import psycopg


@dataclass(frozen=True)
class LeaseAcquireResult:
    task_id: str
    worker_id: str
    lease_generation: int
    lease_expires_at: object


def acquire_lease(
    conn: psycopg.Connection,
    *,
    task_id: str,
    worker_id: str,
    lease_duration: timedelta,
) -> LeaseAcquireResult | None:
    """Lease a READY task or reclaim an expired LEASED/RUNNING task."""
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE tasks
            SET
                state = 'LEASED',
                lease_owner = %s,
                lease_generation = lease_generation + 1,
                lease_expires_at = NOW() + %s,
                last_heartbeat_at = NOW(),
                updated_at = NOW()
            WHERE task_id = %s
              AND (
                  state = 'READY'
                  OR (
                      state IN ('LEASED', 'RUNNING')
                      AND lease_expires_at < NOW()
                  )
              )
            RETURNING lease_generation, lease_expires_at
            """,
            (worker_id, lease_duration, task_id),
        )
        row = cur.fetchone()
    if row is None:
        return None
    generation, expires_at = row
    return LeaseAcquireResult(
        task_id=task_id,
        worker_id=worker_id,
        lease_generation=generation,
        lease_expires_at=expires_at,
    )
