"""Extend an active task lease while the worker is still alive."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

import psycopg


@dataclass(frozen=True)
class HeartbeatResult:
    task_id: str
    worker_id: str
    lease_generation: int
    lease_expires_at: object


def heartbeat(
    conn: psycopg.Connection,
    *,
    task_id: str,
    worker_id: str,
    lease_generation: int,
    lease_duration: timedelta,
) -> HeartbeatResult | None:
    """Refresh lease_expires_at for the current owner and generation."""
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE tasks
            SET
                lease_expires_at = NOW() + %s,
                last_heartbeat_at = NOW(),
                updated_at = NOW()
            WHERE task_id = %s
              AND lease_owner = %s
              AND lease_generation = %s
              AND state IN ('LEASED', 'RUNNING')
              AND lease_expires_at >= NOW()
            RETURNING lease_expires_at
            """,
            (lease_duration, task_id, worker_id, lease_generation),
        )
        row = cur.fetchone()
    if row is None:
        return None
    return HeartbeatResult(
        task_id=task_id,
        worker_id=worker_id,
        lease_generation=lease_generation,
        lease_expires_at=row[0],
    )
