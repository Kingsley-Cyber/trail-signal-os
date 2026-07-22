"""Half-lives, expiry, and re-collection triggers (N29, LAW 1 deterministic)."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

import psycopg

from control.scheduler import admit_task
from db.repositories.constraints import insert_lineage_edge_idempotent
from signal_engine.normalize import HALF_LIFE_DAYS
from signal_engine.score import ScoreError, ScoreResult, load_weights, score

CODE_VERSION = "freshness-1.0.0"
FRESHNESS_VERSION = CODE_VERSION

DEFAULT_OPPORTUNITY_TTL_DAYS = 14
SEARCH_LANE = "search"
RECOLLECT_RELATION = "recollect_for"
RECOLLECT_TASK_KIND = "recollection"


class FreshnessError(Exception):
    """Deterministic freshness evaluation failed."""


@dataclass(frozen=True)
class FreshnessEvaluation:
    as_of: str
    active_signals: tuple[dict[str, Any], ...]
    expired_signals: tuple[dict[str, Any], ...]
    expired_signal_ids: tuple[str, ...]
    needs_recollection: bool


@dataclass(frozen=True)
class RecollectionResult:
    task_id: str
    query_spec_id: str
    job_id: str
    admitted: bool
    admission_reason: str
    idempotency_key: str
    lineage_edges_written: int


def _parse_timestamp(value: str) -> datetime:
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    return datetime.fromisoformat(value).astimezone(timezone.utc)


def _format_timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def load_half_life_days(weights_path: Path | None = None) -> dict[str, int]:
    """Load half-life table from config/weights.yaml (doc 08 §10)."""
    weights = load_weights(weights_path)
    half_life = weights.get("half_life_days")
    if not isinstance(half_life, Mapping):
        raise FreshnessError("weights.half_life_days must be a mapping")
    return {str(key): int(value) for key, value in half_life.items()}


def compute_expires_at(
    observed_at: str,
    signal_type: str,
    *,
    half_life_days: Mapping[str, int] | None = None,
) -> str:
    """Compute signal.expires_at from observed_at + half-life (doc 08 §10)."""
    active = dict(HALF_LIFE_DAYS)
    if half_life_days is not None:
        active.update(half_life_days)
    half_life = active.get(signal_type, 60)
    if half_life <= 0:
        raise FreshnessError("half_life_days must be positive")
    observed = _parse_timestamp(observed_at)
    return _format_timestamp(observed + timedelta(days=half_life))


def is_signal_expired(signal: Mapping[str, Any], *, as_of: str) -> bool:
    """True when as_of is at or past signal.expires_at."""
    expires_at = signal.get("expires_at")
    if not isinstance(expires_at, str) or not expires_at:
        raise FreshnessError("signal.expires_at must be a non-empty ISO timestamp")
    return _parse_timestamp(as_of) >= _parse_timestamp(expires_at)


def _extract_signal_list(
    signals: Sequence[Mapping[str, Any]] | Mapping[str, Any],
) -> list[dict[str, Any]]:
    if isinstance(signals, Mapping) and "signals" in signals:
        raw_items = signals["signals"]
        if not isinstance(raw_items, list):
            raise FreshnessError("signals bundle must contain a signals array")
        return [dict(item) for item in raw_items]
    if isinstance(signals, Sequence) and not isinstance(signals, (str, bytes)):
        return [dict(item) for item in signals]
    raise FreshnessError("signals must be a sequence or a bundle with a signals array")


def partition_signals(
    signals: Sequence[Mapping[str, Any]] | Mapping[str, Any],
    *,
    as_of: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Split signals into (active, expired) at as_of."""
    active: list[dict[str, Any]] = []
    expired: list[dict[str, Any]] = []
    for signal in _extract_signal_list(signals):
        if is_signal_expired(signal, as_of=as_of):
            expired.append(signal)
        else:
            active.append(signal)
    return active, expired


def filter_active_signals(
    signals: Sequence[Mapping[str, Any]] | Mapping[str, Any],
    *,
    as_of: str,
) -> list[dict[str, Any]]:
    """Exclude expired signals before deterministic scoring (doc 08 §10)."""
    active, _expired = partition_signals(signals, as_of=as_of)
    return active


