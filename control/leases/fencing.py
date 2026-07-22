"""Fenced task state updates — guard 2 integration."""

from __future__ import annotations

from typing import Any

import psycopg

from guards.runtime_guards import guard2_require_fenced_update

TERMINAL_STATES = frozenset({"SUCCEEDED", "FAILED", "BLOCKED", "DEAD_LETTER"})


def update_task_fenced(
    conn: psycopg.Connection,
    *,
    task_id: str,
    worker_id: str,
    lease_generation: int,
    new_state: str,
    result_artifact_id: str | None = None,
    extra_sets: dict[str, Any] | None = None,
) -> None:
    """Apply a lease-fenced task update; stale generation raises StaleLeaseError."""
    assignments = ["state = %s", "updated_at = NOW()"]
    params: list[Any] = [new_state]

    if result_artifact_id is not None:
        assignments.append("result_artifact_id = %s")
        params.append(result_artifact_id)

    if new_state in TERMINAL_STATES:
        assignments.append("completed_at = NOW()")

    if extra_sets:
        for column, value in extra_sets.items():
            assignments.append(f"{column} = %s")
            params.append(value)

    params.extend([task_id, worker_id, lease_generation])
    sql = f"""
        UPDATE tasks
        SET {", ".join(assignments)}
        WHERE task_id = %s
          AND lease_owner = %s
          AND lease_generation = %s
    """
    with conn.cursor() as cur:
        cur.execute(sql, params)
        rows_updated = cur.rowcount

    guard2_require_fenced_update(
        rows_updated,
        expected_owner=worker_id,
        actual_owner=worker_id,
    )
