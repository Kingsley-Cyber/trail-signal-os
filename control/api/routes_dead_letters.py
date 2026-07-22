"""Dead-letter inspection and requeue routes."""

from __future__ import annotations

from typing import Annotated, Any

import psycopg
from fastapi import APIRouter, Depends, HTTPException, Query, status

from control.api.auth import require_bearer
from control.api.deps import get_db

router = APIRouter(prefix="/v1/dead-letters", tags=["dead-letters"])


@router.get("")
def list_dead_letters(
    conn: Annotated[psycopg.Connection, Depends(get_db)],
) -> dict[str, Any]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT task_id, job_id, lane, attempt, completed_at, updated_at
            FROM tasks
            WHERE state = 'DEAD_LETTER'
            ORDER BY updated_at DESC
            """
        )
        rows = cur.fetchall()
    items = [
        {
            "task_id": row[0],
            "job_id": row[1],
            "lane": row[2],
            "attempt": row[3],
            "completed_at": (
                row[4].isoformat().replace("+00:00", "Z") if row[4] is not None else None
            ),
            "updated_at": row[5].isoformat().replace("+00:00", "Z"),
        }
        for row in rows
    ]
    return {"dead_letters": items}


@router.post("/{task_id}/requeue", dependencies=[Depends(require_bearer)])
def requeue_dead_letter(
    task_id: str,
    conn: Annotated[psycopg.Connection, Depends(get_db)],
    confirm: Annotated[bool, Query()] = False,
) -> dict[str, Any]:
    if not confirm:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="confirm=true is required for destructive requeue",
        )
    with conn.cursor() as cur:
        cur.execute(
            "SELECT state FROM tasks WHERE task_id = %s",
            (task_id,),
        )
        row = cur.fetchone()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="task not found")
    if row[0] != "DEAD_LETTER":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"task is not in DEAD_LETTER state: {row[0]}",
        )
    conn.execute(
        """
        UPDATE tasks
        SET state = 'READY',
            completed_at = NULL,
            lease_owner = NULL,
            lease_generation = 0,
            lease_expires_at = NULL,
            last_heartbeat_at = NULL,
            updated_at = NOW()
        WHERE task_id = %s
        """,
        (task_id,),
    )
    return {"task_id": task_id, "state": "READY", "requeued": True}
