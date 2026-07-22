"""Tier discount + hostile-dependence cap; tier-loss degrades, never evades (N28, LAW 1)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from typing import TYPE_CHECKING

from guards.runtime_guards import guard10_route_403_to_blocked
from signal_engine.coverage import (
    build_grid_from_signals,
    find_coverage_gaps,
    hard_required_met,
)

if TYPE_CHECKING:
    from signal_engine.score import ScoreResult

CODE_VERSION = "tiers-1.0.0"
TIERS_VERSION = CODE_VERSION

DEFAULT_TIER_WEIGHT: dict[str, float] = {
    "open": 1.00,
    "defended": 0.85,
    "hostile": 0.50,
}
HOSTILE_DEPENDENCE_CONFIDENCE_CAP = 0.50
HOSTILE_TIER = "hostile"


class TierError(Exception):
    """Deterministic tier evaluation failed."""


@dataclass(frozen=True)
class TierEffects:
    hostile_dependent: bool
    raw_confidence: float
    confidence: float
    tier_loss_gaps: tuple[dict[str, str], ...]


def tier_weight_for(
    source_tier: str,
    *,
    tier_weight: Mapping[str, float] | None = None,
) -> float:
    """Doc 08 §9: open 1.00 · defended 0.85 · hostile 0.50."""
    active = dict(DEFAULT_TIER_WEIGHT)
    if tier_weight is not None:
        active.update(tier_weight)
    if source_tier not in active:
        raise TierError(f"unsupported source tier: {source_tier!r}")
    return active[source_tier]


def filter_signals_without_tiers(
    signals: Sequence[Mapping[str, Any]],
    excluded_tiers: Sequence[str],
) -> list[dict[str, Any]]:
    """Remove signals whose source tier is in excluded_tiers (tier-loss simulation)."""
    excluded = set(excluded_tiers)
    return [
        dict(signal)
        for signal in signals
        if str(signal.get("source", {}).get("tier", "")) not in excluded
    ]


def compute_hostile_dependent(
    *,
    niche_id: str,
    signals: Sequence[Mapping[str, Any]],
    as_of: str,
    min_cell_confidence: float,
) -> bool:
    """Doc 08 §9: true when losing hostile-tier signals breaks hard-required coverage."""
    full_grid = build_grid_from_signals(
        niche_id=niche_id,
        signals=signals,
        updated_at=as_of,
    )
    if not hard_required_met(full_grid, min_cell_confidence=min_cell_confidence):
        return False

    without_hostile = filter_signals_without_tiers(signals, (HOSTILE_TIER,))
    reduced_grid = build_grid_from_signals(
        niche_id=niche_id,
        signals=without_hostile,
        updated_at=as_of,
    )
    return not hard_required_met(reduced_grid, min_cell_confidence=min_cell_confidence)


def apply_hostile_dependence_cap(
    raw_confidence: float,
    *,
    hostile_dependent: bool,
    cap: float = HOSTILE_DEPENDENCE_CONFIDENCE_CAP,
) -> float:
    """Doc 08 §9: opp_confidence = min(opp_confidence, 0.50) when hostile-dependent."""
    if not hostile_dependent:
        return raw_confidence
    return min(raw_confidence, cap)


def _round_confidence(value: float) -> float:
    return round(value, 2)


def evaluate_tier_effects(
    *,
    raw_confidence: float,
    niche_id: str,
    signals: Sequence[Mapping[str, Any]],
    as_of: str,
    min_cell_confidence: float = 0.40,
) -> TierEffects:
    """Apply hostile-dependence detection + confidence cap."""
    hostile_dependent = compute_hostile_dependent(
        niche_id=niche_id,
        signals=signals,
        as_of=as_of,
        min_cell_confidence=min_cell_confidence,
    )
    capped = apply_hostile_dependence_cap(
        raw_confidence,
        hostile_dependent=hostile_dependent,
    )
    tier_loss_gaps = tier_loss_gaps_after_removal(
        niche_id=niche_id,
        signals=signals,
        as_of=as_of,
        excluded_tiers=(HOSTILE_TIER,),
        min_cell_confidence=min_cell_confidence,
    )
    return TierEffects(
        hostile_dependent=hostile_dependent,
        raw_confidence=raw_confidence,
        confidence=_round_confidence(capped),
        tier_loss_gaps=tier_loss_gaps,
    )


def tier_loss_gaps_after_removal(
    *,
    niche_id: str,
    signals: Sequence[Mapping[str, Any]],
    as_of: str,
    excluded_tiers: Sequence[str] = (HOSTILE_TIER,),
    min_cell_confidence: float = 0.40,
) -> tuple[dict[str, str], ...]:
    """Coverage gaps after simulating tier loss (doc 06 degradation, no evasion)."""
    remaining = filter_signals_without_tiers(signals, excluded_tiers)
    grid = build_grid_from_signals(
        niche_id=niche_id,
        signals=remaining,
        updated_at=as_of,
    )
    return tuple(gap.to_dict() for gap in find_coverage_gaps(
        grid,
        min_cell_confidence=min_cell_confidence,
    ))


def route_tier_loss_response(*, status_code: int, escalation: str | None) -> str:
    """Guard 10: tier-loss routes BLOCKED, never stealth escalation (doc 06 §2.7)."""
    return guard10_route_403_to_blocked(status_code=status_code, escalation=escalation)


def replay_opportunity_score(
    signals: Sequence[Mapping[str, Any]] | Mapping[str, Any],
    weights: Mapping[str, Any],
    *,
    niche_id: str,
    as_of: str,
) -> ScoreResult:
    """Deterministic replay: same signals + weights → same score (Gate 6 / v4 §13)."""
    from signal_engine.score import compute_score_result

    return compute_score_result(
        signals,
        weights,
        niche_id=niche_id,
        as_of=as_of,
    )


def diff_opportunity_scores(
    left: Mapping[str, Any],
    right: Mapping[str, Any],
) -> dict[str, Any]:
    """Compare two opportunity.v1 payloads; mirrors lineage.diff subscore deltas."""
    left_sub = dict(left.get("subscores") or {})
    right_sub = dict(right.get("subscores") or {})
    axes = sorted(set(left_sub) | set(right_sub))
    subscore_deltas = {
        axis: round(right_sub.get(axis, 0.0) - left_sub.get(axis, 0.0), 3)
        for axis in axes
    }
    left_prov = dict(left.get("provenance") or {})
    right_prov = dict(right.get("provenance") or {})
    version_tag_changes: list[dict[str, Any]] = []
    if left_prov.get("weights_version") != right_prov.get("weights_version"):
        version_tag_changes.append(
            {
                "field": "weights_version",
                "left": left_prov.get("weights_version"),
                "right": right_prov.get("weights_version"),
            },
        )
    score_delta = round(float(right.get("score", 0.0)) - float(left.get("score", 0.0)), 2)
    confidence_delta = round(
        float(right.get("confidence", 0.0)) - float(left.get("confidence", 0.0)),
        2,
    )
    identical = (
        score_delta == 0.0
        and confidence_delta == 0.0
        and all(delta == 0.0 for delta in subscore_deltas.values())
        and not version_tag_changes
    )
    return {
        "left_opportunity_id": left.get("opportunity_id"),
        "right_opportunity_id": right.get("opportunity_id"),
        "score_delta": score_delta,
        "confidence_delta": confidence_delta,
        "subscore_deltas": subscore_deltas,
        "version_tag_changes": version_tag_changes,
        "identical": identical,
    }


def score_after_tier_loss(
    signals: Sequence[Mapping[str, Any]] | Mapping[str, Any],
    weights: Mapping[str, Any],
    *,
    niche_id: str,
    as_of: str,
    excluded_tiers: Sequence[str] = (HOSTILE_TIER,),
) -> tuple[ScoreResult, tuple[dict[str, str], ...]]:
    """Score after tier loss: degraded confidence + gap flags, no evasion path."""
    from signal_engine.score import compute_score_result, score

    if isinstance(signals, Mapping) and "signals" in signals:
        bundle = dict(signals)
        bundle["signals"] = filter_signals_without_tiers(
            bundle["signals"],
            excluded_tiers,
        )
        remaining = bundle["signals"]
        result = score(bundle, weights, niche_id=niche_id, as_of=as_of)
    else:
        remaining = filter_signals_without_tiers(signals, excluded_tiers)
        result = compute_score_result(
            remaining,
            weights,
            niche_id=niche_id,
            as_of=as_of,
        )
    gaps = tier_loss_gaps_after_removal(
        niche_id=niche_id,
        signals=remaining,
        as_of=as_of,
        excluded_tiers=(),
        min_cell_confidence=float(weights.get("min_cell_confidence", 0.40)),
    )
    return result, gaps


__all__ = [
    "CODE_VERSION",
    "DEFAULT_TIER_WEIGHT",
    "HOSTILE_DEPENDENCE_CONFIDENCE_CAP",
    "HOSTILE_TIER",
    "TIERS_VERSION",
    "TierEffects",
    "TierError",
    "apply_hostile_dependence_cap",
    "compute_hostile_dependent",
    "diff_opportunity_scores",
    "evaluate_tier_effects",
    "filter_signals_without_tiers",
    "replay_opportunity_score",
    "route_tier_loss_response",
    "score_after_tier_loss",
    "tier_loss_gaps_after_removal",
    "tier_weight_for",
]
