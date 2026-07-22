"""Reclaim tasks whose worker leases have expired."""

from __future__ import annotations

import psycopg


def reclaim_expired_leases(
    conn: psycopg.Connection,
    *,
    limit: int = 100,
) -> list[str]:
    """Move expired LEASED/RUNNING tasks back to READY for redispatch."""
    with conn.cursor() as cur:
        cur.execute(
            """
            WITH expired AS (
                SELECT task_id
                FROM tasks
                WHERE state IN ('LEASED', 'RUNNING')
                  AND lease_expires_at IS NOT NULL
                  AND lease_expires_at < NOW()
                ORDER BY lease_expires_at
                LIMIT %s
                FOR UPDATE SKIP LOCKED
            )
            UPDATE tasks AS t
            SET
                state = 'READY',
                lease_owner = NULL,
                lease_expires_at = NULL,
                last_heartbeat_at = NULL,
                updated_at = NOW()
            FROM expired
            WHERE t.task_id = expired.task_id
            RETURNING t.task_id
            """,
            (limit,),
        )
        return [row[0] for row in cur.fetchall()]
