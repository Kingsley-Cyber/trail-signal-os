"""Retry classifier, backoff, circuit breaker, dead-letter (doc 06 §2, archive §10–11)."""

from control.retries.backoff import compute_backoff_seconds, retry_at_from_delay
from control.retries.circuit_breaker import (
    CircuitConfig,
    CircuitRegistry,
    CircuitSnapshot,
    CircuitState,
    CircuitTransition,
    RouteCircuit,
    build_degradation_event,
)
from control.retries.classifier import FailureAction, FailureClassification, classify_failure
from control.retries.dead_letter import DeadLetterResult, send_to_dead_letter
from control.retries.handle import FailureHandlingResult, handle_task_failure, record_route_success
from control.retries.settings import CODE_VERSION

__all__ = [
    "CODE_VERSION",
    "CircuitConfig",
    "CircuitRegistry",
    "CircuitSnapshot",
    "CircuitState",
    "CircuitTransition",
    "DeadLetterResult",
    "FailureAction",
    "FailureClassification",
    "FailureHandlingResult",
    "RouteCircuit",
    "build_degradation_event",
    "classify_failure",
    "compute_backoff_seconds",
    "handle_task_failure",
    "record_route_success",
    "retry_at_from_delay",
    "send_to_dead_letter",
]
