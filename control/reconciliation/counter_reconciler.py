"""Reconcile job status against actual task-state histograms."""

from __future__ import annotations

from dataclasses import dataclass

import psycopg

TERMINAL_JOB_STATUSES = frozenset(
    {
        "COMPLETED",
        "CANCELLED",
        "FAILED",
        "COMPLETED_WITH_GAPS",
    }
)


@dataclass(frozen=True)
class JobCounterMismatch:
    job_id: str
    job_status: str
    issue: str


def flag_counter_mismatches(
    conn: psycopg.Connection,
    *,
    limit: int = 100,
) -> list[JobCounterMismatch]:
    """Flag jobs whose status disagrees with underlying task states."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT j.job_id, j.status
            FROM research_jobs j
            WHERE j.status IN ('COMPLETED', 'CANCELLED', 'FAILED', 'COMPLETED_WITH_GAPS')
              AND EXISTS (
                  SELECT 1
                  FROM tasks t
                  WHERE t.job_id = j.job_id
                    AND t.state IN ('READY', 'LEASED', 'RUNNING', 'RETRY_WAIT', 'PENDING')
              )
            ORDER BY j.updated_at DESC
            LIMIT %s
            """,
            (limit,),
        )
        active_tasks_on_terminal_job = [
            JobCounterMismatch(
                job_id=row[0],
                job_status=row[1],
                issue="terminal_job_has_active_tasks",
            )
            for row in cur.fetchall()
        ]

        cur.execute(
            """
            SELECT j.job_id, j.status
            FROM research_jobs j
            WHERE j.status IN ('CREATED', 'PLANNING', 'DISCOVERING', 'ACQUIRING', 'EXTRACTING', 'INDEXING', 'SYNTHESIZING')
              AND NOT EXISTS (
                  SELECT 1
                  FROM tasks t
                  WHERE t.job_id = j.job_id
                    AND t.state NOT IN ('SUCCEEDED', 'FAILED', 'DEAD_LETTER', 'BLOCKED')
              )
              AND EXISTS (
                  SELECT 1
                  FROM tasks t
                  WHERE t.job_id = j.job_id
              )
            ORDER BY j.updated_at DESC
            LIMIT %s
            """,
            (limit,),
        )
        stale_active_job = [
            JobCounterMismatch(
                job_id=row[0],
                job_status=row[1],
                issue="active_job_all_tasks_terminal",
            )
            for row in cur.fetchall()
        ]

    return active_tasks_on_terminal_job + stale_active_job
