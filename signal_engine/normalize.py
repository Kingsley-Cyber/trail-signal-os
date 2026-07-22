"""Deterministic signal normalization — signal_raw → signal.v1 (N23, LAW 1 deterministic side)."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Sequence

import psycopg
from jsonschema import Draft202012Validator

from db.repositories.constraints import insert_lineage_edge_idempotent
from db.repositories.persist_artifact import persist_artifact
from fixtures.load import SCHEMAS_DIR
from guards.runtime_guards import guard11_assert_normalize_invariants, guard6_require_lineage_edge

CODE_VERSION = "normalize-1.0.0"
NORMALIZE_VERSION = CODE_VERSION
SIGNAL_SCHEMA_VERSION = "signal.v1"

NEGATIVE_SIGNAL_TYPES = frozenset({"competition"})
NEGATIVE_METRIC_NAMES = frozenset(
    {
        "listing_count",
        "listing_density",
        "seller_count",
        "ad_count",
        "ad_intensity",
        "content_saturation",
    }
)

HALF_LIFE_DAYS = {
    "demand": 60,
    "growth": 30,
    "pain": 180,
    "competition": 45,
    "content": 21,
}


class NormalizeError(Exception):
    """Deterministic normalization failed — hard error, never retry."""


def _load_signal_schema() -> dict[str, Any]:
    return json.loads((SCHEMAS_DIR / "signal.v1.schema.json").read_text(encoding="utf-8"))


def validate_signal_v1(signal: dict[str, Any]) -> None:
    Draft202012Validator(_load_signal_schema()).validate(signal)


def winsorize(value: float, cohort: Sequence[float], *, lower_pct: float = 5.0, upper_pct: float = 95.0) -> float:
    """Clip raw value to [p5, p95] of the cohort."""
    if len(cohort) < 2:
        return value
    sorted_vals = sorted(float(v) for v in cohort)
    lo_idx = int((lower_pct / 100.0) * (len(sorted_vals) - 1))
    hi_idx = int((upper_pct / 100.0) * (len(sorted_vals) - 1))
    lo = sorted_vals[lo_idx]
    hi = sorted_vals[hi_idx]
    return max(lo, min(hi, value))


def percentile_rank(cohort: Sequence[float], value: float) -> float:
    """Unit-free percentile rank within cohort, ∈ [0, 1]."""
    sorted_vals = sorted(float(v) for v in cohort)
    n = len(sorted_vals)
    if n == 0:
        raise NormalizeError("cohort must be non-empty")
    if n == 1:
        return 0.5
    below = sum(1 for item in sorted_vals if item < value)
    equal = sum(1 for item in sorted_vals if item == value)
    rank = below + max(equal - 1, 0) / 2.0
    return rank / (n - 1)


def apply_direction(
    score: float,
    *,
    signal_type: str,
    metric_name: str,
) -> tuple[float, bool]:
    """Invert score when higher raw means worse opportunity."""
    if signal_type in NEGATIVE_SIGNAL_TYPES or metric_name in NEGATIVE_METRIC_NAMES:
        return 1.0 - score, True
    return score, True


def build_signal_id(signal_raw: dict[str, Any]) -> str:
    niche_token = signal_raw["niche_id"].split("-")[0]
    return f"sig_{niche_token}_{signal_raw['signal_type']}"


def content_hash_for_signal(signal: dict[str, Any]) -> str:
    canonical = json.dumps(
        {
            "niche_id": signal["niche_id"],
            "signal_type": signal["signal_type"],
            "source": signal["source"],
            "window": signal["window"],
            "normalized_score": signal["normalized_score"],
            "derived_from": signal["derived_from"],
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def _parse_timestamp(value: str) -> datetime:
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    return datetime.fromisoformat(value).astimezone(timezone.utc)


def _format_timestamp(value: datetime) -> str:
    return value.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def normalize_signal_raw(
    signal_raw: dict[str, Any],
    *,
    cohort_raw_values: Sequence[float],
    config_hash: str,
    created_at: str,
    observed_at: str | None = None,
    signal_id: str | None = None,
) -> dict[str, Any]:
    """Map raw metric to normalized signal.v1 using winsorize + percentile rank."""
    raw_metric = signal_raw["raw_metric"]
    raw_value = float(raw_metric["value"])
    metric_name = str(raw_metric["name"])
    signal_type = str(signal_raw["signal_type"])

    cohort = [float(v) for v in cohort_raw_values]
    if raw_value not in cohort:
        cohort = list(cohort) + [raw_value]

    clipped = winsorize(raw_value, cohort)
    ranked = percentile_rank(cohort, clipped)
    normalized_score, direction_applied = apply_direction(
        ranked,
        signal_type=signal_type,
        metric_name=metric_name,
    )

    guard11_assert_normalize_invariants(
        normalized_score=normalized_score,
        window=signal_raw.get("window"),
        direction_applied=direction_applied,
    )

    observed = _parse_timestamp(observed_at or created_at)
    half_life = HALF_LIFE_DAYS.get(signal_type, 60)
    expires_at = _format_timestamp(observed + timedelta(days=half_life))

    signal = {
        "signal_id": signal_id or build_signal_id(signal_raw),
        "niche_id": signal_raw["niche_id"],
        "signal_type": signal_type,
        "source": dict(signal_raw["source"]),
        "window": dict(signal_raw["window"]),
        "normalized_score": normalized_score,
        "confidence": 0.0,
        "observed_at": _format_timestamp(observed),
        "expires_at": expires_at,
        "derived_from": list(signal_raw["evidence_ids"]),
        "provenance": {
            "code_version": NORMALIZE_VERSION,
            "schema_version": SIGNAL_SCHEMA_VERSION,
            "config_hash": config_hash,
            "created_at": created_at,
        },
        "schema_version": SIGNAL_SCHEMA_VERSION,
    }
    validate_signal_v1(signal)
    return signal


def assert_normalize_invariants(signal: dict[str, Any]) -> None:
    """Runtime guard 11 wrapper for normalized signal.v1 artifacts."""
    guard11_assert_normalize_invariants(
        normalized_score=float(signal["normalized_score"]),
        window=signal.get("window"),
        direction_applied=True,
    )


def _lineage_edge_exists(
    conn: psycopg.Connection,
    *,
    signal_id: str,
    evidence_id: str,
) -> bool:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT 1
            FROM lineage_edges
            WHERE child_kind = %s
              AND child_id = %s
              AND parent_kind = 'evidence.v1'
              AND parent_id = %s
              AND relation = 'derived_from'
            """,
            (SIGNAL_SCHEMA_VERSION, signal_id, evidence_id),
        )
        return cur.fetchone() is not None


