"""N5 leases — fencing poison (guard 2) and reaper reclaim integration tests."""

from __future__ import annotations

import json
import os
import unittest
from datetime import timedelta
from pathlib import Path

import psycopg

from control.leases import (
    acquire_lease,
    heartbeat,
    reclaim_expired_leases,
    update_task_fenced,
)
from db.repositories.connection import connect
from db.repositories.constraints import insert_task_idempotent
from db.repositories.migrate import apply_migrations
from guards.exceptions import StaleLeaseError

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


def _insert_ready_task(
    conn: psycopg.Connection,
    *,
    task_id: str,
    job_id: str,
    idempotency_key: str,
) -> None:
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
    insert_task_idempotent(
        conn,
        task_id=task_id,
        job_id=job_id,
        lane="http",
        idempotency_key=idempotency_key,
        payload_ref=f"postgres://tasks/{task_id}",
        provenance=_task_provenance(),
    )


def _fetch_task(conn: psycopg.Connection, task_id: str) -> tuple:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT state, lease_owner, lease_generation, lease_expires_at
            FROM tasks
            WHERE task_id = %s
            """,
            (task_id,),
        )
        row = cur.fetchone()
    assert row is not None
    return row


@unittest.skipUnless(_postgres_available(), "Postgres unavailable (need .env POSTGRES_*)")
class PostgresLeaseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.conn = connect()
        apply_migrations(cls.conn)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.conn.close()

    def setUp(self) -> None:
        self.conn.execute("SAVEPOINT n5_test_case")

    def tearDown(self) -> None:
        self.conn.execute("ROLLBACK TO SAVEPOINT n5_test_case")

    def test_acquire_and_heartbeat_extend_lease(self) -> None:
        task_id = "tsk_n5_heartbeat"
        job_id = "job_n5_heartbeat"
        worker_id = "worker-a"
        _insert_ready_task(
            self.conn,
            task_id=task_id,
            job_id=job_id,
            idempotency_key="sha256:" + ("h1" * 32),
        )

        acquired = acquire_lease(
            self.conn,
            task_id=task_id,
            worker_id=worker_id,
            lease_duration=LEASE_DURATION,
        )
        self.assertIsNotNone(acquired)
        assert acquired is not None
        self.assertEqual(acquired.lease_generation, 1)

        before = _fetch_task(self.conn, task_id)
        self.assertEqual(before[0], "LEASED")
        self.assertEqual(before[1], worker_id)

        refreshed = heartbeat(
            self.conn,
            task_id=task_id,
            worker_id=worker_id,
            lease_generation=acquired.lease_generation,
            lease_duration=LEASE_DURATION,
        )
        self.assertIsNotNone(refreshed)
        after = _fetch_task(self.conn, task_id)
        self.assertGreaterEqual(after[3], before[3])

    def test_stale_generation_write_raises_stale_lease_error(self) -> None:
        task_id = "tsk_n5_fencing_poison"
        job_id = "job_n5_fencing_poison"
        worker_a = "worker-a"
        worker_b = "worker-b"
        _insert_ready_task(
            self.conn,
            task_id=task_id,
            job_id=job_id,
            idempotency_key="sha256:" + ("f1" * 32),
        )

        first = acquire_lease(
            self.conn,
            task_id=task_id,
            worker_id=worker_a,
            lease_duration=LEASE_DURATION,
        )
        self.assertIsNotNone(first)
        assert first is not None
        stale_generation = first.lease_generation

        self.conn.execute(
            """
            UPDATE tasks
            SET lease_expires_at = NOW() - INTERVAL '1 minute'
            WHERE task_id = %s
            """,
            (task_id,),
        )
        second = acquire_lease(
            self.conn,
            task_id=task_id,
            worker_id=worker_b,
            lease_duration=LEASE_DURATION,
        )
        self.assertIsNotNone(second)
        assert second is not None
        self.assertGreater(second.lease_generation, stale_generation)

        with self.assertRaises(StaleLeaseError) as ctx:
            update_task_fenced(
                self.conn,
                task_id=task_id,
                worker_id=worker_a,
                lease_generation=stale_generation,
                new_state="SUCCEEDED",
                result_artifact_id="art_n5_stale",
            )
        self.assertIn("0 rows", str(ctx.exception))

        state, owner, generation, _ = _fetch_task(self.conn, task_id)
        self.assertEqual(state, "LEASED")
        self.assertEqual(owner, worker_b)
        self.assertEqual(generation, second.lease_generation)

    def test_reaper_reclaims_expired_lease(self) -> None:
        task_id = "tsk_n5_reaper"
        job_id = "job_n5_reaper"
        worker_a = "worker-a"
        worker_b = "worker-b"
        _insert_ready_task(
            self.conn,
            task_id=task_id,
            job_id=job_id,
            idempotency_key="sha256:" + ("r1" * 32),
        )

        acquired = acquire_lease(
            self.conn,
            task_id=task_id,
            worker_id=worker_a,
            lease_duration=LEASE_DURATION,
        )
        self.assertIsNotNone(acquired)
        assert acquired is not None
        first_generation = acquired.lease_generation

        self.conn.execute(
            """
            UPDATE tasks
            SET lease_expires_at = NOW() - INTERVAL '1 minute'
            WHERE task_id = %s
            """,
            (task_id,),
        )

        reclaimed = reclaim_expired_leases(self.conn)
        self.assertIn(task_id, reclaimed)

        state, owner, generation, expires_at = _fetch_task(self.conn, task_id)
        self.assertEqual(state, "READY")
        self.assertIsNone(owner)
        self.assertIsNone(expires_at)
        self.assertEqual(generation, first_generation)

        reacquired = acquire_lease(
            self.conn,
            task_id=task_id,
            worker_id=worker_b,
            lease_duration=LEASE_DURATION,
        )
        self.assertIsNotNone(reacquired)
        assert reacquired is not None
        self.assertEqual(reacquired.worker_id, worker_b)
        self.assertGreater(reacquired.lease_generation, first_generation)


class IntegrationCheckLeases(unittest.TestCase):
    """Integration check hook for gate-verifier (guard 2 fencing + reaper reclaim)."""

    @unittest.skipUnless(_postgres_available(), "Postgres unavailable (need .env POSTGRES_*)")
    def test_fencing_poison_guard2(self) -> None:
        suite = unittest.TestSuite()
        suite.addTest(
            PostgresLeaseTests("test_stale_generation_write_raises_stale_lease_error")
        )
        result = unittest.TextTestRunner(verbosity=0).run(suite)
        self.assertTrue(result.wasSuccessful())

    @unittest.skipUnless(_postgres_available(), "Postgres unavailable (need .env POSTGRES_*)")
    def test_reaper_reclaim(self) -> None:
        suite = unittest.TestSuite()
        suite.addTest(PostgresLeaseTests("test_reaper_reclaims_expired_lease"))
        result = unittest.TextTestRunner(verbosity=0).run(suite)
        self.assertTrue(result.wasSuccessful())


if __name__ == "__main__":
    unittest.main()
