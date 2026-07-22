"""Inter-lane backpressure for fetch admission (control_plane_v3 §2)."""

from __future__ import annotations

from dataclasses import dataclass

import psycopg

from control.scheduler.settings import (
    DOWNSTREAM_BACKLOG_LANES,
    FETCH_LANES,
    load_phases_config,
)

BACKLOG_STATES = ("READY", "LEASED", "RUNNING", "RETRY_WAIT", "PENDING")


@dataclass
class BackpressureGate:
    """Tracks fetch-lane pause across scheduler ticks (high/low hysteresis)."""

    fetch_paused: bool = False

    def update(
        self,
        backlogs: dict[str, int],
        *,
        high_limits: dict[str, int],
        low_limits: dict[str, int],
    ) -> None:
        if any(backlogs[key] >= high_limits[key] for key in backlogs):
            self.fetch_paused = True
        elif self.fetch_paused and all(backlogs[key] <= low_limits[key] for key in backlogs):
            self.fetch_paused = False


@dataclass(frozen=True)
class BackpressureState:
    paused: bool
    reason: str
    backlogs: dict[str, int]
    watermarks: dict[str, dict[str, int]]


def _lane_backlog(conn: psycopg.Connection, lane: str) -> int:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT COUNT(*)
            FROM tasks
            WHERE lane = %s
              AND state = ANY(%s)
            """,
            (lane, list(BACKLOG_STATES)),
        )
        row = cur.fetchone()
    assert row is not None
    return int(row[0])


def _watermark_limits(
    watermarks: dict[str, dict[str, int]],
    backlogs: dict[str, int],
) -> tuple[dict[str, int], dict[str, int]]:
    high_limits = {
        key: int(watermarks.get(key, {}).get("high", 0))
        for key in backlogs
    }
    low_limits = {
        key: int(watermarks.get(key, {}).get("low", 0))
        for key in backlogs
    }
    return high_limits, low_limits


def measure_backpressure(
    conn: psycopg.Connection,
    *,
    gate: BackpressureGate | None = None,
) -> BackpressureState:
    phases = load_phases_config()
    watermarks = phases.get("watermarks", {})
    backlogs = {
        key: _lane_backlog(conn, lane)
        for lane, key in DOWNSTREAM_BACKLOG_LANES.items()
    }
    high_limits, low_limits = _watermark_limits(watermarks, backlogs)

    if gate is not None:
        gate.update(backlogs, high_limits=high_limits, low_limits=low_limits)
        paused = gate.fetch_paused
        reason = (
            "downstream_backpressure_paused"
            if paused
            else "downstream_below_high_watermark"
        )
    else:
        paused = any(backlogs[key] >= high_limits[key] for key in backlogs)
        reason = (
            "downstream_at_high_watermark"
            if paused
            else "downstream_below_high_watermark"
        )

    return BackpressureState(
        paused=paused,
        reason=reason,
        backlogs=backlogs,
        watermarks=watermarks,
    )


def fetch_admission_allowed(
    conn: psycopg.Connection,
    *,
    lane: str,
    backpressure: BackpressureState | None = None,
    gate: BackpressureGate | None = None,
) -> bool:
    if lane not in FETCH_LANES:
        return True
    state = backpressure or measure_backpressure(conn, gate=gate)
    return not state.paused
