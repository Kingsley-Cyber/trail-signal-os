"""Query and append lineage_edges (LAW 2)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

import psycopg

from db.repositories.constraints import insert_lineage_edge_idempotent


@dataclass(frozen=True)
class LineageEdge:
    child_kind: str
    child_id: str
    parent_kind: str
    parent_id: str
    relation: str
    version_tag: str | None
    created_at: datetime

    def as_dict(self) -> dict[str, Any]:
        created = self.created_at
        if isinstance(created, datetime):
            created_at = created.replace(microsecond=0).isoformat().replace("+00:00", "Z")
        else:
            created_at = str(created)
        return {
            "child_kind": self.child_kind,
            "child_id": self.child_id,
            "parent_kind": self.parent_kind,
            "parent_id": self.parent_id,
            "relation": self.relation,
            "version_tag": self.version_tag,
            "created_at": created_at,
        }


def _row_to_edge(row: tuple[Any, ...]) -> LineageEdge:
    return LineageEdge(
        child_kind=row[0],
        child_id=row[1],
        parent_kind=row[2],
        parent_id=row[3],
        relation=row[4],
        version_tag=row[5],
        created_at=row[6],
    )


_EDGE_COLUMNS = """
    child_kind,
    child_id,
    parent_kind,
    parent_id,
    relation,
    version_tag,
    created_at
"""


def list_edges(
    conn: psycopg.Connection,
    *,
    child_kind: str | None = None,
    child_id: str | None = None,
    parent_kind: str | None = None,
    parent_id: str | None = None,
    limit: int = 500,
) -> list[LineageEdge]:
    """List lineage edges with optional filters."""
    clauses: list[str] = []
    params: list[Any] = []
    if child_kind is not None:
        clauses.append("child_kind = %s")
        params.append(child_kind)
    if child_id is not None:
        clauses.append("child_id = %s")
        params.append(child_id)
    if parent_kind is not None:
        clauses.append("parent_kind = %s")
        params.append(parent_kind)
    if parent_id is not None:
        clauses.append("parent_id = %s")
        params.append(parent_id)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    params.append(limit)
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT {_EDGE_COLUMNS}
            FROM lineage_edges
            {where}
            ORDER BY created_at ASC, child_kind, child_id, parent_kind, parent_id
            LIMIT %s
            """,
            params,
        )
        rows = cur.fetchall()
    return [_row_to_edge(row) for row in rows]


def edges_for_child(
    conn: psycopg.Connection,
    *,
    child_kind: str,
    child_id: str,
) -> list[LineageEdge]:
    return list_edges(conn, child_kind=child_kind, child_id=child_id)


def edges_for_parent(
    conn: psycopg.Connection,
    *,
    parent_kind: str,
    parent_id: str,
) -> list[LineageEdge]:
    return list_edges(conn, parent_kind=parent_kind, parent_id=parent_id)


def write_lineage_edge(
    conn: psycopg.Connection,
    *,
    child_kind: str,
    child_id: str,
    parent_kind: str,
    parent_id: str,
    relation: str,
    version_tag: str | None = None,
) -> bool:
    """Append a lineage edge idempotently; return True when inserted."""
    return insert_lineage_edge_idempotent(
        conn,
        child_kind=child_kind,
        child_id=child_id,
        parent_kind=parent_kind,
        parent_id=parent_id,
        relation=relation,
        version_tag=version_tag,
    )
