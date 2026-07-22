"""ACQUIRE / ENRICH / INDEX phase profiles (control_plane_v3 §2)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from control.resources.settings import load_phases_config
from control.scheduler.settings import FETCH_LANES

PhaseName = Literal["ACQUIRE", "ENRICH", "INDEX"]
VALID_PHASES = frozenset({"ACQUIRE", "ENRICH", "INDEX"})


@dataclass(frozen=True)
class PhaseProfile:
    phase: PhaseName
    browser_enabled: bool
    http_concurrency: int
    local_llm_enabled: bool
    neo4j_enabled: bool
    enrich_workers: int
    index_workers: int
    parser_processes: int


def load_phase_profile(phase: str) -> PhaseProfile:
    if phase not in VALID_PHASES:
        raise ValueError(f"unknown phase: {phase}")
    raw = load_phases_config().get("profiles", {}).get(phase, {})
    return PhaseProfile(
        phase=phase,  # type: ignore[arg-type]
        browser_enabled=bool(raw.get("browser_enabled", False)),
        http_concurrency=int(raw.get("http_concurrency", 0)),
        local_llm_enabled=bool(raw.get("local_llm_enabled", False)),
        neo4j_enabled=bool(raw.get("neo4j_enabled", False)),
        enrich_workers=int(raw.get("enrich_workers", 0)),
        index_workers=int(raw.get("index_workers", 0)),
        parser_processes=int(raw.get("parser_processes", 0)),
    )


def lane_enabled_for_phase(lane: str, profile: PhaseProfile) -> bool:
    """Return whether a lane is permitted by the active phase profile."""
    if lane in FETCH_LANES:
        if lane == "browser":
            return profile.browser_enabled
        if lane in {"search", "http", "media"}:
            return profile.http_concurrency > 0
        return False

    if lane == "extract":
        return profile.parser_processes > 0

    if lane == "enrich":
        return profile.local_llm_enabled and profile.enrich_workers > 0

    if lane == "index":
        return profile.index_workers > 0

    return True
