from __future__ import annotations
import json
from pathlib import Path
from typing import Mapping

DIMENSIONS = (
    "behavior_frequency", "friction_severity", "workaround_strength",
    "complaint_repetition", "product_simplicity", "shipping_fit",
    "margin_potential", "return_risk_inverse", "community_reachability",
    "competition_gap", "seasonal_timing", "expansion_potential", "risk_inverse",
)


def load_scoring_config(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _numeric(value: object, field: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be numeric, got {value!r}") from exc
    if not 0 <= number <= 5:
        raise ValueError(f"{field} must be between 0 and 5, got {number}")
    return number


def score_candidate(row: Mapping[str, object], config: Mapping[str, object]) -> dict[str, object]:
    weights = config["dimensions"]
    weighted = 0.0
    total_weight = 0.0
    for dimension in DIMENSIONS:
        value = _numeric(row.get(dimension, ""), dimension)
        weight = float(weights[dimension])
        weighted += value * weight
        total_weight += weight
    normalized = (weighted / (5 * total_weight)) * 100

    penalties_applied: list[str] = []
    penalties = config.get("penalties", {})
    evidence_ids = str(row.get("evidence_ids", "")).strip()
    score_basis = str(row.get("score_basis", "seed_prior")).strip()
    if score_basis != "seed_prior" and not evidence_ids:
        normalized -= float(penalties.get("unverified_metric", 0))
        penalties_applied.append("unverified_metric")
    if str(row.get("risk_inverse", "0")) in {"0", "1"}:
        normalized -= float(penalties.get("high_safety_dependency", 0))
        penalties_applied.append("high_safety_dependency")
    normalized = max(0.0, min(100.0, normalized))

    band = "unknown"
    for label, bounds in config.get("bands", {}).items():
        low, high = float(bounds[0]), float(bounds[1])
        if low <= normalized <= high:
            band = label
            break
    gate = str(row.get("hard_gates_passed", "false")).lower() == "true"
    return {
        "weighted_score": f"{normalized:.2f}",
        "score_band": band,
        "hard_gates_passed": str(gate).lower(),
        "experiment_eligible": str(gate and normalized >= 65).lower(),
        "penalties_applied": ";".join(penalties_applied),
    }