def persist_signal_v1(
    conn: psycopg.Connection,
    signal: dict[str, Any],
    *,
    evidence_ids: list[str],
    classify_task_id: str,
    storage_root: Path | None = None,
) -> tuple[str, bool, bool]:
    """Persist signal.v1 with LAW 2 lineage edges to each evidence parent."""
    validate_signal_v1(signal)
    assert_normalize_invariants(signal)
    content_hash = content_hash_for_signal(signal)
    artifact_inserted = persist_artifact(
        conn,
        artifact_id=signal["signal_id"],
        content_hash=content_hash,
        artifact_kind=SIGNAL_SCHEMA_VERSION,
        payload=signal,
        derived_from=list(signal["derived_from"]),
        provenance=signal["provenance"],
        created_by_task=classify_task_id,
        schema_version=SIGNAL_SCHEMA_VERSION,
        storage_root=storage_root,
    )

    any_edge = False
    for evidence_id in evidence_ids:
        edge_inserted = insert_lineage_edge_idempotent(
            conn,
            child_kind=SIGNAL_SCHEMA_VERSION,
            child_id=signal["signal_id"],
            parent_kind="evidence.v1",
            parent_id=evidence_id,
            relation="derived_from",
            version_tag=CODE_VERSION,
        )
        any_edge = any_edge or edge_inserted or _lineage_edge_exists(
            conn,
            signal_id=signal["signal_id"],
            evidence_id=evidence_id,
        )

    guard6_require_lineage_edge(
        parent_refs=signal["derived_from"],
        lineage_edge_written=any_edge,
    )
    return signal["signal_id"], artifact_inserted, any_edge


__all__ = [
    "CODE_VERSION",
    "NORMALIZE_VERSION",
    "NormalizeError",
    "apply_direction",
    "assert_normalize_invariants",
    "build_signal_id",
    "content_hash_for_signal",
    "normalize_signal_raw",
    "percentile_rank",
    "persist_signal_v1",
    "validate_signal_v1",
    "winsorize",
]
