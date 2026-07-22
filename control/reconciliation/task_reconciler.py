"""Reclaim lease inconsistencies and flag task/artifact linkage gaps."""

from __future__ import annotations

from dataclasses import dataclass

import psycopg

from control.leases.reaper import reclaim_expired_leases


@dataclass(frozen=True)
class TaskInconsistency:
    task_id: str
    issue: str


def reclaim_task_inconsistencies(
    conn: psycopg.Connection,
    *,
    limit: int = 100,
) -> list[str]:
    """Move expired LEASED/RUNNING tasks back to READY."""
    return reclaim_expired_leases(conn, limit=limit)


def flag_task_inconsistencies(
    conn: psycopg.Connection,
    *,
    limit: int = 100,
) -> list[TaskInconsistency]:
    """Flag tasks whose result artifact pointer is missing or mismatched."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT t.task_id, 'missing_result_artifact'
            FROM tasks t
            WHERE t.state = 'SUCCEEDED'
              AND t.result_artifact_id IS NULL
            ORDER BY t.updated_at DESC
            LIMIT %s
            """,
            (limit,),
        )
        missing = [
            TaskInconsistency(task_id=row[0], issue=row[1]) for row in cur.fetchall()
        ]

        cur.execute(
            """
            SELECT t.task_id, 'result_artifact_not_found'
            FROM tasks t
            LEFT JOIN artifacts a ON a.artifact_id = t.result_artifact_id
            WHERE t.result_artifact_id IS NOT NULL
              AND a.artifact_id IS NULL
            ORDER BY t.updated_at DESC
            LIMIT %s
            """,
            (limit,),
        )
        dangling = [
            TaskInconsistency(task_id=row[0], issue=row[1]) for row in cur.fetchall()
        ]

    return missing + dangling
