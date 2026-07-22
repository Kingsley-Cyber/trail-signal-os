"""Governor orchestration — phase gating + backpressure under memory pressure."""

from __future__ import annotations

from dataclasses import dataclass

import psycopg

from control.resources.host_metrics import (
    MemoryMetrics,
    MetricsProvider,
    PressureLevel,
    classify_pressure,
    read_memory_metrics,
)
from control.resources.phase_gating import PhaseProfile, lane_enabled_for_phase, load_phase_profile
from control.resources.pressure_policy import (
    PressureActions,
    lane_allowed_under_pressure,
    pressure_actions,
)
from control.scheduler.admit import AdmissionResult, admit_task
from control.scheduler.backpressure import (
    BackpressureGate,
    BackpressureState,
    measure_backpressure,
)
from control.scheduler.settings import FETCH_LANES


@dataclass(frozen=True)
class GovernorState:
    phase: str
    profile: PhaseProfile
    memory: MemoryMetrics
    pressure: PressureLevel
    actions: PressureActions
    backpressure: BackpressureState
    fetch_paused: bool

    @property
    def combined_fetch_paused(self) -> bool:
        return self.fetch_paused or not self.actions.allow_fetch_lanes


def evaluate_governor(
    conn: psycopg.Connection,
    *,
    phase: str,
    metrics: MemoryMetrics | None = None,
    metrics_provider: MetricsProvider | None = None,
    backpressure_gate: BackpressureGate | None = None,
) -> GovernorState:
    """Single governor tick: phase profile, memory pressure, downstream backpressure."""
    profile = load_phase_profile(phase)
    memory = metrics if metrics is not None else read_memory_metrics(provider=metrics_provider)
    pressure = classify_pressure(memory)
    actions = pressure_actions(pressure, profile)
    gate = backpressure_gate if backpressure_gate is not None else BackpressureGate()
    backpressure = measure_backpressure(conn, gate=gate)
    fetch_paused = backpressure.paused or not actions.allow_fetch_lanes
    return GovernorState(
        phase=phase,
        profile=profile,
        memory=memory,
        pressure=pressure,
        actions=actions,
        backpressure=backpressure,
        fetch_paused=fetch_paused,
    )


def lane_admission_allowed(
    *,
    lane: str,
    governor: GovernorState,
) -> tuple[bool, str]:
    """Combined phase + memory + backpressure gate for one lane."""
    if not lane_enabled_for_phase(lane, governor.profile):
        return False, f"phase_{governor.phase.lower()}_lane_disabled"

    pressure_ok, pressure_reason = lane_allowed_under_pressure(lane, governor.actions)
    if not pressure_ok:
        return False, pressure_reason

    if lane in FETCH_LANES and governor.combined_fetch_paused:
        if not governor.actions.allow_fetch_lanes:
            return False, governor.actions.reason
        return False, governor.backpressure.reason

    return True, "governor_admit"


def admit_task_with_governor(
    conn: psycopg.Connection,
    *,
    task_id: str,
    phase: str,
    metrics: MemoryMetrics | None = None,
    metrics_provider: MetricsProvider | None = None,
    backpressure_gate: BackpressureGate | None = None,
) -> AdmissionResult:
    """Admit one task after governor phase/pressure/backpressure checks."""
    gate = backpressure_gate if backpressure_gate is not None else BackpressureGate()
    governor = evaluate_governor(
        conn,
        phase=phase,
        metrics=metrics,
        metrics_provider=metrics_provider,
        backpressure_gate=gate,
    )

    with conn.cursor() as cur:
        cur.execute("SELECT lane FROM tasks WHERE task_id = %s", (task_id,))
        row = cur.fetchone()
    if row is None:
        return AdmissionResult(task_id=task_id, admitted=False, reason="task_not_found")

    lane = row[0]
    allowed, reason = lane_admission_allowed(lane=lane, governor=governor)
    if not allowed:
        return AdmissionResult(task_id=task_id, admitted=False, reason=reason)

    return admit_task(
        conn,
        task_id=task_id,
        backpressure=governor.backpressure,
        backpressure_gate=gate,
    )
