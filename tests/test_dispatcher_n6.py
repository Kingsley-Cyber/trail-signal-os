"""N6 dispatcher+outbox — guard 4 atomicity and restart-Redis republish tests."""

from __future__ import annotations

import json
import os
import unittest
from pathlib import Path

import psycopg

from control.dispatcher import (
    connect_redis,
    enqueue_ready_task,
    publish_pending_outbox,
    republish_missing_stream_messages,
    resolve_stream_name,
)
from control.dispatcher.publish import publish_outbox_event
from control.dispatcher.republish import stream_contains_event
from db.repositories.connection import connect
from db.repositories.migrate import apply_migrations

REPO_ROOT = Path(__file__).resolve().parents[1]
ENV_FILE = REPO_ROOT / ".env"
CONFIG_HASH = "sha256:" + ("a" * 64)
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


def _count_outbox_for_task(conn: psycopg.Connection, task_id: str) -> int:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) FROM outbox_events WHERE task_id = %s",
            (task_id,),
        )
        row = cur.fetchone()
    assert row is not None
    return int(row[0])


def _fetch_outbox(conn: psycopg.Connection, event_id: int) -> tuple:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT stream_name, published_at
            FROM outbox_events
            WHERE event_id = %s
            """,
            (event_id,),
        )
        row = cur.fetchone()
    assert row is not None
    return row


def _clear_stream(redis_client, stream_name: str) -> None:
    redis_client.delete(stream_name)


@unittest.skipUnless(_infra_available(), "Postgres+Redis unavailable (need .env)")
class DispatcherOutboxTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.conn = connect()
        apply_migrations(cls.conn)
        cls.redis = connect_redis()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.conn.close()

    def setUp(self) -> None:
        self.conn.execute("SAVEPOINT n6_test_case")

    def tearDown(self) -> None:
        self.conn.execute("ROLLBACK TO SAVEPOINT n6_test_case")

    def test_enqueue_inserts_task_and_outbox_atomically(self) -> None:
        task_id = "tsk_n6_atomic"
        job_id = "job_n6_atomic"
        stream_name = resolve_stream_name("http", 2)
        _clear_stream(self.redis, stream_name)
        _insert_job(self.conn, job_id)

        result = enqueue_ready_task(
            self.conn,
            task_id=task_id,
            job_id=job_id,
            lane="http",
            priority=2,
            idempotency_key="sha256:" + ("a1" * 32),
            payload_ref=f"postgres://tasks/{task_id}",
            provenance=_task_provenance(),
            created_at=CREATED_AT,
        )

        self.assertEqual(result.task_id, task_id)
        self.assertEqual(result.stream_name, resolve_stream_name("http", 2))
        stream_name, published_at = _fetch_outbox(self.conn, result.event_id)
        self.assertEqual(stream_name, "cp:http:normal")
        self.assertIsNone(published_at)
        self.assertEqual(self.redis.xlen(stream_name), 0)

    def test_failed_enqueue_rolls_back_outbox(self) -> None:
        task_id = "tsk_n6_rollback"
        job_id = "job_n6_rollback"
        _insert_job(self.conn, job_id)
        key = "sha256:" + ("b1" * 32)

        enqueue_ready_task(
            self.conn,
            task_id=task_id,
            job_id=job_id,
            lane="http",
            idempotency_key=key,
            payload_ref=f"postgres://tasks/{task_id}",
            provenance=_task_provenance(),
            created_at=CREATED_AT,
        )

        with self.assertRaises(psycopg.Error):
            enqueue_ready_task(
                self.conn,
                task_id=task_id,
                job_id=job_id,
                lane="http",
                idempotency_key="sha256:" + ("b2" * 32),
                payload_ref=f"postgres://tasks/{task_id}",
                provenance=_task_provenance(),
                created_at=CREATED_AT,
            )

        self.assertEqual(_count_outbox_for_task(self.conn, task_id), 1)

    def test_unpublished_outbox_published_after_crash_before_xadd(self) -> None:
        task_id = "tsk_n6_crash"
        job_id = "job_n6_crash"
        stream_name = resolve_stream_name("http", 2)
        _clear_stream(self.redis, stream_name)
        _insert_job(self.conn, job_id)

        result = enqueue_ready_task(
            self.conn,
            task_id=task_id,
            job_id=job_id,
            lane="http",
            idempotency_key="sha256:" + ("c1" * 32),
            payload_ref=f"postgres://tasks/{task_id}",
            provenance=_task_provenance(),
            created_at=CREATED_AT,
        )

        _, published_at = _fetch_outbox(self.conn, result.event_id)
        self.assertIsNone(published_at)
        self.assertEqual(self.redis.xlen(result.stream_name), 0)

        published = publish_pending_outbox(self.conn, self.redis)
        self.assertEqual(published, 1)

        _, published_at = _fetch_outbox(self.conn, result.event_id)
        self.assertIsNotNone(published_at)
        self.assertEqual(self.redis.xlen(result.stream_name), 1)
        self.assertTrue(
            stream_contains_event(self.redis, result.stream_name, result.event_id)
        )

    def test_publish_is_idempotent(self) -> None:
        task_id = "tsk_n6_idempotent"
        job_id = "job_n6_idempotent"
        stream_name = resolve_stream_name("extract", 2)
        _clear_stream(self.redis, stream_name)
        _insert_job(self.conn, job_id)

        result = enqueue_ready_task(
            self.conn,
            task_id=task_id,
            job_id=job_id,
            lane="extract",
            idempotency_key="sha256:" + ("d1" * 32),
            payload_ref=f"postgres://tasks/{task_id}",
            provenance=_task_provenance(),
            created_at=CREATED_AT,
        )

        self.assertEqual(publish_pending_outbox(self.conn, self.redis), 1)
        length_after_first = self.redis.xlen(result.stream_name)

        self.assertFalse(
            publish_outbox_event(
                self.conn,
                self.redis,
                event_id=result.event_id,
                stream_name=result.stream_name,
                payload=result.payload,
            )
        )
        self.assertEqual(publish_pending_outbox(self.conn, self.redis), 0)
        self.assertEqual(self.redis.xlen(result.stream_name), length_after_first)

    def test_restart_redis_republish_restores_stream_entry(self) -> None:
        task_id = "tsk_n6_restart"
        job_id = "job_n6_restart"
        stream_name = resolve_stream_name("browser", 2)
        _clear_stream(self.redis, stream_name)
        _insert_job(self.conn, job_id)

        result = enqueue_ready_task(
            self.conn,
            task_id=task_id,
            job_id=job_id,
            lane="browser",
            idempotency_key="sha256:" + ("e1" * 32),
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

        republished = republish_missing_stream_messages(self.conn, self.redis)
        self.assertEqual(republished, 1)
        self.assertEqual(self.redis.xlen(result.stream_name), 1)
        self.assertTrue(
            stream_contains_event(self.redis, result.stream_name, result.event_id)
        )


class IntegrationCheckDispatcher(unittest.TestCase):
    """Integration check hook for gate-verifier (guard 4 outbox atomicity + restart-Redis)."""

    @unittest.skipUnless(_infra_available(), "Postgres+Redis unavailable (need .env)")
    def test_outbox_atomicity_guard4(self) -> None:
        suite = unittest.TestSuite()
        suite.addTest(
            DispatcherOutboxTests("test_enqueue_inserts_task_and_outbox_atomically")
        )
        suite.addTest(DispatcherOutboxTests("test_failed_enqueue_rolls_back_outbox"))
        suite.addTest(
            DispatcherOutboxTests("test_unpublished_outbox_published_after_crash_before_xadd")
        )
        suite.addTest(DispatcherOutboxTests("test_publish_is_idempotent"))
        result = unittest.TextTestRunner(verbosity=0).run(suite)
        self.assertTrue(result.wasSuccessful())

    @unittest.skipUnless(_infra_available(), "Postgres+Redis unavailable (need .env)")
    def test_restart_redis_republish(self) -> None:
        suite = unittest.TestSuite()
        suite.addTest(
            DispatcherOutboxTests("test_restart_redis_republish_restores_stream_entry")
        )
        result = unittest.TextTestRunner(verbosity=0).run(suite)
        self.assertTrue(result.wasSuccessful())


if __name__ == "__main__":
    unittest.main()
