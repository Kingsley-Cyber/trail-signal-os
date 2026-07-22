"""Global lane concurrency gates from config/limits.yaml max_in_flight."""

from __future__ import annotations

from dataclasses import dataclass

import psycopg

from control.scheduler.settings import load_limits_config

IN_FLIGHT_STATES = ("READY", "LEASED", "RUNNING")

LANE_MAX_IN_FLIGHT_KEY = {
    "http": "http_global",
    "browser": "browser_pages",
    "media": "media_concurrency",
    "enrich": "enrich_workers",
    "index": "index_workers",
}


@dataclass(frozen=True)
class ConcurrencyCheckResult:
    allowed: bool
    reason: str
    field: str | None = None
    limit: int | None = None
    in_flight: int | None = None


def count_lane_in_flight(conn: psycopg.Connection, *, lane: str) -> int:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT COUNT(*)
            FROM tasks
            WHERE lane = %s
              AND state = ANY(%s)
            """,
            (lane, list(IN_FLIGHT_STATES)),
        )
        row = cur.fetchone()
    assert row is not None
    return int(row[0])


def check_lane_concurrency(conn: psycopg.Connection, *, lane: str) -> ConcurrencyCheckResult:
    limits_key = LANE_MAX_IN_FLIGHT_KEY.get(lane)
    if limits_key is None:
        return ConcurrencyCheckResult(allowed=True, reason="lane_not_concurrency_gated")

    limits = load_limits_config().get("max_in_flight", {})
    limit = int(limits.get(limits_key, 0))
    if limit <= 0:
        return ConcurrencyCheckResult(allowed=True, reason="lane_not_concurrency_gated")

    in_flight = count_lane_in_flight(conn, lane=lane)
    if in_flight >= limit:
        return ConcurrencyCheckResult(
            allowed=False,
            reason="max_in_flight_exceeded",
            field=limits_key,
            limit=limit,
            in_flight=in_flight,
        )
    return ConcurrencyCheckResult(
        allowed=True,
        reason="within_max_in_flight",
        field=limits_key,
        limit=limit,
        in_flight=in_flight,
    )
