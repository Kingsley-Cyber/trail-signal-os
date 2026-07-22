"""Per domain:route circuit breakers (archive control_plane §11, doc 06 §2)."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any

from control.retries.settings import (
    CODE_VERSION,
    CircuitConfig,
    cooldown_steps_for_route,
)


class CircuitState(str, Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


@dataclass
class CircuitSnapshot:
    route_key: str
    state: CircuitState
    consecutive_failures: int
    failure_rate: float
    cooldown_until: datetime | None
    open_count: int


@dataclass
class CircuitTransition:
    route_key: str
    previous_state: CircuitState
    new_state: CircuitState
    event_type: str
    cooldown_until: datetime | None
    failure_class: str | None = None


@dataclass
class RouteCircuit:
    route_key: str
    config: CircuitConfig = field(default_factory=CircuitConfig)
    state: CircuitState = CircuitState.CLOSED
    consecutive_failures: int = 0
    recent_results: deque[bool] = field(default_factory=lambda: deque(maxlen=20))
    open_count: int = 0
    cooldown_until: datetime | None = None
    half_open_probe_allowed: bool = False

    def __post_init__(self) -> None:
        self.recent_results = deque(self.recent_results, maxlen=self.config.window_size)

    def _cooldown_duration_seconds(self) -> int:
        steps = cooldown_steps_for_route(self.route_key, self.config)
        index = min(self.open_count, len(steps) - 1)
        return steps[index]

    def _maybe_open(self, *, now: datetime, failure_class: str | None) -> CircuitTransition | None:
        window = list(self.recent_results)
        failure_rate = sum(1 for ok in window if not ok) / len(window) if window else 0.0
        should_open = self.consecutive_failures >= self.config.consecutive_threshold or (
            len(window) >= self.config.window_size
            and failure_rate > self.config.failure_rate_threshold
        )
        if not should_open:
            return None

        previous = self.state
        self.state = CircuitState.OPEN
        self.open_count += 1
        cooldown = self._cooldown_duration_seconds()
        self.cooldown_until = now + timedelta(seconds=cooldown)
        self.half_open_probe_allowed = False
        return CircuitTransition(
            route_key=self.route_key,
            previous_state=previous,
            new_state=CircuitState.OPEN,
            event_type="circuit_open",
            cooldown_until=self.cooldown_until,
            failure_class=failure_class,
        )

    def refresh_state(self, *, now: datetime) -> CircuitTransition | None:
        """Move OPEN → HALF_OPEN once cooldown expires."""
        if self.state != CircuitState.OPEN or self.cooldown_until is None:
            return None
        if now < self.cooldown_until:
            return None
        previous = self.state
        self.state = CircuitState.HALF_OPEN
        self.half_open_probe_allowed = True
        return CircuitTransition(
            route_key=self.route_key,
            previous_state=previous,
            new_state=CircuitState.HALF_OPEN,
            event_type="probe_allowed",
            cooldown_until=None,
        )

    def allow_request(self, *, now: datetime) -> bool:
        self.refresh_state(now=now)
        if self.state == CircuitState.CLOSED:
            return True
        if self.state == CircuitState.HALF_OPEN:
            return self.half_open_probe_allowed
        return False

    def record_failure(
        self,
        *,
        now: datetime,
        failure_class: str | None = None,
    ) -> CircuitTransition | None:
        self.refresh_state(now=now)
        if self.state == CircuitState.OPEN:
            self.recent_results.append(False)
            return None

        if self.state == CircuitState.HALF_OPEN:
            self.half_open_probe_allowed = False
            self.consecutive_failures += 1
            self.recent_results.append(False)
            return self._maybe_open(now=now, failure_class=failure_class)

        self.consecutive_failures += 1
        self.recent_results.append(False)
        return self._maybe_open(now=now, failure_class=failure_class)

    def record_success(self, *, now: datetime) -> CircuitTransition | None:
        self.recent_results.append(True)
        if self.state == CircuitState.HALF_OPEN:
            previous = self.state
            self.state = CircuitState.CLOSED
            self.consecutive_failures = 0
            self.cooldown_until = None
            self.half_open_probe_allowed = False
            self.open_count = 0
            return CircuitTransition(
                route_key=self.route_key,
                previous_state=previous,
                new_state=CircuitState.CLOSED,
                event_type="circuit_close",
                cooldown_until=None,
            )

        self.consecutive_failures = 0
        return None

    def snapshot(self) -> CircuitSnapshot:
        window = list(self.recent_results)
        failure_rate = sum(1 for ok in window if not ok) / len(window) if window else 0.0
        return CircuitSnapshot(
            route_key=self.route_key,
            state=self.state,
            consecutive_failures=self.consecutive_failures,
            failure_rate=failure_rate,
            cooldown_until=self.cooldown_until,
            open_count=self.open_count,
        )


class CircuitRegistry:
    """In-memory circuit store keyed by domain:route."""

    def __init__(self, config: CircuitConfig | None = None) -> None:
        self._config = config or CircuitConfig()
        self._circuits: dict[str, RouteCircuit] = {}

    def get(self, route_key: str) -> RouteCircuit:
        if route_key not in self._circuits:
            self._circuits[route_key] = RouteCircuit(
                route_key=route_key,
                config=self._config,
            )
        return self._circuits[route_key]

    def allow_request(self, route_key: str, *, now: datetime) -> bool:
        return self.get(route_key).allow_request(now=now)

    def record_failure(
        self,
        route_key: str,
        *,
        now: datetime,
        failure_class: str | None = None,
    ) -> CircuitTransition | None:
        return self.get(route_key).record_failure(now=now, failure_class=failure_class)

    def record_success(self, route_key: str, *, now: datetime) -> CircuitTransition | None:
        return self.get(route_key).record_success(now=now)


def build_degradation_event(
    *,
    event_id: str,
    domain: str,
    route: str,
    transition: CircuitTransition,
    config_hash: str,
    recorded_at: datetime,
    task_id: str | None = None,
    job_id: str | None = None,
) -> dict[str, Any]:
    """Build a degradation_event.v1 payload for circuit transitions."""
    if recorded_at.tzinfo is None:
        recorded_at = recorded_at.replace(tzinfo=timezone.utc)
    payload: dict[str, Any] = {
        "event_id": event_id,
        "domain": domain,
        "route": route,
        "event_type": transition.event_type,
        "recorded_at": recorded_at.isoformat().replace("+00:00", "Z"),
        "provenance": {
            "code_version": CODE_VERSION,
            "schema_version": "degradation_event.v1",
            "config_hash": config_hash,
            "created_at": recorded_at.isoformat().replace("+00:00", "Z"),
        },
        "schema_version": "degradation_event.v1",
    }
    if transition.failure_class:
        payload["failure_class"] = transition.failure_class
    if transition.cooldown_until is not None:
        cooldown = transition.cooldown_until
        if cooldown.tzinfo is None:
            cooldown = cooldown.replace(tzinfo=timezone.utc)
        payload["cooldown_until"] = cooldown.isoformat().replace("+00:00", "Z")
    if task_id:
        payload["task_id"] = task_id
    if job_id:
        payload["job_id"] = job_id
    return payload
