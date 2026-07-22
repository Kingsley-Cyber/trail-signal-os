"""Active worker inspection routes."""

from __future__ import annotations

from typing import Annotated, Any

import psycopg
from fastapi import APIRouter, Depends

from control.api.deps import get_db

router = APIRouter(prefix="/v1/workers", tags=["workers"])


@router.get("")
def list_workers(
    conn: Annotated[psycopg.Connection, Depends(get_db)],
) -> dict[str, Any]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT lease_owner,
                   COUNT(*) AS active_tasks,
                   MAX(last_heartbeat_at) AS last_heartbeat_at
            FROM tasks
            WHERE lease_owner IS NOT NULL
              AND state IN ('LEASED', 'RUNNING')
            GROUP BY lease_owner
            ORDER BY lease_owner
            """
        )
        rows = cur.fetchall()
    workers = [
        {
            "worker_id": row[0],
            "active_tasks": row[1],
            "last_heartbeat_at": (
                row[2].isoformat().replace("+00:00", "Z") if row[2] is not None else None
            ),
        }
        for row in rows
    ]
    return {"workers": workers}
