"""N9 reconciler — republish missing streams, orphan flagging, lease reclaim tests."""

from __future__ import annotations

import json
import os
import unittest
from datetime import timedelta
from pathlib import Path

import psycopg

from control.dispatcher import (
    connect_redis,
    enqueue_ready_task,
    publish_pending_outbox,
    resolve_stream_name,
)
from control.dispatcher.republish import stream_contains_event
from control.leases import acquire_lease
from control.reconciliation import run_reconciler_pass
from control.reconciliation.artifact_reconciler import flag_lineage_gaps
from control.reconciliation.stream_reconciler import republish_missing_streams
from control.reconciliation.task_reconciler import reclaim_task_inconsistencies
from db.repositories.connection import connect
from db.repositories.migrate import apply_migrations

REPO_ROOT = Path(__file__).resolve().parents[1]
ENV_FILE = REPO_ROOT / ".env"
CONFIG_HASH = "sha256:" + ("a" * 64)
CREATED_AT = "2026-07-21T12:00:00Z"
LEASE_DURATION = timedelta(seconds=60)

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


def _redis_available() -> bool:
    _load_dotenv()
    try:
        client = connect_redis()
        client.ping()
        return True
    except Exception:
        return False


def _infra_available() -> bool:
    return _postgres_available() and _redis_available()


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


def _task_provenance() -> dict:
    return {
        "schema_version": "task.v1",
        "config_hash": CONFIG_HASH,
        "created_at": CREATED_AT,
    }


def _insert_job(conn: psycopg.Connection, job_id: str) -> None:
    conn.execute(
        """
        INSERT INTO research_jobs (
            job_id, job_kind, status, config_hash, budget, provenance
        )
        VALUES (%s, %s, %s, %s, %s::jsonb, %s::jsonb)
        ON CONFLICT (job_id) DO NOTHING
        """,
        _sample_job(job_id),
    )


def _insert_artifact(
    conn: psycopg.Connection,
    *,
    artifact_id: str,
    derived_from: list[str],
    artifact_kind: str = "page.v1",
) -> None:
    conn.execute(
        """
        INSERT INTO artifacts (
            artifact_id,
            content_hash,
            storage_uri,
            artifact_kind,
            derived_from,
            provenance
        )
        VALUES (%s, %s, %s, %s, %s::jsonb, %s::jsonb)
        """,
        (
            artifact_id,
            "sha256:" + ("f" * 64),
            f"file://artifacts/{artifact_id}.json",
            artifact_kind,
            json.dumps(derived_from),
            json.dumps(
                {
                    "code_version": "extract-1.0.0",
                    "schema_version": artifact_kind,
                    "config_hash": CONFIG_HASH,
                    "created_at": CREATED_AT,
                }
            ),
        ),
    )


def _clear_stream(redis_client, stream_name: str) -> None:
    redis_client.delete(stream_name)


@unittest.skipUnless(_infra_available(), "Postgres+Redis unavailable (need .env)")
class ReconcilerStreamTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.conn = connect()
        apply_migrations(cls.conn)
        cls.redis = connect_redis()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.conn.close()

    def setUp(self) -> None:
        self.conn.execute("SAVEPOINT n9_stream_case")

    def tearDown(self) -> None:
        self.conn.execute("ROLLBACK TO SAVEPOINT n9_stream_case")

    def test_republish_missing_stream_messages_via_reconciler(self) -> None:
        task_id = "tsk_n9_republish"
        job_id = "job_n9_republish"
        stream_name = resolve_stream_name("browser", 2)
        _clear_stream(self.redis, stream_name)
        _insert_job(self.conn, job_id)

        result = enqueue_ready_task(
            self.conn,
            task_id=task_id,
            job_id=job_id,
            lane="browser",
            idempotency_key="sha256:" + ("e9" * 32),
            payload_ref=f"postgres://tasks/{task_id}",
            provenance=_task_provenance(),
            created_at=CREATED_AT,
        )
        self.assertEqual(publish_pending_outbox(self.conn, self.redis), 1)
        self.assertTrue(
            stream_contains_event(self.redis, result.stream_name, result.event_id)
        )

        self.redis.delete(result.stream_name)
        self.assertEqual(self.redis.xlen(result.stream_name), 0)

        republished = republish_missing_streams(self.conn, self.redis)
        self.assertEqual(republished, 1)
        self.assertEqual(self.redis.xlen(result.stream_name), 1)
        self.assertTrue(
            stream_contains_event(self.redis, result.stream_name, result.event_id)
        )

    def test_run_reconciler_pass_republish(self) -> None:
        task_id = "tsk_n9_pass_republish"
        job_id = "job_n9_pass_republish"
        stream_name = resolve_stream_name("http", 2)
        _clear_stream(self.redis, stream_name)
        _insert_job(self.conn, job_id)

        result = enqueue_ready_task(
            self.conn,
            task_id=task_id,
            job_id=job_id,
            lane="http",
            idempotency_key="sha256:" + ("f9" * 32),
            payload_ref=f"postgres://tasks/{task_id}",
            provenance=_task_provenance(),
            created_at=CREATED_AT,
        )
        self.assertEqual(publish_pending_outbox(self.conn, self.redis), 1)
        self.redis.delete(result.stream_name)

        pass_result = run_reconciler_pass(self.conn, self.redis)
        self.assertEqual(pass_result.republished_streams, 1)
        self.assertTrue(
            stream_contains_event(self.redis, result.stream_name, result.event_id)
        )


