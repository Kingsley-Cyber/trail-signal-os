"""Transitive parent DAG walk — reaches query_spec leaves."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

import psycopg

from lineage.edges import LineageEdge, edges_for_child


@dataclass(frozen=True)
class TraceNode:
    kind: str
    node_id: str
    relation_to_parent: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def key(self) -> tuple[str, str]:
        return (self.kind, self.node_id)

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "kind": self.kind,
            "id": self.node_id,
        }
        if self.relation_to_parent is not None:
            payload["relation_to_parent"] = self.relation_to_parent
        if self.metadata:
            payload["metadata"] = self.metadata
        return payload


@dataclass(frozen=True)
class TraceResult:
    root_kind: str
    root_id: str
    nodes: tuple[TraceNode, ...]
    edges: tuple[LineageEdge, ...]
    query_spec_leaves: tuple[dict[str, Any], ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "root": {"kind": self.root_kind, "id": self.root_id},
            "nodes": [node.as_dict() for node in self.nodes],
            "edges": [edge.as_dict() for edge in self.edges],
            "query_spec_leaves": list(self.query_spec_leaves),
            "complete_to_query_spec": len(self.query_spec_leaves) > 0,
        }


def resolve_root(
    conn: psycopg.Connection,
    artifact_id: str,
    *,
    kind: str | None = None,
) -> tuple[str, str]:
    """Resolve artifact_id to (kind, id); infer kind from artifacts when omitted."""
    if kind is not None:
        return kind, artifact_id
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT artifact_kind
            FROM artifacts
            WHERE artifact_id = %s
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (artifact_id,),
        )
        row = cur.fetchone()
    if row is not None:
        return row[0], artifact_id
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT 1
            FROM query_specs
            WHERE query_spec_id = %s
            """,
            (artifact_id,),
        )
        if cur.fetchone() is not None:
            return "query_spec", artifact_id
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT 1
            FROM tasks
            WHERE task_id = %s
            """,
            (artifact_id,),
        )
        if cur.fetchone() is not None:
            return "task", artifact_id
    raise LookupError(f"unknown artifact_id: {artifact_id}")


def _fetch_query_specs(
    conn: psycopg.Connection,
    query_spec_ids: set[str],
) -> dict[str, dict[str, Any]]:
    if not query_spec_ids:
        return {}
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT query_spec_id, job_id, text, engine, params, created_at
            FROM query_specs
            WHERE query_spec_id = ANY(%s)
            """,
            (list(query_spec_ids),),
        )
        rows = cur.fetchall()
    specs: dict[str, dict[str, Any]] = {}
    for row in rows:
        created_at = row[5]
        if hasattr(created_at, "replace"):
            created_at = created_at.replace(microsecond=0).isoformat().replace("+00:00", "Z")
        params = row[4]
        if isinstance(params, str):
            params = json.loads(params)
        specs[row[0]] = {
            "query_spec_id": row[0],
            "job_id": row[1],
            "text": row[2],
            "engine": row[3],
            "params": params,
            "created_at": created_at,
        }
    return specs


def _fetch_artifact_metadata(
    conn: psycopg.Connection,
    artifact_id: str,
) -> dict[str, Any]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT artifact_kind, content_hash, derived_from, provenance
            FROM artifacts
            WHERE artifact_id = %s
            LIMIT 1
            """,
            (artifact_id,),
        )
        row = cur.fetchone()
    if row is None:
        return {}
    derived_from = row[2]
    provenance = row[3]
    if isinstance(derived_from, str):
        derived_from = json.loads(derived_from)
    if isinstance(provenance, str):
        provenance = json.loads(provenance)
    return {
        "artifact_kind": row[0],
        "content_hash": row[1],
        "derived_from": derived_from,
        "provenance": provenance,
    }


def _node_metadata(
    conn: psycopg.Connection,
    kind: str,
    node_id: str,
) -> dict[str, Any]:
    if kind == "query_spec":
        specs = _fetch_query_specs(conn, {node_id})
        return specs.get(node_id, {})
    if kind == "page.v1" or kind.endswith(".v1"):
        meta = _fetch_artifact_metadata(conn, node_id)
        if meta:
            return meta
    if kind == "task":
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT job_id, lane, task_kind, payload_ref, state
                FROM tasks
                WHERE task_id = %s
                """,
                (node_id,),
            )
            row = cur.fetchone()
        if row is not None:
            return {
                "job_id": row[0],
                "lane": row[1],
                "task_kind": row[2],
                "payload_ref": row[3],
                "state": row[4],
            }
    return {}


def trace_ancestors(
    conn: psycopg.Connection,
    *,
    root_kind: str,
    root_id: str,
) -> TraceResult:
    """Walk lineage_edges upward from root; collect nodes, edges, and query_spec leaves."""
    visited: set[tuple[str, str]] = set()
    queue: list[tuple[str, str, str | None]] = [(root_kind, root_id, None)]
    collected_edges: dict[tuple[str, str, str, str], LineageEdge] = {}
    nodes: dict[tuple[str, str], TraceNode] = {}

    while queue:
        kind, node_id, relation = queue.pop(0)
        key = (kind, node_id)
        if key in visited:
            continue
        visited.add(key)
        metadata = _node_metadata(conn, kind, node_id)
        nodes[key] = TraceNode(
            kind=kind,
            node_id=node_id,
            relation_to_parent=relation,
            metadata=metadata,
        )
        for edge in edges_for_child(conn, child_kind=kind, child_id=node_id):
            edge_key = (
                edge.child_kind,
                edge.child_id,
                edge.parent_kind,
                edge.parent_id,
            )
            collected_edges[edge_key] = edge
            parent_key = (edge.parent_kind, edge.parent_id)
            if parent_key not in visited:
                queue.append((edge.parent_kind, edge.parent_id, edge.relation))

    query_spec_ids = {node_id for kind, node_id in visited if kind == "query_spec"}
    specs = _fetch_query_specs(conn, query_spec_ids)
    leaves = tuple(specs[qs_id] for qs_id in sorted(specs))

    ordered_nodes = tuple(nodes[key] for key in sorted(nodes))
    ordered_edges = tuple(
        collected_edges[key] for key in sorted(collected_edges)
    )
    return TraceResult(
        root_kind=root_kind,
        root_id=root_id,
        nodes=ordered_nodes,
        edges=ordered_edges,
        query_spec_leaves=leaves,
    )


def trace(
    conn: psycopg.Connection,
    artifact_id: str,
    *,
    kind: str | None = None,
) -> TraceResult:
    root_kind, root_id = resolve_root(conn, artifact_id, kind=kind)
    return trace_ancestors(conn, root_kind=root_kind, root_id=root_id)
