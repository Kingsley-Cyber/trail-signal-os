"""Job budget checks before scheduler admission."""

from __future__ import annotations

from dataclasses import dataclass

import psycopg

LANE_BUDGET_FIELD = {
    "search": "max_queries",
    "http": "max_fetched_urls",
    "browser": "browser_pages",
    "media": "media_items",
}

SPEND_STATES = ("READY", "LEASED", "RUNNING", "SUCCEEDED", "RETRY_WAIT")


@dataclass(frozen=True)
class BudgetCheckResult:
    allowed: bool
    reason: str
    field: str | None = None
    limit: int | None = None
    spent: int | None = None


def _fetch_job_budget(conn: psycopg.Connection, job_id: str) -> dict:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT budget FROM research_jobs WHERE job_id = %s",
            (job_id,),
        )
        row = cur.fetchone()
    if row is None:
        raise ValueError(f"unknown job_id: {job_id}")
    return row[0]


def count_lane_spend(conn: psycopg.Connection, *, job_id: str, lane: str) -> int:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT COUNT(*)
            FROM tasks
            WHERE job_id = %s
              AND lane = %s
              AND state = ANY(%s)
            """,
            (job_id, lane, list(SPEND_STATES)),
        )
        row = cur.fetchone()
    assert row is not None
    return int(row[0])


def check_lane_budget(
    conn: psycopg.Connection,
    *,
    job_id: str,
    lane: str,
) -> BudgetCheckResult:
    field = LANE_BUDGET_FIELD.get(lane)
    if field is None:
        return BudgetCheckResult(allowed=True, reason="lane_not_budgeted")

    budget = _fetch_job_budget(conn, job_id)
    limit = int(budget[field])
    spent = count_lane_spend(conn, job_id=job_id, lane=lane)
    if spent >= limit:
        return BudgetCheckResult(
            allowed=False,
            reason="budget_exhausted",
            field=field,
            limit=limit,
            spent=spent,
        )
    return BudgetCheckResult(
        allowed=True,
        reason="within_budget",
        field=field,
        limit=limit,
        spent=spent,
    )
