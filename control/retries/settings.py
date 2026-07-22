"""Retry and circuit-breaker defaults (archive control_plane §10–11, doc 06 §2)."""

from __future__ import annotations

from dataclasses import dataclass

CODE_VERSION = "circuits-1.0.0"

# archive control_plane §11 — open after 10 consecutive or >50% of last 20
CONSECUTIVE_FAILURE_THRESHOLD = 10
FAILURE_RATE_THRESHOLD = 0.5
FAILURE_WINDOW_SIZE = 20

# archive control_plane §11 — 5m → 15m → 30m on repeat opens
DEFAULT_COOLDOWN_SECONDS = (300, 900, 1800)

# doc 06 §2 — youtube:ytdlp route cooldown escalation (12h → 24h → 48h)
ROUTE_COOLDOWN_SECONDS: dict[str, tuple[int, ...]] = {
    "youtube:ytdlp": (43_200, 86_400, 172_800),
}

# archive control_plane §10 — lane retry ceilings (seconds)
LANE_MAX_DELAYS: dict[str, float] = {
    "http": 32.0,
    "browser": 45.0,
    "search": 32.0,
    "media": 32.0,
    "extract": 30.0,
    "enrich": 30.0,
    "index": 30.0,
    "default": 32.0,
}

LANE_BASE_DELAYS: dict[str, float] = {
    "http": 2.0,
    "browser": 5.0,
    "search": 2.0,
    "media": 2.0,
    "extract": 1.0,
    "enrich": 1.0,
    "index": 1.0,
    "default": 2.0,
}


@dataclass(frozen=True)
class CircuitConfig:
    consecutive_threshold: int = CONSECUTIVE_FAILURE_THRESHOLD
    failure_rate_threshold: float = FAILURE_RATE_THRESHOLD
    window_size: int = FAILURE_WINDOW_SIZE
    default_cooldown_seconds: tuple[int, ...] = DEFAULT_COOLDOWN_SECONDS


def cooldown_steps_for_route(route_key: str, config: CircuitConfig | None = None) -> tuple[int, ...]:
    """Return escalating cooldown durations for a domain:route key."""
    if route_key in ROUTE_COOLDOWN_SECONDS:
        return ROUTE_COOLDOWN_SECONDS[route_key]
    if config is not None:
        return config.default_cooldown_seconds
    return DEFAULT_COOLDOWN_SECONDS
