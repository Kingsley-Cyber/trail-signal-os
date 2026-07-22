"""N2 db — migration apply and guard 3 idempotency constraint tests."""

from __future__ import annotations

import json
import os
import unittest
from pathlib import Path

import psycopg

from db.repositories.connection import connect, load_postgres_settings
from db.repositories.constraints import (
    GUARD3_UNIQUE_CONSTRAINTS,
    assert_guard3_constraints,
    insert_lineage_edge_idempotent,
    insert_task_idempotent,
)
from db.repositories.migrate import apply_migrations, migrations_dir

REPO_ROOT = Path(__file__).resolve().parents[1]
ENV_FILE = REPO_ROOT / ".env"
CONFIG_HASH = "sha256:" + ("a" * 64)
IDEMPOTENCY_A = "sha256:" + ("c" * 64)
IDEMPOTENCY_B = "sha256:" + ("d" * 64)
CREATED_AT = "2026-07-21T12:00:00Z"

BUDGET = {
    "max_queries": 10,
    "max_fetched_urls": 100,
    "per_domain_urls": 50,
    "browser_pages": 5,
    "media_items": 10,
    "max_bytes": 1048576,
    "deadline_minutes": 30,
    "max_attempts": 3,
    "llm_budget": {"max_calls": 10, "max_tokens": 10000, "max_usd": 0},
    "schema_version": "budget.v1",
}


def _load_dotenv() -> None:
    if not ENV_FILE.is_file():
        return
    for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())


def _postgres_available() -> bool:
    _load_dotenv()
    if not os.environ.get("POSTGRES_PASSWORD"):
        return False
    try:
        with connect() as conn:
            conn.execute("SELECT 1")
        return True
    except (psycopg.Error, RuntimeError):
        return False


def _sample_job(job_id: str) -> tuple:
    provenance = {
        "schema_version": "job.v1",
        "config_hash": CONFIG_HASH,
        "created_at": CREATED_AT,
    }
    return (
        job_id,
        "dossier",
        "CREATED",
        CONFIG_HASH,
        json.dumps(BUDGET),
        json.dumps(provenance),
    )


class MigrationArtifactTests(unittest.TestCase):
    def test_migrations_directory_has_ordered_sql(self) -> None:
        files = sorted(migrations_dir().glob("*.sql"))
        self.assertGreaterEqual(len(files), 2)
        names = [path.name for path in files]
        self.assertIn("001_schema_migrations.sql", names)
        self.assertIn("002_core_foundation.sql", names)

    def test_core_migration_declares_guard3_uniques(self) -> None:
        sql = (migrations_dir() / "002_core_foundation.sql").read_text(encoding="utf-8")
        self.assertIn("CREATE UNIQUE INDEX idx_tasks_idempotency", sql)
        self.assertIn("lineage_edges_unique_edge", sql)
        self.assertIn("UNIQUE (child_kind, child_id, parent_kind, parent_id)", sql)

    def test_lineage_edges_append_only_triggers_present(self) -> None:
        sql = (migrations_dir() / "002_core_foundation.sql").read_text(encoding="utf-8")
        self.assertIn("prevent_lineage_edge_mutation", sql)
        self.assertIn("lineage_edges_no_update", sql)
        self.assertIn("lineage_edges_no_delete", sql)


