"""Runtime write guards (doc 09 §1 guards 2, 5, 6, 7, 10, 11)."""

from __future__ import annotations

from typing import Any, Callable, Iterable, Mapping

from guards.exceptions import GuardViolation, StaleLeaseError

REQUIRED_PROVENANCE_KEYS = (
    "schema_version",
    "config_hash",
    "created_at",
)


def guard2_require_fenced_update(rows_updated: int, *, expected_owner: str, actual_owner: str) -> None:
    """Guard 2: fenced task update must affect exactly one row or raise StaleLeaseError."""
    if actual_owner != expected_owner:
        raise StaleLeaseError("lease_owner mismatch on task state update")
    if rows_updated == 0:
        raise StaleLeaseError("stale lease_generation: task state update affected 0 rows")


def guard6_require_lineage_edge(
    *,
    parent_refs: Iterable[str],
    lineage_edge_written: bool,
) -> None:
    """Guard 2 runtime half for LAW 2: parent refs and lineage edge both required."""
    refs = list(parent_refs)
    if not refs:
        raise GuardViolation("LAW 2: derived artifact requires non-empty parent refs")
    if not lineage_edge_written:
        raise GuardViolation("LAW 2: derived artifact requires lineage_edges row")


def guard7_require_provenance(provenance: Mapping[str, Any] | None) -> None:
    """Guard 7: every artifact write requires a provenance stamp."""
    if not provenance:
        raise GuardViolation("artifact write missing provenance stamp")
    missing = [key for key in REQUIRED_PROVENANCE_KEYS if key not in provenance]
    if missing:
        raise GuardViolation(
            "artifact provenance missing required keys: " + ", ".join(missing)
        )


def guard10_route_403_to_blocked(*, status_code: int, escalation: str | None) -> str:
    """Guard 10 runtime: HTTP 403 must route to BLOCKED, never stealth escalation."""
    if status_code != 403:
        return "RETRY"
    if escalation:
        raise GuardViolation(
            f"403 handler attempted browser escalation `{escalation}`; must route BLOCKED"
        )
    return "BLOCKED"


def guard11_assert_normalize_invariants(
    *,
    normalized_score: float,
    window: Mapping[str, str] | None,
    direction_applied: bool,
) -> None:
    """Guard 11: normalized values must stay in [0, 1] with window + direction applied."""
    if not 0.0 <= normalized_score <= 1.0:
        raise GuardViolation(
            f"normalize invariant violated: score {normalized_score} outside [0, 1]"
        )
    if not window or "from" not in window or "to" not in window:
        raise GuardViolation("normalize invariant violated: window must be set")
    if not direction_applied:
        raise GuardViolation("normalize invariant violated: direction not applied")


def guard12_assert_score_reproducible(
    score_fn: Callable[[], float],
    *,
    expected: float,
    runs: int = 2,
    tolerance: float = 1e-12,
) -> None:
    """Guard 12: score path must be deterministic across repeated runs."""
    values = [score_fn() for _ in range(runs)]
    if any(abs(value - expected) > tolerance for value in values):
        raise GuardViolation(
            f"score reproducibility failed: expected {expected}, got {values}"
        )
    if len(set(values)) != 1:
        raise GuardViolation(f"score reproducibility failed: nondeterministic {values}")
