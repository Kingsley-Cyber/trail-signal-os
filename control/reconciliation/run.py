"""Orchestrate one reconciler pass across stream/task/counter/artifact checks."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import psycopg

from control.reconciliation.artifact_reconciler import LineageGap, flag_lineage_gaps
from control.reconciliation.counter_reconciler import JobCounterMismatch, flag_counter_mismatches
from control.reconciliation.settings import ReconcilerSettings
from control.reconciliation.stream_reconciler import republish_missing_streams
from control.reconciliation.task_reconciler import (
    TaskInconsistency,
    flag_task_inconsistencies,
    reclaim_task_inconsistencies,
)


@dataclass(frozen=True)
class ReconcilerPassResult:
    republished_streams: int = 0
    reclaimed_tasks: list[str] = field(default_factory=list)
    lineage_gaps: list[LineageGap] = field(default_factory=list)
    counter_mismatches: list[JobCounterMismatch] = field(default_factory=list)
    task_inconsistencies: list[TaskInconsistency] = field(default_factory=list)


def run_reconciler_pass(
    conn: psycopg.Connection,
    redis_client: Any | None = None,
    *,
    settings: ReconcilerSettings | None = None,
) -> ReconcilerPassResult:
    """Run stream repair, lease reclaim, and inconsistency flagging in one pass."""
    cfg = settings or ReconcilerSettings()

    republished = 0
    if redis_client is not None:
        republished = republish_missing_streams(
            conn,
            redis_client,
            batch_size=cfg.stream_batch_size,
        )

    reclaimed = reclaim_task_inconsistencies(conn, limit=cfg.lease_reclaim_limit)
    lineage_gaps = flag_lineage_gaps(conn, limit=cfg.artifact_scan_limit)
    counter_mismatches = flag_counter_mismatches(conn, limit=cfg.counter_scan_limit)
    task_inconsistencies = flag_task_inconsistencies(conn, limit=cfg.lease_reclaim_limit)

    return ReconcilerPassResult(
        republished_streams=republished,
        reclaimed_tasks=reclaimed,
        lineage_gaps=lineage_gaps,
        counter_mismatches=counter_mismatches,
        task_inconsistencies=task_inconsistencies,
    )
