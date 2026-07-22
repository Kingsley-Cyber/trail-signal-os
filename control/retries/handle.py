"""Orchestrate classifier, backoff, circuits, and dead-letter on task failure."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import psycopg

from control.retries.backoff import compute_backoff_seconds, retry_at_from_delay
from control.retries.circuit_breaker import (
    CircuitRegistry,
    CircuitTransition,
    build_degradation_event,
)
from control.retries.classifier import FailureAction, FailureClassification, classify_failure
from control.retries.dead_letter import send_to_dead_letter


@dataclass(frozen=True)
class FailureHandlingResult:
    task_id: str
    failure_class: str
    action: str
    retry_at: datetime | None = None
    attempt: int | None = None
    degradation_event: dict[str, Any] | None = None
    circuit_state: str | None = None


def _utc_now(now: datetime | None) -> datetime:
    if now is None:
        return datetime.now(timezone.utc)
    if now.tzinfo is None:
        return now.replace(tzinfo=timezone.utc)
    return now


def _set_terminal_state(
    conn: psycopg.Connection,
    *,
    task_id: str,
    state: str,
) -> None:
    conn.execute(
        """
        UPDATE tasks
        SET state = %s,
            completed_at = NOW(),
            updated_at = NOW()
        WHERE task_id = %s
        """,
        (state, task_id),
    )


def _set_retry_wait(
    conn: psycopg.Connection,
    *,
    task_id: str,
    retry_at: datetime,
    attempt: int,
) -> None:
    conn.execute(
        """
        UPDATE tasks
        SET state = 'RETRY_WAIT',
            retry_at = %s,
            attempt = %s,
            lease_owner = NULL,
            lease_generation = lease_generation + 1,
            lease_expires_at = NULL,
            updated_at = NOW()
        WHERE task_id = %s
        """,
        (retry_at, attempt, task_id),
    )


def handle_task_failure(
    conn: psycopg.Connection,
    *,
    task_id: str,
    job_id: str,
    domain: str,
    route: str,
    lane: str,
    attempt: int,
    max_attempts: int,
    circuits: CircuitRegistry,
    config_hash: str,
    status_code: int | None = None,
    error_code: str | None = None,
    escalation: str | None = None,
    retry_after_seconds: float | None = None,
    now: datetime | None = None,
    event_id_prefix: str = "deg_n8",
) -> FailureHandlingResult:
    """Apply retry policy to a failed task; returns the chosen action."""
    current_time = _utc_now(now)
    route_key = route if ":" in route else f"{domain}:{route}"

    classification = classify_failure(
        status_code=status_code,
        error_code=error_code,
        escalation=escalation,
    )

    if classification.action is FailureAction.BLOCKED:
        _set_terminal_state(conn, task_id=task_id, state="BLOCKED")
        return FailureHandlingResult(
            task_id=task_id,
            failure_class=classification.failure_class,
            action="BLOCKED",
        )

    if classification.action is FailureAction.FAILED:
        _set_terminal_state(conn, task_id=task_id, state="FAILED")
        return FailureHandlingResult(
            task_id=task_id,
            failure_class=classification.failure_class,
            action="FAILED",
        )

    next_attempt = attempt + 1
    if next_attempt > max_attempts:
        send_to_dead_letter(
            conn,
            task_id=task_id,
            failure_class=classification.failure_class,
        )
        return FailureHandlingResult(
            task_id=task_id,
            failure_class=classification.failure_class,
            action="DEAD_LETTER",
            attempt=attempt,
        )

    transition: CircuitTransition | None = None
    if classification.counts_toward_circuit:
        transition = circuits.record_failure(
            route_key,
            now=current_time,
            failure_class=classification.failure_class,
        )

    circuit = circuits.get(route_key)
    if not circuit.allow_request(now=current_time):
        retry_at = circuit.cooldown_until or retry_at_from_delay(
            now=current_time,
            delay_seconds=compute_backoff_seconds(
                attempt=next_attempt,
                lane=lane,
                failure_class=classification.failure_class,
                retry_after_seconds=retry_after_seconds,
            ),
        )
        _set_retry_wait(conn, task_id=task_id, retry_at=retry_at, attempt=next_attempt)
        degradation = None
        if transition and transition.event_type == "circuit_open":
            degradation = build_degradation_event(
                event_id=f"{event_id_prefix}_open",
                domain=domain,
                route=route_key,
                transition=transition,
                config_hash=config_hash,
                recorded_at=current_time,
                task_id=task_id,
                job_id=job_id,
            )
        return FailureHandlingResult(
            task_id=task_id,
            failure_class=classification.failure_class,
            action="RETRY_WAIT",
            retry_at=retry_at,
            attempt=next_attempt,
            degradation_event=degradation,
            circuit_state=circuit.state.value,
        )

    delay = compute_backoff_seconds(
        attempt=next_attempt,
        lane=lane,
        failure_class=classification.failure_class,
        retry_after_seconds=retry_after_seconds,
    )
    retry_at = retry_at_from_delay(now=current_time, delay_seconds=delay)
    _set_retry_wait(conn, task_id=task_id, retry_at=retry_at, attempt=next_attempt)

    degradation = None
    if transition and transition.event_type == "circuit_open":
        degradation = build_degradation_event(
            event_id=f"{event_id_prefix}_open",
            domain=domain,
            route=route_key,
            transition=transition,
            config_hash=config_hash,
            recorded_at=current_time,
            task_id=task_id,
            job_id=job_id,
        )

    return FailureHandlingResult(
        task_id=task_id,
        failure_class=classification.failure_class,
        action="RETRY_WAIT",
        retry_at=retry_at,
        attempt=next_attempt,
        degradation_event=degradation,
        circuit_state=circuit.state.value,
    )


def record_route_success(
    circuits: CircuitRegistry,
    *,
    domain: str,
    route: str,
    now: datetime | None = None,
    config_hash: str,
    event_id: str = "deg_n8_close",
) -> dict[str, Any] | None:
    """Record a successful probe/request; returns circuit_close event when applicable."""
    current_time = _utc_now(now)
    route_key = route if ":" in route else f"{domain}:{route}"
    transition = circuits.record_success(route_key, now=current_time)
    if transition is None or transition.event_type != "circuit_close":
        return None
    return build_degradation_event(
        event_id=event_id,
        domain=domain,
        route=route_key,
        transition=transition,
        config_hash=config_hash,
        recorded_at=current_time,
    )
