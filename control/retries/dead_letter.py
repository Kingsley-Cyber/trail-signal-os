"""Dead-letter routing for exhausted or non-retryable tasks."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

import psycopg


@dataclass(frozen=True)
class DeadLetterResult:
    task_id: str
    previous_state: str
    failure_class: str
    reason: str


def send_to_dead_letter(
    conn: psycopg.Connection,
    *,
    task_id: str,
    failure_class: str,
    reason: str = "max_attempts_exhausted",
) -> DeadLetterResult:
    """Move a task to DEAD_LETTER — terminal, reviewable state."""
    with conn.cursor() as cur:
        cur.execute("SELECT state FROM tasks WHERE task_id = %s", (task_id,))
        before = cur.fetchone()
        if before is None:
            raise LookupError(f"task not found: {task_id}")
        previous_state = before[0]
        cur.execute(
            """
            UPDATE tasks
            SET state = 'DEAD_LETTER',
                completed_at = NOW(),
                updated_at = NOW()
            WHERE task_id = %s
            """,
            (task_id,),
        )
        if cur.rowcount != 1:
            raise LookupError(f"task not found: {task_id}")
    return DeadLetterResult(
        task_id=task_id,
        previous_state=previous_state,
        failure_class=failure_class,
        reason=reason,
    )


def utc_now() -> datetime:
    return datetime.now(timezone.utc)
