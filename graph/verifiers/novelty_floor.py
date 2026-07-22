"""novelty_floor — expand back-edge novelty percentage gate (doc 07 §4)."""

from __future__ import annotations

from typing import Any

from graph.verifiers.base import VerifierFn, VerifierResult

DEFAULT_FLOOR_PCT = 0.05
_DIMENSIONS = ("entities", "claims", "domains")


def _novelty_pct(before: dict[str, int], after: dict[str, int]) -> float:
    new_items = sum(max(0, after.get(key, 0) - before.get(key, 0)) for key in _DIMENSIONS)
    total_after = sum(after.get(key, 0) for key in _DIMENSIONS)
    if total_after <= 0:
        return 0.0
    return new_items / total_after


def novelty_floor(*, floor_pct: float = DEFAULT_FLOOR_PCT) -> VerifierFn:
    """Pass when novelty percentage is at or above the configured floor."""

    def _verify(output: dict[str, Any], packed_input: dict[str, Any]) -> VerifierResult:
        novelty_ctx = packed_input.get("novelty")
        if not isinstance(novelty_ctx, dict):
            return VerifierResult(
                passed=False,
                violations=("packed_input.novelty with baseline counts is required",),
            )

        baseline = novelty_ctx.get("baseline")
        if not isinstance(baseline, dict):
            return VerifierResult(passed=False, violations=("packed_input.novelty.baseline is required",))

        after = output.get("expand_counts") or output.get("counts")
        if not isinstance(after, dict):
            return VerifierResult(
                passed=False,
                violations=("output.expand_counts (entities/claims/domains) is required",),
            )

        threshold = float(novelty_ctx.get("floor_pct", floor_pct))
        novelty = _novelty_pct(
            {key: int(baseline.get(key, 0)) for key in _DIMENSIONS},
            {key: int(after.get(key, 0)) for key in _DIMENSIONS},
        )
        if novelty < threshold:
            return VerifierResult(
                passed=False,
                violations=(
                    f"novelty {novelty:.2%} below floor {threshold:.2%}",
                ),
            )
        return VerifierResult(passed=True)

    return _verify