def evaluate_freshness(
    signals: Sequence[Mapping[str, Any]] | Mapping[str, Any],
    *,
    as_of: str,
) -> FreshnessEvaluation:
    """Evaluate signal freshness and whether re-collection is required."""
    active, expired = partition_signals(signals, as_of=as_of)
    expired_ids = tuple(str(item["signal_id"]) for item in expired)
    return FreshnessEvaluation(
        as_of=as_of,
        active_signals=tuple(active),
        expired_signals=tuple(expired),
        expired_signal_ids=expired_ids,
        needs_recollection=bool(expired),
    )


def active_signal_bundle(
    signals: Mapping[str, Any],
    *,
    as_of: str,
) -> dict[str, Any]:
    """Return a signals bundle containing only non-expired signals."""
    bundle = dict(signals)
    bundle["signals"] = filter_active_signals(signals, as_of=as_of)
    return bundle


def score_active_signals(
    signals: Mapping[str, Any],
    weights: Mapping[str, Any] | None = None,
    *,
    as_of: str,
    **score_kwargs: Any,
) -> tuple[ScoreResult, FreshnessEvaluation]:
    """Score only non-expired signals; returns score + freshness evaluation."""
    evaluation = evaluate_freshness(signals, as_of=as_of)
    bundle = active_signal_bundle(signals, as_of=as_of)
    if not evaluation.active_signals:
        raise ScoreError("no active signals remain after freshness filter")
    result = score(bundle, weights, as_of=as_of, **score_kwargs)
    return result, evaluation


def opportunity_expires_at(
    opportunity: Mapping[str, Any],
    *,
    ttl_days: int = DEFAULT_OPPORTUNITY_TTL_DAYS,
) -> datetime:
    """Compute opportunity staleness deadline from as_of + TTL (v4 §10)."""
    as_of = opportunity.get("as_of")
    if not isinstance(as_of, str) or not as_of:
        raise FreshnessError("opportunity.as_of must be a non-empty ISO timestamp")
    if ttl_days < 0:
        raise FreshnessError("ttl_days must be non-negative")
    return _parse_timestamp(as_of) + timedelta(days=ttl_days)


def is_opportunity_stale(
    opportunity: Mapping[str, Any],
    *,
    as_of: str,
    signals: Sequence[Mapping[str, Any]] | Mapping[str, Any] | None = None,
    ttl_days: int = DEFAULT_OPPORTUNITY_TTL_DAYS,
) -> bool:
    """True when as_of is past opportunity TTL or any scored_from signal is expired."""
    now = _parse_timestamp(as_of)
    if now >= opportunity_expires_at(opportunity, ttl_days=ttl_days):
        return True
    if signals is None:
        return False
    scored_from = opportunity.get("scored_from")
    if not isinstance(scored_from, list):
        return False
    signal_index = {
        str(item["signal_id"]): item for item in _extract_signal_list(signals)
    }
    for signal_id in scored_from:
        signal = signal_index.get(str(signal_id))
        if signal is not None and is_signal_expired(signal, as_of=as_of):
            return True
    return False


def apply_opportunity_expiry(
    opportunity: Mapping[str, Any],
    *,
    as_of: str,
    signals: Sequence[Mapping[str, Any]] | Mapping[str, Any] | None = None,
    ttl_days: int = DEFAULT_OPPORTUNITY_TTL_DAYS,
) -> dict[str, Any]:
    """Mark opportunity EXPIRED when stale; otherwise leave status unchanged."""
    updated = dict(opportunity)
    if is_opportunity_stale(
        updated,
        as_of=as_of,
        signals=signals,
        ttl_days=ttl_days,
    ):
        updated["status"] = "EXPIRED"
    return updated


def make_recollection_task_id(job_id: str, query_spec_id: str) -> str:
    digest = hashlib.sha256(f"recollect|{job_id}|{query_spec_id}".encode()).hexdigest()[:12]
    return f"tsk_rec_{digest}"


def make_recollection_idempotency_key(signal_id: str, query_spec_id: str) -> str:
    digest = hashlib.sha256(f"recollect|{signal_id}|{query_spec_id}".encode()).hexdigest()
    return f"idem_rec_{digest}"


def _task_provenance(*, config_hash: str, created_at: str, expired_signal_ids: Sequence[str]) -> dict[str, Any]:
    return {
        "schema_version": "task.v1",
        "config_hash": config_hash,
        "created_at": created_at,
        "code_version": FRESHNESS_VERSION,
        "expired_signal_ids": list(expired_signal_ids),
    }


