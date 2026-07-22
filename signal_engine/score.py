"""Deterministic opportunity scoring — geo-mean + interactions (N26, LAW 1)."""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

import yaml
from jsonschema import Draft202012Validator

from fixtures.load import SCHEMAS_DIR
from signal_engine.coverage import (
    build_grid_from_signals,
    evaluate_coverage_gate,
    hard_required_met,
)
from signal_engine.normalize import NORMALIZE_VERSION

CODE_VERSION = "score-1.0.0"
SCORING_VERSION = CODE_VERSION
OPPORTUNITY_SCHEMA_VERSION = "opportunity.v1"

DEFAULT_WEIGHTS_PATH = Path(__file__).resolve().parents[1] / "config" / "weights.yaml"

SCORE_DECIMALS = 2
CONFIDENCE_DECIMALS = 2
SUBSCORE_DECIMALS = 3

AXIS_KEYS = ("demand", "growth", "pain", "content")
REQUIRED_CONFIDENCE_AXES = ("demand", "growth", "pain", "competition", "content")


class ScoreError(Exception):
    """Deterministic scoring failed."""


@dataclass(frozen=True)
class ScoreResult:
    score: float
    subscores: dict[str, float]
    confidence: float
    coverage_gaps: tuple[dict[str, str], ...]
    hostile_dependent: bool
    scored_from: tuple[str, ...]
    raw_score: float
    raw_confidence: float