@unittest.skipUnless(_postgres_available(), "Postgres unavailable (need .env POSTGRES_*)")
class ReconcilerArtifactTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.conn = connect()
        apply_migrations(cls.conn)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.conn.close()

    def setUp(self) -> None:
        self.conn.execute("SAVEPOINT n9_artifact_case")

    def tearDown(self) -> None:
        self.conn.execute("ROLLBACK TO SAVEPOINT n9_artifact_case")

    def test_flags_inline_ref_without_lineage_edge(self) -> None:
        artifact_id = "pg_n9_orphan_inline"
        parent_task = "tsk_n9_orphan_parent"
        _insert_artifact(
            self.conn,
            artifact_id=artifact_id,
            derived_from=[parent_task],
        )

        gaps = flag_lineage_gaps(self.conn)
        matching = [
            gap
            for gap in gaps
            if gap.artifact_id == artifact_id and gap.parent_id == parent_task
        ]
        self.assertEqual(len(matching), 1)
        self.assertEqual(matching[0].issue, "inline_ref_without_edge")

    def test_run_reconciler_pass_flags_orphan_artifact(self) -> None:
        artifact_id = "pg_n9_orphan_pass"
        _insert_artifact(
            self.conn,
            artifact_id=artifact_id,
            derived_from=["tsk_n9_orphan_pass_parent"],
        )

        pass_result = run_reconciler_pass(self.conn, redis_client=None)
        self.assertTrue(
            any(
                gap.artifact_id == artifact_id
                and gap.issue == "inline_ref_without_edge"
                for gap in pass_result.lineage_gaps
            )
        )


@unittest.skipUnless(_postgres_available(), "Postgres unavailable (need .env POSTGRES_*)")
class ReconcilerTaskTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.conn = connect()
        apply_migrations(cls.conn)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.conn.close()

    def setUp(self) -> None:
        self.conn.execute("SAVEPOINT n9_task_case")

    def tearDown(self) -> None:
        self.conn.execute("ROLLBACK TO SAVEPOINT n9_task_case")

    def test_reclaim_expired_leases_via_reconciler(self) -> None:
        job_id = "job_n9_reclaim"
        task_id = "tsk_n9_reclaim"
        _insert_job(self.conn, job_id)
        enqueue_ready_task(
            self.conn,
            task_id=task_id,
            job_id=job_id,
            lane="http",
            idempotency_key="sha256:" + ("r9" * 32),
            payload_ref=f"postgres://tasks/{task_id}",
            provenance=_task_provenance(),
            created_at=CREATED_AT,
        )
        acquire_lease(
            self.conn,
            task_id=task_id,
            worker_id="worker_n9_stale",
            lease_duration=LEASE_DURATION,
        )
        self.conn.execute(
            """
            UPDATE tasks
            SET lease_expires_at = NOW() - INTERVAL '1 minute'
            WHERE task_id = %s
            """,
            (task_id,),
        )

        reclaimed = reclaim_task_inconsistencies(self.conn)
        self.assertIn(task_id, reclaimed)

        with self.conn.cursor() as cur:
            cur.execute("SELECT state FROM tasks WHERE task_id = %s", (task_id,))
            row = cur.fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row[0], "READY")


class IntegrationCheckReconciler(unittest.TestCase):
    """Integration check hook for gate-verifier (republish missing; flag orphans)."""

    @unittest.skipUnless(_infra_available(), "Postgres+Redis unavailable (need .env)")
    def test_republish_missing_streams(self) -> None:
        suite = unittest.TestSuite()
        suite.addTest(
            ReconcilerStreamTests("test_republish_missing_stream_messages_via_reconciler")
        )
        suite.addTest(ReconcilerStreamTests("test_run_reconciler_pass_republish"))
        result = unittest.TextTestRunner(verbosity=0).run(suite)
        self.assertTrue(result.wasSuccessful())

    @unittest.skipUnless(_postgres_available(), "Postgres unavailable (need .env POSTGRES_*)")
    def test_flag_orphan_artifacts(self) -> None:
        suite = unittest.TestSuite()
        suite.addTest(
            ReconcilerArtifactTests("test_flags_inline_ref_without_lineage_edge")
        )
        suite.addTest(
            ReconcilerArtifactTests("test_run_reconciler_pass_flags_orphan_artifact")
        )
        result = unittest.TextTestRunner(verbosity=0).run(suite)
        self.assertTrue(result.wasSuccessful())

    @unittest.skipUnless(_postgres_available(), "Postgres unavailable (need .env POSTGRES_*)")
    def test_reclaim_lease_inconsistency(self) -> None:
        suite = unittest.TestSuite()
        suite.addTest(ReconcilerTaskTests("test_reclaim_expired_leases_via_reconciler"))
        result = unittest.TextTestRunner(verbosity=0).run(suite)
        self.assertTrue(result.wasSuccessful())


if __name__ == "__main__":
    unittest.main()