@unittest.skipUnless(_postgres_available(), "Postgres unavailable (need .env POSTGRES_*)")
class PostgresMigrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.conn = connect()
        apply_migrations(cls.conn)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.conn.close()

    def setUp(self) -> None:
        self.conn.execute("SAVEPOINT n2_test_case")

    def tearDown(self) -> None:
        self.conn.execute("ROLLBACK TO SAVEPOINT n2_test_case")

    def test_migrations_apply_idempotently(self) -> None:
        first = apply_migrations(self.conn)
        second = apply_migrations(self.conn)
        self.assertEqual(first, [])
        self.assertEqual(second, [])

    def test_guard3_unique_constraints_exist(self) -> None:
        specs = assert_guard3_constraints(self.conn)
        self.assertEqual(len(specs), len(GUARD3_UNIQUE_CONSTRAINTS))

    def test_duplicate_task_idempotency_key_is_no_op(self) -> None:
        job_id = "job_n2_guard3_task"
        task_id = "tsk_n2_guard3_task"
        provenance = {
            "schema_version": "task.v1",
            "config_hash": CONFIG_HASH,
            "created_at": CREATED_AT,
        }
        self.conn.execute(
            """
            INSERT INTO research_jobs (
                job_id, job_kind, status, config_hash, budget, provenance
            )
            VALUES (%s, %s, %s, %s, %s::jsonb, %s::jsonb)
            """,
            _sample_job(job_id),
        )
        inserted = insert_task_idempotent(
            self.conn,
            task_id=task_id,
            job_id=job_id,
            lane="http",
            idempotency_key=IDEMPOTENCY_A,
            payload_ref=f"postgres://tasks/{task_id}",
            provenance=provenance,
        )
        duplicate = insert_task_idempotent(
            self.conn,
            task_id="tsk_n2_guard3_task_dup",
            job_id=job_id,
            lane="http",
            idempotency_key=IDEMPOTENCY_A,
            payload_ref="postgres://tasks/tsk_n2_guard3_task_dup",
            provenance=provenance,
        )
        with self.conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM tasks WHERE idempotency_key = %s",
                (IDEMPOTENCY_A,),
            )
            count = cur.fetchone()[0]
        self.assertTrue(inserted)
        self.assertFalse(duplicate)
        self.assertEqual(count, 1)

    def test_duplicate_lineage_edge_is_no_op(self) -> None:
        first = insert_lineage_edge_idempotent(
            self.conn,
            child_kind="page.v1",
            child_id="pg_n2_test",
            parent_kind="query_spec",
            parent_id="qs_n2_test",
            relation="derived_from",
            version_tag="extract-1.0.0",
        )
        second = insert_lineage_edge_idempotent(
            self.conn,
            child_kind="page.v1",
            child_id="pg_n2_test",
            parent_kind="query_spec",
            parent_id="qs_n2_test",
            relation="derived_from",
            version_tag="extract-1.0.0",
        )
        with self.conn.cursor() as cur:
            cur.execute(
                """
                SELECT COUNT(*)
                FROM lineage_edges
                WHERE child_kind = %s
                  AND child_id = %s
                  AND parent_kind = %s
                  AND parent_id = %s
                """,
                ("page.v1", "pg_n2_test", "query_spec", "qs_n2_test"),
            )
            count = cur.fetchone()[0]
        self.assertTrue(first)
        self.assertFalse(second)
        self.assertEqual(count, 1)

    def test_lineage_edges_reject_update(self) -> None:
        insert_lineage_edge_idempotent(
            self.conn,
            child_kind="artifact",
            child_id="art_n2_immutable",
            parent_kind="task",
            parent_id="tsk_n2_immutable",
            relation="produced_by",
        )
        with self.assertRaises(psycopg.errors.RaiseException):
            with self.conn.transaction():
                self.conn.execute(
                    """
                    UPDATE lineage_edges
                    SET relation = 'mutated'
                    WHERE child_id = %s
                    """,
                    ("art_n2_immutable",),
                )


class IntegrationCheckMigrations(unittest.TestCase):
    """Integration check hook for gate-verifier (guard 3 when Postgres is up)."""

    def test_guard3_constraint_catalog_matches_migration(self) -> None:
        sql = (migrations_dir() / "002_core_foundation.sql").read_text(encoding="utf-8")
        for table_name, name in GUARD3_UNIQUE_CONSTRAINTS:
            self.assertIn(name, sql, msg=f"missing guard-3 constraint {table_name}.{name}")

    @unittest.skipUnless(_postgres_available(), "Postgres unavailable (need .env POSTGRES_*)")
    def test_live_guard3_constraints_after_migrate(self) -> None:
        with connect() as conn:
            apply_migrations(conn)
            assert_guard3_constraints(conn)


if __name__ == "__main__":
    unittest.main()