def load_weights(path: Path | None = None) -> dict[str, Any]:
    """Load versioned weights from config/weights.yaml."""
    weights_path = path or DEFAULT_WEIGHTS_PATH
    if not weights_path.is_file():
        raise ScoreError(f"weights file not found: {weights_path}")
    payload = yaml.safe_load(weights_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ScoreError(f"weights file must contain a mapping: {weights_path}")
    _validate_weights(payload)
    return payload


def _validate_weights(weights: Mapping[str, Any]) -> None:
    version = weights.get("version")
    if not isinstance(version, str) or not version:
        raise ScoreError("weights.version must be a non-empty string")

    axis_weights = weights.get("axis_weights")
    if not isinstance(axis_weights, Mapping):
        raise ScoreError("weights.axis_weights must be a mapping")
    axis_sum = sum(float(axis_weights[key]) for key in AXIS_KEYS)
    competition_weight = float(axis_weights.get("competition", 0.0))
    total = axis_sum + competition_weight
    if not math.isclose(total, 1.0, rel_tol=0.0, abs_tol=1e-9):
        raise ScoreError("axis_weights must sum to 1")

    interactions = weights.get("interactions")
    if not isinstance(interactions, Mapping):
        raise ScoreError("weights.interactions must be a mapping")


def shrink_toward_neutral(value: float, confidence: float) -> float:
    """Doc 08 §7(a): low-confidence dimensions stay near neutral."""
    clamped_conf = max(0.0, min(1.0, confidence))
    return 0.5 + (value - 0.5) * clamped_conf


def _round_score(value: float) -> float:
    return round(value, SCORE_DECIMALS)


def _round_confidence(value: float) -> float:
    return round(value, CONFIDENCE_DECIMALS)


def _round_subscore(value: float) -> float:
    return round(value, SUBSCORE_DECIMALS)


def _weighted_geometric_mean(values: Mapping[str, float], weights: Mapping[str, float]) -> float:
    total_weight = sum(float(weights[key]) for key in weights)
    if total_weight <= 0:
        raise ScoreError("geometric mean weights must be positive")
    log_sum = 0.0
    for key, weight in weights.items():
        value = float(values[key])
        if value <= 0:
            raise ScoreError(f"geometric mean requires positive values; {key}={value}")
        log_sum += float(weight) * math.log(value)
    return math.exp(log_sum / total_weight)


def _geometric_mean(values: Sequence[float]) -> float:
    if not values:
        raise ScoreError("geometric mean requires at least one value")
    product = math.prod(values)
    if product <= 0:
        raise ScoreError("geometric mean requires positive values")
    return product ** (1.0 / len(values))


def _extract_signal_list(signals: Sequence[Mapping[str, Any]] | Mapping[str, Any]) -> list[dict[str, Any]]:
    if isinstance(signals, Mapping) and "signals" in signals:
        raw_items = signals["signals"]
        if not isinstance(raw_items, list):
            raise ScoreError("signals bundle must contain a signals array")
        return [dict(item) for item in raw_items]
    if isinstance(signals, Sequence) and not isinstance(signals, (str, bytes)):
        return [dict(item) for item in signals]
    raise ScoreError("signals must be a sequence or a bundle with a signals array")


def _extract_ad_intensity(
    signals: Sequence[Mapping[str, Any]] | Mapping[str, Any],
    *,
    ad_intensity: Mapping[str, Any] | float | None,
) -> tuple[float, float | None]:
    if ad_intensity is not None:
        if isinstance(ad_intensity, (int, float)):
            return float(ad_intensity), None
        if isinstance(ad_intensity, Mapping):
            value = ad_intensity.get("normalized_score")
            if not isinstance(value, (int, float)):
                raise ScoreError("ad_intensity.normalized_score must be numeric")
            confidence = ad_intensity.get("confidence")
            conf_value = float(confidence) if isinstance(confidence, (int, float)) else None
            return float(value), conf_value
        raise ScoreError("ad_intensity must be numeric or a mapping")

    if isinstance(signals, Mapping):
        bundle_value = signals.get("ad_intensity")
        if isinstance(bundle_value, Mapping):
            normalized = bundle_value.get("normalized_score")
            if isinstance(normalized, (int, float)):
                confidence = bundle_value.get("confidence")
                conf_value = float(confidence) if isinstance(confidence, (int, float)) else None
                return float(normalized), conf_value
    return 0.5, None


def _select_best_signals(signals: Sequence[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    selected: dict[str, dict[str, Any]] = {}
    for signal in signals:
        signal_type = str(signal["signal_type"])
        if signal_type not in REQUIRED_CONFIDENCE_AXES:
            continue
        candidate_conf = float(signal["confidence"])
        existing = selected.get(signal_type)
        if existing is None:
            selected[signal_type] = dict(signal)
            continue
        existing_conf = float(existing["confidence"])
        if candidate_conf > existing_conf:
            selected[signal_type] = dict(signal)
            continue
        if candidate_conf == existing_conf and str(signal["signal_id"]) < str(existing["signal_id"]):
            selected[signal_type] = dict(signal)
    return selected


def _axis_inputs(
    selected: Mapping[str, Mapping[str, Any]],
) -> tuple[dict[str, float], dict[str, float], dict[str, float]]:
    normalized: dict[str, float] = {}
    confidences: dict[str, float] = {}
    shrunk: dict[str, float] = {}

    for axis in AXIS_KEYS:
        signal = selected.get(axis)
        if signal is None:
            normalized[axis] = 0.5
            confidences[axis] = 0.0
        else:
            normalized[axis] = float(signal["normalized_score"])
            confidences[axis] = float(signal["confidence"])
        shrunk[axis] = shrink_toward_neutral(normalized[axis], confidences[axis])

    competition = selected.get("competition")
    if competition is None:
        competition_score = 0.5
        competition_conf = 0.0
    else:
        competition_score = float(competition["normalized_score"])
        competition_conf = float(competition["confidence"])

    competition_shrunk = shrink_toward_neutral(competition_score, competition_conf)
    uncrowded = 1.0 - competition_shrunk
    normalized["competition"] = competition_score
    confidences["competition"] = competition_conf
    shrunk["competition"] = uncrowded

    return normalized, confidences, shrunk


def _compute_hostile_dependent(
    *,
    niche_id: str,
    signals: Sequence[Mapping[str, Any]],
    as_of: str,
    min_cell_confidence: float,
) -> bool:
    full_grid = build_grid_from_signals(
        niche_id=niche_id,
        signals=signals,
        updated_at=as_of,
    )
    if not hard_required_met(full_grid, min_cell_confidence=min_cell_confidence):
        return False

    without_hostile = [
        signal
        for signal in signals
        if str(signal.get("source", {}).get("tier", "")) != "hostile"
    ]
    reduced_grid = build_grid_from_signals(
        niche_id=niche_id,
        signals=without_hostile,
        updated_at=as_of,
    )
    return not hard_required_met(reduced_grid, min_cell_confidence=min_cell_confidence)


def compute_score_result(
    signals: Sequence[Mapping[str, Any]] | Mapping[str, Any],
    weights: Mapping[str, Any],
    *,
    niche_id: str,
    as_of: str,
    ad_intensity: Mapping[str, Any] | float | None = None,
    dossier_deadline_at: str | None = None,
) -> ScoreResult:
    """Pure scoring path: normalized signals + weights → ScoreResult."""
    signal_items = _extract_signal_list(signals)
    if not signal_items:
        raise ScoreError("at least one signal is required")

    min_cell_confidence = float(weights.get("min_cell_confidence", 0.40))
    grid = build_grid_from_signals(
        niche_id=niche_id,
        signals=signal_items,
        updated_at=as_of,
    )
    gate = evaluate_coverage_gate(
        grid,
        as_of=as_of,
        dossier_deadline_at=dossier_deadline_at,
        min_cell_confidence=min_cell_confidence,
    )
    if not gate.admitted and not gate.scores_with_gaps:
        raise ScoreError("coverage gate blocked scoring")

    selected = _select_best_signals(signal_items)
    _, confidences, shrunk = _axis_inputs(selected)

    ad_value, ad_confidence = _extract_ad_intensity(signals, ad_intensity=ad_intensity)
    if ad_confidence is None:
        ad_shrunk = ad_value
    else:
        ad_shrunk = shrink_toward_neutral(ad_value, ad_confidence)

    axis_weights = {key: float(weights["axis_weights"][key]) for key in AXIS_KEYS}
    axis_weights["competition"] = float(weights["axis_weights"]["competition"])
    base_inputs = {
        "demand": shrunk["demand"],
        "growth": shrunk["growth"],
        "pain": shrunk["pain"],
        "competition": shrunk["competition"],
        "content": shrunk["content"],
    }
    base = _weighted_geometric_mean(base_inputs, axis_weights)

    lambda_gap = float(weights["interactions"]["lambda_gap"])
    lambda_pain = float(weights["interactions"]["lambda_pain"])
    demand_gap = shrunk["demand"] * shrunk["competition"]
    underserved_pain = shrunk["pain"] * (1.0 - ad_shrunk)
    raw_final = base * (1.0 + lambda_gap * demand_gap + lambda_pain * underserved_pain)
    raw_final = max(0.0, min(1.0, raw_final))

    raw_confidence = _geometric_mean([confidences[axis] for axis in REQUIRED_CONFIDENCE_AXES])
    hostile_dependent = _compute_hostile_dependent(
        niche_id=niche_id,
        signals=signal_items,
        as_of=as_of,
        min_cell_confidence=min_cell_confidence,
    )
    if hostile_dependent:
        raw_confidence = min(raw_confidence, 0.50)

    subscores = {
        "demand": _round_subscore(shrunk["demand"]),
        "growth": _round_subscore(shrunk["growth"]),
        "pain": _round_subscore(shrunk["pain"]),
        "competition": _round_subscore(shrunk["competition"]),
        "content": _round_subscore(shrunk["content"]),
    }
    scored_from = tuple(
        str(selected[axis]["signal_id"])
        for axis in REQUIRED_CONFIDENCE_AXES
        if axis in selected
    )

    return ScoreResult(
        score=_round_score(raw_final),
        subscores=subscores,
        confidence=_round_confidence(raw_confidence),
        coverage_gaps=gate.coverage_gaps,
        hostile_dependent=hostile_dependent,
        scored_from=scored_from,
        raw_score=raw_final,
        raw_confidence=raw_confidence,
    )


def score(
    signals: Sequence[Mapping[str, Any]] | Mapping[str, Any],
    weights: Mapping[str, Any] | None = None,
    *,
    weights_path: Path | None = None,
    niche_id: str | None = None,
    as_of: str | None = None,
    ad_intensity: Mapping[str, Any] | float | None = None,
    dossier_deadline_at: str | None = None,
) -> ScoreResult:
    """Deterministic score(signals, weights) → opportunity value + subscores."""
    active_weights = load_weights(weights_path) if weights is None else dict(weights)
    _validate_weights(active_weights)

    signal_items = _extract_signal_list(signals)
    resolved_niche_id = niche_id
    if resolved_niche_id is None:
        if isinstance(signals, Mapping) and isinstance(signals.get("niche_id"), str):
            resolved_niche_id = signals["niche_id"]
        elif signal_items:
            resolved_niche_id = str(signal_items[0]["niche_id"])
        else:
            raise ScoreError("niche_id is required")

    resolved_as_of = as_of
    if resolved_as_of is None:
        resolved_as_of = str(signal_items[0]["observed_at"])

    return compute_score_result(
        signals,
        active_weights,
        niche_id=resolved_niche_id,
        as_of=resolved_as_of,
        ad_intensity=ad_intensity,
        dossier_deadline_at=dossier_deadline_at,
    )


def _load_opportunity_schema() -> dict[str, Any]:
    return json.loads((SCHEMAS_DIR / "opportunity.v1.schema.json").read_text(encoding="utf-8"))


def validate_opportunity_v1(opportunity: dict[str, Any]) -> None:
    Draft202012Validator(_load_opportunity_schema()).validate(opportunity)


def build_opportunity_v1(
    *,
    opportunity_id: str,
    niche_id: str,
    candidate: Mapping[str, Any],
    score_result: ScoreResult,
    weights: Mapping[str, Any],
    config_hash: str,
    as_of: str,
    generating_queries: Sequence[str] = (),
    created_at: str | None = None,
) -> dict[str, Any]:
    """Build a schema-valid opportunity.v1 from a ScoreResult."""
    timestamp = created_at or as_of
    opportunity = {
        "opportunity_id": opportunity_id,
        "niche_id": niche_id,
        "candidate": dict(candidate),
        "score": score_result.score,
        "subscores": dict(score_result.subscores),
        "confidence": score_result.confidence,
        "coverage_gaps": [dict(gap) for gap in score_result.coverage_gaps],
        "hostile_dependent": score_result.hostile_dependent,
        "scored_from": list(score_result.scored_from),
        "generating_queries": list(generating_queries),
        "provenance": {
            "scoring_version": SCORING_VERSION,
            "weights_version": str(weights["version"]),
            "normalize_version": NORMALIZE_VERSION,
            "config_hash": config_hash,
            "created_at": timestamp,
        },
        "as_of": as_of,
        "schema_version": OPPORTUNITY_SCHEMA_VERSION,
    }
    validate_opportunity_v1(opportunity)
    return opportunity


def content_hash_for_opportunity(opportunity: dict[str, Any]) -> str:
    canonical = json.dumps(
        {
            "niche_id": opportunity["niche_id"],
            "candidate": opportunity["candidate"],
            "score": opportunity["score"],
            "subscores": opportunity["subscores"],
            "confidence": opportunity["confidence"],
            "scored_from": opportunity["scored_from"],
            "provenance": opportunity["provenance"],
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def score_camping_fixture(
    *,
    weights_path: Path | None = None,
    config_hash: str = "sha256:" + ("a" * 64),
) -> dict[str, Any]:
    """Score the offline camping-fixture bundle into opportunity.v1."""
    from fixtures.load import load_fixtures

    corpus = load_fixtures()
    camping = corpus.camping_signals
    weights = load_weights(weights_path)
    result = score(camping, weights)
    expected = corpus.camping_expected
    candidate = dict(expected["candidate"])
    return build_opportunity_v1(
        opportunity_id=str(expected["opportunity_id"]),
        niche_id=str(camping["niche_id"]),
        candidate=candidate,
        score_result=result,
        weights=weights,
        config_hash=config_hash,
        as_of=str(expected["as_of"]),
        generating_queries=list(expected.get("generating_queries", [])),
        created_at=str(expected["provenance"]["created_at"]),
    )


__all__ = [
    "CODE_VERSION",
    "DEFAULT_WEIGHTS_PATH",
    "OPPORTUNITY_SCHEMA_VERSION",
    "SCORING_VERSION",
    "ScoreError",
    "ScoreResult",
    "build_opportunity_v1",
    "compute_score_result",
    "content_hash_for_opportunity",
    "load_weights",
    "score",
    "score_camping_fixture",
    "shrink_toward_neutral",
    "validate_opportunity_v1",
]
