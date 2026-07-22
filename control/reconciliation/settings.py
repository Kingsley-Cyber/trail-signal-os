"""Reconciler batch limits."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ReconcilerSettings:
    stream_batch_size: int = 100
    lease_reclaim_limit: int = 100
    artifact_scan_limit: int = 200
    counter_scan_limit: int = 100
