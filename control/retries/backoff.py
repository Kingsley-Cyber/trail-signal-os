"""Bounded exponential backoff with jitter (archive control_plane §10)."""

from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone

from control.retries.settings import LANE_BASE_DELAYS, LANE_MAX_DELAYS


def compute_backoff_seconds(
    *,
    attempt: int,
    lane: str,
    failure_class: str,
    retry_after_seconds: float | None = None,
    rng: random.Random | None = None,
) -> float:
    """Return delay in seconds before the next retry attempt."""
    if failure_class == "HTTP_429" and retry_after_seconds is not None:
        return max(0.0, float(retry_after_seconds))

    base = LANE_BASE_DELAYS.get(lane, LANE_BASE_DELAYS["default"])
    max_delay = LANE_MAX_DELAYS.get(lane, LANE_MAX_DELAYS["default"])
    exponent = max(0, attempt - 1)
    raw = min(max_delay, base * (2**exponent))
    jitter_source = rng or random
    return raw * jitter_source.uniform(0.75, 1.25)


def retry_at_from_delay(
    *,
    now: datetime,
    delay_seconds: float,
) -> datetime:
    """Compute UTC retry_at from a delay."""
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return now + timedelta(seconds=delay_seconds)
