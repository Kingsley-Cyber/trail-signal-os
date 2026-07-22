"""Memory-pressure admission actions (control_plane_v2 §2, environment_profile §2)."""

from __future__ import annotations

from dataclasses import dataclass

from control.resources.host_metrics import PressureLevel
from control.resources.phase_gating import PhaseProfile
from control.scheduler.settings import FETCH_LANES


@dataclass(frozen=True)
class PressureActions:
    allow_browser: bool
    allow_llm_admission: bool
    allow_fetch_lanes: bool
    parser_processes: int
    pressure: PressureLevel
    reason: str


def pressure_actions(
    pressure: PressureLevel,
    profile: PhaseProfile,
) -> PressureActions:
    """Derive lane admission constraints from memory pressure + phase profile."""
    if pressure == PressureLevel.GREEN:
        return PressureActions(
            allow_browser=profile.browser_enabled,
            allow_llm_admission=profile.local_llm_enabled,
            allow_fetch_lanes=True,
            parser_processes=profile.parser_processes,
            pressure=pressure,
            reason="pressure_green",
        )

    if pressure == PressureLevel.ORANGE:
        return PressureActions(
            allow_browser=False,
            allow_llm_admission=False,
            allow_fetch_lanes=True,
            parser_processes=min(profile.parser_processes, 1),
            pressure=pressure,
            reason="pressure_orange_throttle",
        )

    return PressureActions(
        allow_browser=False,
        allow_llm_admission=False,
        allow_fetch_lanes=False,
        parser_processes=0,
        pressure=pressure,
        reason="pressure_red_pause",
    )


def lane_allowed_under_pressure(lane: str, actions: PressureActions) -> tuple[bool, str]:
    if lane in FETCH_LANES:
        if not actions.allow_fetch_lanes:
            return False, actions.reason
        if lane == "browser" and not actions.allow_browser:
            return False, "pressure_orange_no_browser"
        return True, "pressure_fetch_allowed"

    if lane == "enrich" and not actions.allow_llm_admission:
        return False, "pressure_orange_no_llm"

    if lane == "extract" and actions.parser_processes <= 0:
        return False, actions.reason

    return True, "pressure_lane_allowed"
