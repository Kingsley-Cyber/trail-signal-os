"""Guard 3 helpers — idempotency unique constraints and no-op inserts."""

from __future__ import annotations

import json
from dataclasses import dataclass

import psycopg

GUARD3_UNIQUE_CONSTRAINTS: tuple[tuple[str, str], ...] = (
    ("tasks", "idx_tasks_idempotency"),
    ("lineage_edges", "lineage_edges_unique_edge"),
)


@dataclass(frozen=True)
class UniqueConstraintSpec:
    table_name: str
    constraint_name: str


def assert_guard3_constraints(conn: psycopg.Connection) -> list[UniqueConstraintSpec]:
    """Return specs for guard-3 unique constraints; raise if any are missing."""
    found: list[UniqueConstraintSpec] = []
    missing: list[str] = []
    with conn.cursor() as cur:
        for table_name, constraint_name in GUARD3_UNIQUE_CONSTRAINTS:
            cur.execute(
                """
                SELECT 1
                FROM pg_constraint c
                JOIN pg_class t ON t.oid = c.conrelid
                JOIN pg_namespace n ON n.oid = t.relnamespace
                WHERE n.nspname = 'public'
                  AND t.relname = %s
                  AND c.conname = %s
                  AND c.contype IN ('u', 'p')
                """,
                (table_name, constraint_name),
            )
            if cur.fetchone() is None:
                cur.execute(
                    """
                    SELECT 1
                    FROM pg_indexes
                    WHERE schemaname = 'public'
                      AND tablename = %s
                      AND indexname = %s
                      AND indexdef ILIKE '%%UNIQUE%%'
                    """,
                    (table_name, constraint_name),
                )
                if cur.fetchone() is None:
                    missing.append(f"{table_name}.{constraint_name}")
                    continue
            found.append(UniqueConstraintSpec(table_name, constraint_name))

    if missing:
        raise AssertionError(
            "Guard 3 idempotency constraints missing: " + ", ".join(missing)
        )
    return found


def insert_task_idempotent(
    conn: psycopg.Connection,
    *,
    task_id: str,
    job_id: str,
    lane: str,
    idempotency_key: str,
    payload_ref: str,
    provenance: dict,
) -> bool:
    """Insert a task row; return True when inserted, False on duplicate key no-op."""
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO tasks (
                task_id,
                job_id,
                lane,
                state,
                idempotency_key,
                payload_ref,
                provenance
            )
            VALUES (%s, %s, %s, 'READY', %s, %s, %s::jsonb)
            ON CONFLICT (idempotency_key) DO NOTHING
            RETURNING task_id
            """,
            (task_id, job_id, lane, idempotency_key, payload_ref, json.dumps(provenance)),
        )
        return cur.fetchone() is not None


def insert_lineage_edge_idempotent(
    conn: psycopg.Connection,
    *,
    child_kind: str,
    child_id: str,
    parent_kind: str,
    parent_id: str,
    relation: str,
    version_tag: str | None = None,
) -> bool:
    """Insert a lineage edge; return True when inserted, False on duplicate no-op."""
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO lineage_edges (
                child_kind,
                child_id,
                parent_kind,
                parent_id,
                relation,
                version_tag
            )
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT ON CONSTRAINT lineage_edges_unique_edge DO NOTHING
            RETURNING child_id
            """,
            (child_kind, child_id, parent_kind, parent_id, relation, version_tag),
        )
        return cur.fetchone() is not None
