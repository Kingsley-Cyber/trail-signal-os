"""Flag orphan artifacts and inline-ref/lineage edge disagreements (guard 6)."""

from __future__ import annotations

from dataclasses import dataclass

import psycopg


@dataclass(frozen=True)
class LineageGap:
    artifact_id: str
    artifact_kind: str
    parent_id: str
    issue: str


def flag_lineage_gaps(
    conn: psycopg.Connection,
    *,
    limit: int = 200,
) -> list[LineageGap]:
    """Return inline parent refs on artifacts that lack a matching lineage_edges row."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT
                a.artifact_id,
                a.artifact_kind,
                ref.value AS parent_id
            FROM artifacts a
            CROSS JOIN LATERAL jsonb_array_elements_text(a.derived_from) AS ref(value)
            WHERE jsonb_array_length(a.derived_from) > 0
              AND NOT EXISTS (
                  SELECT 1
                  FROM lineage_edges le
                  WHERE le.child_kind = a.artifact_kind
                    AND le.child_id = a.artifact_id
                    AND le.parent_id = ref.value
              )
            ORDER BY a.created_at DESC, a.artifact_id, ref.value
            LIMIT %s
            """,
            (limit,),
        )
        inline_gaps = [
            LineageGap(
                artifact_id=row[0],
                artifact_kind=row[1],
                parent_id=row[2],
                issue="inline_ref_without_edge",
            )
            for row in cur.fetchall()
        ]

        cur.execute(
            """
            SELECT a.artifact_id, a.artifact_kind, NULL::text
            FROM artifacts a
            WHERE jsonb_array_length(a.derived_from) > 0
              AND NOT EXISTS (
                  SELECT 1
                  FROM lineage_edges le
                  WHERE le.child_kind = a.artifact_kind
                    AND le.child_id = a.artifact_id
              )
            ORDER BY a.created_at DESC, a.artifact_id
            LIMIT %s
            """,
            (limit,),
        )
        orphan_artifacts = [
            LineageGap(
                artifact_id=row[0],
                artifact_kind=row[1],
                parent_id=row[2] or "",
                issue="orphan_artifact_no_edges",
            )
            for row in cur.fetchall()
        ]

    return inline_gaps + orphan_artifacts
