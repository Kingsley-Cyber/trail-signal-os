"""Replay leaf query_specs from a lineage root with optional version pins."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import psycopg

from lineage.trace import TraceResult, resolve_root, trace_ancestors


@dataclass(frozen=True)
class ReplayQuerySpec:
    query_spec_id: str
    job_id: str
    text: str
    engine: str
    params: dict[str, Any]
    version_pins: dict[str, str | None]

    def as_dict(self) -> dict[str, Any]:
        return {
            "query_spec_id": self.query_spec_id,
            "job_id": self.job_id,
            "text": self.text,
            "engine": self.engine,
            "params": self.params,
            "version_pins": self.version_pins,
        }


@dataclass(frozen=True)
class ReplayPlan:
    root_kind: str
    root_id: str
    pin_versions: bool
    query_specs: tuple[ReplayQuerySpec, ...]
    trace_edges: tuple[dict[str, Any], ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "root": {"kind": self.root_kind, "id": self.root_id},
            "pin_versions": self.pin_versions,
            "query_specs": [spec.as_dict() for spec in self.query_specs],
            "trace_edge_count": len(self.trace_edges),
            "replayable": len(self.query_specs) > 0,
        }


def _version_pins_for_leaf(
    trace: TraceResult,
    query_spec_id: str,
    *,
    pin_versions: bool,
) -> dict[str, str | None]:
    if not pin_versions:
        return {}
    root = (trace.root_kind, trace.root_id)
    target = ("query_spec", query_spec_id)
    child_to_parent: dict[
        tuple[str, str],
        list[tuple[tuple[str, str], str | None]],
    ] = {}
    for edge in trace.edges:
        child = (edge.child_kind, edge.child_id)
        parent = (edge.parent_kind, edge.parent_id)
        child_to_parent.setdefault(child, []).append((parent, edge.version_tag))

    queue: list[tuple[tuple[str, str], dict[str, str | None]]] = [(root, {})]
    visited: set[tuple[str, str]] = set()
    while queue:
        current, path_pins = queue.pop(0)
        if current in visited:
            continue
        visited.add(current)
        if current == target:
            return path_pins
        for parent, version_tag in child_to_parent.get(current, []):
            rel = f"{current[0]}:{current[1]}->{parent[0]}:{parent[1]}"
            next_pins = dict(path_pins)
            next_pins[rel] = version_tag
            queue.append((parent, next_pins))
    return {}


def replay_lineage(
    conn: psycopg.Connection,
    artifact_id: str,
    *,
    kind: str | None = None,
    pin_versions: bool = True,
) -> ReplayPlan:
    """Re-emit leaf query_specs reachable from artifact_id."""
    root_kind, root_id = resolve_root(conn, artifact_id, kind=kind)
    trace = trace_ancestors(conn, root_kind=root_kind, root_id=root_id)

    replay_specs: list[ReplayQuerySpec] = []
    for leaf in trace.query_spec_leaves:
        qs_id = leaf["query_spec_id"]
        pins = _version_pins_for_leaf(trace, qs_id, pin_versions=pin_versions)
        replay_specs.append(
            ReplayQuerySpec(
                query_spec_id=qs_id,
                job_id=leaf["job_id"],
                text=leaf["text"],
                engine=leaf["engine"],
                params=dict(leaf.get("params") or {}),
                version_pins=pins,
            )
        )

    return ReplayPlan(
        root_kind=root_kind,
        root_id=root_id,
        pin_versions=pin_versions,
        query_specs=tuple(replay_specs),
        trace_edges=tuple(edge.as_dict() for edge in trace.edges),
    )