def _insert_pending_recollection_task(
    conn: psycopg.Connection,
    *,
    task_id: str,
    job_id: str,
    query_spec_id: str,
    idempotency_key: str,
    provenance: dict[str, Any],
) -> bool:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO tasks (
                task_id,
                job_id,
                task_kind,
                lane,
                priority,
                state,
                idempotency_key,
                payload_ref,
                provenance
            )
            VALUES (%s, %s, %s, %s, 2, 'PENDING', %s, %s, %s::jsonb)
            ON CONFLICT (idempotency_key) DO NOTHING
            RETURNING task_id
            """,
            (
                task_id,
                job_id,
                RECOLLECT_TASK_KIND,
                SEARCH_LANE,
                idempotency_key,
                query_spec_id,
                json.dumps(provenance),
            ),
        )
        return cur.fetchone() is not None


def enqueue_recollection_for_query(
    conn: psycopg.Connection,
    *,
    job_id: str,
    query_spec_id: str,
    expired_signal_ids: Sequence[str],
    config_hash: str,
    created_at: str,
    admit: bool = True,
) -> RecollectionResult:
    """Enqueue a PENDING search-lane re-collection task and admit via scheduler (N7)."""
    if not expired_signal_ids:
        raise FreshnessError("expired_signal_ids must be non-empty")
    task_id = make_recollection_task_id(job_id, query_spec_id)
    idempotency_key = make_recollection_idempotency_key(
        "|".join(sorted(expired_signal_ids)),
        query_spec_id,
    )
    provenance = _task_provenance(
        config_hash=config_hash,
        created_at=created_at,
        expired_signal_ids=expired_signal_ids,
    )
    inserted = _insert_pending_recollection_task(
        conn,
        task_id=task_id,
        job_id=job_id,
        query_spec_id=query_spec_id,
        idempotency_key=idempotency_key,
        provenance=provenance,
    )
    edges_written = 0
    for signal_id in expired_signal_ids:
        if insert_lineage_edge_idempotent(
            conn,
            child_kind="task",
            child_id=task_id,
            parent_kind="signal",
            parent_id=str(signal_id),
            relation=RECOLLECT_RELATION,
            version_tag=FRESHNESS_VERSION,
        ):
            edges_written += 1
    if insert_lineage_edge_idempotent(
        conn,
        child_kind="task",
        child_id=task_id,
        parent_kind="query_spec",
        parent_id=query_spec_id,
        relation=RECOLLECT_RELATION,
        version_tag=FRESHNESS_VERSION,
    ):
        edges_written += 1

    admitted = False
    admission_reason = "duplicate_idempotency_key"
    if inserted and admit:
        admission = admit_task(conn, task_id=task_id)
        admitted = admission.admitted
        admission_reason = admission.reason
    elif inserted:
        admission_reason = "admit_skipped"

    return RecollectionResult(
        task_id=task_id,
        query_spec_id=query_spec_id,
        job_id=job_id,
        admitted=admitted,
        admission_reason=admission_reason,
        idempotency_key=idempotency_key,
        lineage_edges_written=edges_written,
    )


def trigger_recollection_for_expiry(
    conn: psycopg.Connection,
    *,
    evaluation: FreshnessEvaluation,
    job_id: str,
    generating_queries: Sequence[str],
    config_hash: str,
    created_at: str,
    admit: bool = True,
) -> list[RecollectionResult]:
    """Expiry → re-collection: enqueue search tasks for each generating query (v4 §10)."""
    if not evaluation.needs_recollection:
        return []
    if not generating_queries:
        raise FreshnessError("generating_queries required when signals expired")
    results: list[RecollectionResult] = []
    for query_spec_id in generating_queries:
        results.append(
            enqueue_recollection_for_query(
                conn,
                job_id=job_id,
                query_spec_id=str(query_spec_id),
                expired_signal_ids=evaluation.expired_signal_ids,
                config_hash=config_hash,
                created_at=created_at,
                admit=admit,
            ),
        )
    return results


__all__ = [
    "CODE_VERSION",
    "DEFAULT_OPPORTUNITY_TTL_DAYS",
    "FRESHNESS_VERSION",
    "FreshnessError",
    "FreshnessEvaluation",
    "RecollectionResult",
    "active_signal_bundle",
    "apply_opportunity_expiry",
    "compute_expires_at",
    "enqueue_recollection_for_query",
    "evaluate_freshness",
    "filter_active_signals",
    "is_opportunity_stale",
    "is_signal_expired",
    "load_half_life_days",
    "make_recollection_idempotency_key",
    "make_recollection_task_id",
    "opportunity_expires_at",
    "partition_signals",
    "score_active_signals",
    "trigger_recollection_for_expiry",
]
