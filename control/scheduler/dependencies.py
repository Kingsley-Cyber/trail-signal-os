"""Resolve task dependencies and retry eligibility."""

from __future__ import annotations

import psycopg

from control.scheduler.fairness import AdmissionCandidate


def fetch_admission_candidates(
    conn: psycopg.Connection,
    *,
    lane: str | None = None,
) -> list[AdmissionCandidate]:
    """Return PENDING (deps satisfied) and due RETRY_WAIT tasks eligible for admission."""
    params: list[object] = []
    lane_clause = ""
    if lane is not None:
        lane_clause = "AND t.lane = %s"
        params.append(lane)

    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT t.task_id, t.job_id, t.lane, t.priority, t.created_at
            FROM tasks t
            WHERE (
                (
                    t.state = 'PENDING'
                    AND (t.not_before IS NULL OR t.not_before <= NOW())
                    AND NOT EXISTS (
                        SELECT 1
                        FROM task_dependencies d
                        JOIN tasks dep ON dep.task_id = d.depends_on_task_id
                        WHERE d.task_id = t.task_id
                          AND dep.state <> 'SUCCEEDED'
                    )
                )
                OR (
                    t.state = 'RETRY_WAIT'
                    AND t.retry_at IS NOT NULL
                    AND t.retry_at <= NOW()
                )
            )
            {lane_clause}
            ORDER BY t.priority ASC, t.created_at ASC, t.task_id ASC
            """,
            params,
        )
        rows = cur.fetchall()

    return [
        AdmissionCandidate(
            task_id=row[0],
            job_id=row[1],
            lane=row[2],
            priority=row[3],
            created_at=row[4],
        )
        for row in rows
    ]
