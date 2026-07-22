"""Compare two lineage graphs — nodes, edges, and version tags."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import psycopg

from lineage.trace import TraceResult, trace_ancestors


@dataclass(frozen=True)
class LineageDiff:
    left: dict[str, str]
    right: dict[str, str]
    nodes_only_left: tuple[dict[str, str], ...]
    nodes_only_right: tuple[dict[str, str], ...]
    shared_nodes: tuple[dict[str, str], ...]
    edges_only_left: tuple[dict[str, Any], ...]
    edges_only_right: tuple[dict[str, Any], ...]
    shared_edges: tuple[dict[str, Any], ...]
    version_tag_changes: tuple[dict[str, Any], ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "left": self.left,
            "right": self.right,
            "nodes_only_left": list(self.nodes_only_left),
            "nodes_only_right": list(self.nodes_only_right),
            "shared_nodes": list(self.shared_nodes),
            "edges_only_left": list(self.edges_only_left),
            "edges_only_right": list(self.edges_only_right),
            "shared_edges": list(self.shared_edges),
            "version_tag_changes": list(self.version_tag_changes),
            "identical": (
                not self.nodes_only_left
                and not self.nodes_only_right
                and not self.edges_only_left
                and not self.edges_only_right
                and not self.version_tag_changes
            ),
        }


def _node_key(node_kind: str, node_id: str) -> tuple[str, str]:
    return (node_kind, node_id)


def _edge_key(edge: dict[str, Any]) -> tuple[str, str, str, str]:
    return (
        edge["child_kind"],
        edge["child_id"],
        edge["parent_kind"],
        edge["parent_id"],
    )


def _trace_to_sets(
    trace: TraceResult,
) -> tuple[set[tuple[str, str]], dict[tuple[str, str, str, str], dict[str, Any]]]:
    node_keys = {(node.kind, node.node_id) for node in trace.nodes}
    edge_map: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    for edge in trace.edges:
        payload = edge.as_dict()
        edge_map[_edge_key(payload)] = payload
    return node_keys, edge_map


def diff_lineage(
    conn: psycopg.Connection,
    *,
    left_kind: str,
    left_id: str,
    right_kind: str,
    right_id: str,
) -> LineageDiff:
    """Compare ancestor graphs of two roots."""
    left_trace = trace_ancestors(conn, root_kind=left_kind, root_id=left_id)
    right_trace = trace_ancestors(conn, root_kind=right_kind, root_id=right_id)

    left_nodes, left_edges = _trace_to_sets(left_trace)
    right_nodes, right_edges = _trace_to_sets(right_trace)

    only_left = left_nodes - right_nodes
    only_right = right_nodes - left_nodes
    shared = left_nodes & right_nodes

    left_edge_keys = set(left_edges)
    right_edge_keys = set(right_edges)
    edges_only_left = left_edge_keys - right_edge_keys
    edges_only_right = right_edge_keys - left_edge_keys
    shared_edge_keys = left_edge_keys & right_edge_keys

    version_changes: list[dict[str, Any]] = []
    for key in sorted(shared_edge_keys):
        left_edge = left_edges[key]
        right_edge = right_edges[key]
        if left_edge.get("version_tag") != right_edge.get("version_tag"):
            version_changes.append(
                {
                    "edge": {
                        "child_kind": key[0],
                        "child_id": key[1],
                        "parent_kind": key[2],
                        "parent_id": key[3],
                    },
                    "left_version_tag": left_edge.get("version_tag"),
                    "right_version_tag": right_edge.get("version_tag"),
                }
            )

    def node_dict(key: tuple[str, str]) -> dict[str, str]:
        return {"kind": key[0], "id": key[1]}

    return LineageDiff(
        left={"kind": left_kind, "id": left_id},
        right={"kind": right_kind, "id": right_id},
        nodes_only_left=tuple(node_dict(k) for k in sorted(only_left)),
        nodes_only_right=tuple(node_dict(k) for k in sorted(only_right)),
        shared_nodes=tuple(node_dict(k) for k in sorted(shared)),
        edges_only_left=tuple(left_edges[k] for k in sorted(edges_only_left)),
        edges_only_right=tuple(right_edges[k] for k in sorted(edges_only_right)),
        shared_edges=tuple(left_edges[k] for k in sorted(shared_edge_keys)),
        version_tag_changes=tuple(version_changes),
    )
