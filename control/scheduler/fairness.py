"""Weighted round-robin job selection per lane (control_plane_v3 §7)."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass

from control.scheduler.settings import poll_batch_weight


@dataclass(frozen=True)
class AdmissionCandidate:
    task_id: str
    job_id: str
    lane: str
    priority: int
    created_at: object


def select_fair_batch(
    candidates: list[AdmissionCandidate],
    *,
    batch_limit: int,
) -> list[AdmissionCandidate]:
    """Pick up to batch_limit tasks using deficit round-robin weighted by priority."""
    if batch_limit <= 0 or not candidates:
        return []

    by_job: dict[str, deque[AdmissionCandidate]] = {}
    job_weight: dict[str, int] = {}
    job_priority: dict[str, int] = {}
    for candidate in sorted(candidates, key=lambda item: (item.created_at, item.task_id)):
        queue = by_job.setdefault(candidate.job_id, deque())
        queue.append(candidate)
        job_weight[candidate.job_id] = poll_batch_weight(candidate.priority)
        job_priority[candidate.job_id] = candidate.priority

    credits = {job_id: 0 for job_id in by_job}
    selected: list[AdmissionCandidate] = []
    max_weight = max(job_weight.values())

    while len(selected) < batch_limit and any(by_job[job_id] for job_id in by_job):
        for job_id in by_job:
            if by_job[job_id]:
                credits[job_id] += job_weight[job_id]

        eligible = [
            job_id
            for job_id in by_job
            if by_job[job_id] and credits[job_id] >= max_weight
        ]
        if not eligible:
            continue

        job_id = min(
            eligible,
            key=lambda jid: (-credits[jid], job_priority[jid], jid),
        )
        selected.append(by_job[job_id].popleft())
        credits[job_id] -= max_weight

    return selected
