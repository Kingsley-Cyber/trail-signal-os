"""Failure classifier — tags failures for retry policy (doc 06 §2, archive §10)."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from guards.runtime_guards import guard10_route_403_to_blocked

RETRYABLE_HTTP_STATUSES = frozenset({408, 429, 500, 502, 503, 504})
NON_RETRYABLE_HTTP_STATUSES = frozenset({401, 403, 404, 410})
BLOCKED_HTTP_STATUSES = frozenset({403})

RETRYABLE_ERROR_CODES = frozenset(
    {
        "NETWORK_TIMEOUT",
        "DNS_TEMPORARY_FAILURE",
        "CONNECTION_RESET",
        "HTTP_408",
        "HTTP_429",
        "HTTP_500",
        "HTTP_502",
        "HTTP_503",
        "HTTP_504",
        "BROWSER_CRASH",
        "TEMPORARY_STORAGE_FAILURE",
        "DATABASE_CONNECTION_FAILURE",
    }
)

NON_RETRYABLE_ERROR_CODES = frozenset(
    {
        "HTTP_404",
        "HTTP_410",
        "ROBOTS_DISALLOWED",
        "POLICY_DISALLOWED",
        "AUTHORIZATION_REQUIRED",
        "UNSUPPORTED_CONTENT",
        "INVALID_JOB_INPUT",
        "MAX_RESPONSE_SIZE_EXCEEDED",
    }
)

BLOCKED_ERROR_CODES = frozenset({"HTTP_403", "ROBOTS_DISALLOWED", "POLICY_DISALLOWED"})


class FailureAction(str, Enum):
    RETRY_WAIT = "RETRY_WAIT"
    BLOCKED = "BLOCKED"
    FAILED = "FAILED"


@dataclass(frozen=True)
class FailureClassification:
    failure_class: str
    action: FailureAction
    retryable: bool
    counts_toward_circuit: bool


def _http_failure_class(status_code: int) -> str:
    return f"HTTP_{status_code}"


def classify_failure(
    *,
    status_code: int | None = None,
    error_code: str | None = None,
    escalation: str | None = None,
) -> FailureClassification:
    """Classify a fetch/worker failure; never return a generic failed=true tag."""
    if status_code is not None:
        failure_class = _http_failure_class(status_code)
        if status_code in BLOCKED_HTTP_STATUSES:
            guard10_route_403_to_blocked(status_code=status_code, escalation=escalation)
            return FailureClassification(
                failure_class=failure_class,
                action=FailureAction.BLOCKED,
                retryable=False,
                counts_toward_circuit=False,
            )
        if status_code in RETRYABLE_HTTP_STATUSES:
            return FailureClassification(
                failure_class=failure_class,
                action=FailureAction.RETRY_WAIT,
                retryable=True,
                counts_toward_circuit=True,
            )
        if status_code in NON_RETRYABLE_HTTP_STATUSES:
            action = (
                FailureAction.BLOCKED
                if status_code == 401
                else FailureAction.FAILED
            )
            return FailureClassification(
                failure_class=failure_class,
                action=action,
                retryable=False,
                counts_toward_circuit=False,
            )
        return FailureClassification(
            failure_class=failure_class,
            action=FailureAction.FAILED,
            retryable=False,
            counts_toward_circuit=False,
        )

    if error_code is None:
        return FailureClassification(
            failure_class="UNKNOWN_FAILURE",
            action=FailureAction.FAILED,
            retryable=False,
            counts_toward_circuit=False,
        )

    failure_class = error_code
    if error_code in BLOCKED_ERROR_CODES:
        if error_code == "HTTP_403":
            guard10_route_403_to_blocked(status_code=403, escalation=escalation)
        return FailureClassification(
            failure_class=failure_class,
            action=FailureAction.BLOCKED,
            retryable=False,
            counts_toward_circuit=False,
        )
    if error_code in RETRYABLE_ERROR_CODES:
        return FailureClassification(
            failure_class=failure_class,
            action=FailureAction.RETRY_WAIT,
            retryable=True,
            counts_toward_circuit=True,
        )
    if error_code in NON_RETRYABLE_ERROR_CODES:
        action = (
            FailureAction.BLOCKED
            if error_code in {"ROBOTS_DISALLOWED", "POLICY_DISALLOWED"}
            else FailureAction.FAILED
        )
        return FailureClassification(
            failure_class=failure_class,
            action=action,
            retryable=False,
            counts_toward_circuit=False,
        )
    return FailureClassification(
        failure_class=failure_class,
        action=FailureAction.FAILED,
        retryable=False,
        counts_toward_circuit=False,
    )
