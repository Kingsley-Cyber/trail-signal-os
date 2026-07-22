"""Deterministic per-signal confidence — sample × tier × recency (N24, LAW 1)."""

from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any, Mapping

from signal_engine.tiers import DEFAULT_TIER_WEIGHT, TierError, tier_weight_for

CODE_VERSION = "confidence-1.0.0"
CONFIDENCE_VERSION = CODE_VERSION

DEFAULT_WEIGHTS = {"w_n": 0.45, "w_t": 0.30, "w_r": 0.25}
TIER_WEIGHT = DEFAULT_TIER_WEIGHT
N_REF = {
    "demand": 50,
    "growth": 30,
    "pain": 40,
    "competition": 100,
    "content": 60,
}
HALF_LIFE_DAYS = {
    "demand": 60,
    "growth": 30,
    "pain": 180,
    "competition": 45,
    "content": 21,
}


class ConfidenceError(Exception):
    """Deterministic confidence computation failed."""


def _parse_timestamp(value: str) -> datetime:
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    return datetime.fromisoformat(value).astimezone(timezone.utc)


def _sat(value: float) -> float:
    return min(value, 1.0)


def _age_days(*, window_to: str, as_of: str) -> float:
    """Days from measurement window end to scoring reference time."""
    end = _parse_timestamp(window_to)
    reference = _parse_timestamp(as_of)
    delta_seconds = (reference - end).total_seconds()
    return max(0.0, delta_seconds / 86400.0)


def compute_signal_confidence(
    *,
    sample_n: int,
    signal_type: str,
    source_tier: str,
    window_to: str,
    as_of: str,
    weights: Mapping[str, float] | None = None,
    tier_weight: Mapping[str, float] | None = None,
    n_ref: Mapping[str, int] | None = None,
    half_life_days: Mapping[str, int] | None = None,
) -> float:
    """Doc 08 §5: w_n·sat(log1p(n)/log1p(N_ref)) + w_t·tier + w_r·exp(−age/half_life)."""
    if not isinstance(sample_n, int) or sample_n < 1:
        raise ConfidenceError("sample_n must be a positive integer")

    active_weights = dict(DEFAULT_WEIGHTS)
    if weights is not None:
        active_weights.update(weights)
    weight_sum = active_weights["w_n"] + active_weights["w_t"] + active_weights["w_r"]
    if not math.isclose(weight_sum, 1.0, rel_tol=0.0, abs_tol=1e-9):
        raise ConfidenceError("confidence weights must sum to 1")

    if tier_weight is not None:
        active_tier_weight = dict(tier_weight)
    else:
        active_tier_weight = dict(DEFAULT_TIER_WEIGHT)

    active_n_ref = dict(N_REF)
    if n_ref is not None:
        active_n_ref.update(n_ref)
    if signal_type not in active_n_ref:
        raise ConfidenceError(f"unsupported signal_type: {signal_type!r}")

    active_half_life = dict(HALF_LIFE_DAYS)
    if half_life_days is not None:
        active_half_life.update(half_life_days)
    half_life = active_half_life.get(signal_type, 60)
    if half_life <= 0:
        raise ConfidenceError("half_life_days must be positive")

    n_reference = active_n_ref[signal_type]
    sample_term = _sat(
        math.log1p(sample_n) / math.log1p(n_reference),
    )
    try:
        tier_term = tier_weight_for(source_tier, tier_weight=active_tier_weight)
    except TierError as exc:
        raise ConfidenceError(str(exc)) from exc
    age = _age_days(window_to=window_to, as_of=as_of)
    recency_term = math.exp(-age / half_life)

    confidence = (
        active_weights["w_n"] * sample_term
        + active_weights["w_t"] * tier_term
        + active_weights["w_r"] * recency_term
    )
    return max(0.0, min(1.0, confidence))


def confidence_for_signal_raw(
    signal_raw: Mapping[str, Any],
    *,
    as_of: str,
) -> float:
    """Compute confidence from classify output (signal_raw) before normalize persists."""
    raw_metric = signal_raw.get("raw_metric")
    if not isinstance(raw_metric, Mapping):
        raise ConfidenceError("signal_raw.raw_metric must be an object")
    sample_n = raw_metric.get("sample_n")
    if not isinstance(sample_n, int) or sample_n < 1:
        raise ConfidenceError("signal_raw.raw_metric.sample_n must be a positive integer")

    source = signal_raw.get("source")
    if not isinstance(source, Mapping):
        raise ConfidenceError("signal_raw.source must be an object")
    tier = source.get("tier")
    if not isinstance(tier, str):
        raise ConfidenceError("signal_raw.source.tier must be a string")

    window = signal_raw.get("window")
    if not isinstance(window, Mapping):
        raise ConfidenceError("signal_raw.window must be an object")
    window_to = window.get("to")
    if not isinstance(window_to, str):
        raise ConfidenceError("signal_raw.window.to must be an ISO-8601 timestamp")

    signal_type = signal_raw.get("signal_type")
    if not isinstance(signal_type, str):
        raise ConfidenceError("signal_raw.signal_type must be a string")

    return compute_signal_confidence(
        sample_n=sample_n,
        signal_type=signal_type,
        source_tier=tier,
        window_to=window_to,
        as_of=as_of,
    )


def apply_confidence_to_signal(
    signal: dict[str, Any],
    *,
    sample_n: int,
    as_of: str,
) -> dict[str, Any]:
    """Set signal.v1 confidence deterministically; returns the same dict."""
    signal["confidence"] = compute_signal_confidence(
        sample_n=sample_n,
        signal_type=str(signal["signal_type"]),
        source_tier=str(signal["source"]["tier"]),
        window_to=str(signal["window"]["to"]),
        as_of=as_of,
    )
    return signal


__all__ = [
    "CODE_VERSION",
    "CONFIDENCE_VERSION",
    "ConfidenceError",
    "DEFAULT_WEIGHTS",
    "N_REF",
    "TIER_WEIGHT",
    "apply_confidence_to_signal",
    "compute_signal_confidence",
    "confidence_for_signal_raw",
]
