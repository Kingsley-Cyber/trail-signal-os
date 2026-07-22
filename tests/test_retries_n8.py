"""N8 retries+circuits — classifier, backoff, circuit breaker, dead-letter tests."""

from __future__ import annotations

import json
import os
import random
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import psycopg

from control.retries import (
    CircuitConfig,
    CircuitRegistry,
    CircuitState,
    classify_failure,
    compute_backoff_seconds,
    handle_task_failure,
    record_route_success,
    send_to_dead_letter,
)
from control.retries.circuit_breaker import RouteCircuit
from guards.exceptions import GuardViolation
from guards.runtime_guards import guard10_route_403_to_blocked
from db.repositories.connection import connect
from db.repositories.migrate import apply_migrations

REPO_ROOT = Path(__file__).resolve().parents[1]
ENV_FILE = REPO_ROOT / ".env"
CONFIG_HASH = "sha256:" + ("a" * 64)
CREATED_AT = "2026-07-21T12:00:00Z"
BASE_TIME = datetime(2026, 7, 21, 12, 0, 0, tzinfo=timezone.utc)

BUDGET = {
    "max_queries": 10,
    "max_fetched_urls": 100,
    "per_domain_urls": 50,
    "browser_pages": 5,
    "media_items": 10,
    "max_bytes": 1048576,
    "deadline_minutes": 30,
    "max_attempts": 4,
    "llm_budget": {"max_calls": 10, "max_tokens": 10000, "max_usd": 0},
    "schema_version": "budget.v1",
}

FAST_CIRCUIT_CONFIG = CircuitConfig(
    consecutive_threshold=3,
    failure_rate_threshold=0.5,
    window_size=4,
    default_cooldown_seconds=(2, 4, 8),
)


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


def _insert_running_task(
    conn: psycopg.Connection,
    *,
    task_id: str,
    job_id: str,
    attempt: int = 1,
    lane: str = "http",
    idempotency_key: str,
) -> None:
    conn.execute(
        """
        INSERT INTO tasks (
            task_id,
            job_id,
            lane,
            priority,
            attempt,
            state,
            idempotency_key,
            payload_ref,
            provenance,
            created_at
        )
        VALUES (%s, %s, %s, 2, %s, 'RUNNING', %s, %s, %s::jsonb, %s::timestamptz)
        """,
        (
            task_id,
            job_id,
            lane,
            attempt,
            idempotency_key,
            f"postgres://tasks/{task_id}",
            json.dumps(_task_provenance()),
            CREATED_AT,
        ),
    )


def _fetch_task(conn: psycopg.Connection, task_id: str) -> tuple:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT state, attempt, retry_at FROM tasks WHERE task_id = %s",
            (task_id,),
        )
        row = cur.fetchone()
    assert row is not None
    return row[0], int(row[1]), row[2]


class ClassifierUnitTests(unittest.TestCase):
    def test_429_is_retryable(self) -> None:
        result = classify_failure(status_code=429)
        self.assertEqual(result.failure_class, "HTTP_429")
        self.assertTrue(result.retryable)
        self.assertEqual(result.action.value, "RETRY_WAIT")

    def test_403_routes_blocked_not_escalation(self) -> None:
        result = classify_failure(status_code=403, escalation=None)
        self.assertEqual(result.action.value, "BLOCKED")
        self.assertFalse(result.retryable)
        self.assertEqual(
            guard10_route_403_to_blocked(status_code=403, escalation=None),
            "BLOCKED",
        )

    def test_403_with_escalation_raises_guard_violation(self) -> None:
        with self.assertRaises(GuardViolation):
            classify_failure(status_code=403, escalation="browser")

    def test_404_is_non_retryable_failed(self) -> None:
        result = classify_failure(status_code=404)
        self.assertEqual(result.action.value, "FAILED")


class BackoffUnitTests(unittest.TestCase):
    def test_429_respects_retry_after(self) -> None:
        delay = compute_backoff_seconds(
            attempt=2,
            lane="http",
            failure_class="HTTP_429",
            retry_after_seconds=120.0,
        )
        self.assertEqual(delay, 120.0)

    def test_exponential_backoff_respects_lane_ceiling(self) -> None:
        rng = random.Random(0)
        delay = compute_backoff_seconds(
            attempt=5,
            lane="http",
            failure_class="HTTP_503",
            rng=rng,
        )
        self.assertLessEqual(delay, 32.0 * 1.25)
        self.assertGreaterEqual(delay, 32.0 * 0.75)


class CircuitBreakerUnitTests(unittest.TestCase):
    def test_opens_on_consecutive_failures(self) -> None:
        circuit = RouteCircuit(
            route_key="youtube:ytdlp",
            config=FAST_CIRCUIT_CONFIG,
        )
        transition = None
        for _ in range(FAST_CIRCUIT_CONFIG.consecutive_threshold):
            transition = circuit.record_failure(
                now=BASE_TIME,
                failure_class="HTTP_429",
            )
        self.assertIsNotNone(transition)
        assert transition is not None
        self.assertEqual(transition.event_type, "circuit_open")
        self.assertEqual(circuit.state, CircuitState.OPEN)
        self.assertIsNotNone(circuit.cooldown_until)

    def test_opens_on_failure_rate(self) -> None:
        circuit = RouteCircuit(
            route_key="example.com:http",
            config=FAST_CIRCUIT_CONFIG,
        )
        for _ in range(3):
            circuit.record_success(now=BASE_TIME)
        for _ in range(3):
            circuit.record_failure(now=BASE_TIME, failure_class="HTTP_503")
        self.assertEqual(circuit.state, CircuitState.OPEN)

    def test_closes_after_cooldown_on_probe_success(self) -> None:
        registry = CircuitRegistry(config=FAST_CIRCUIT_CONFIG)
        route = "example.com:http"
        for _ in range(FAST_CIRCUIT_CONFIG.consecutive_threshold):
            registry.record_failure(route, now=BASE_TIME, failure_class="HTTP_429")
        circuit = registry.get(route)
        self.assertEqual(circuit.state, CircuitState.OPEN)
        cooldown_end = circuit.cooldown_until
        assert cooldown_end is not None

        self.assertFalse(registry.allow_request(route, now=BASE_TIME))
        self.assertTrue(registry.allow_request(route, now=cooldown_end + timedelta(seconds=1)))

        event = record_route_success(
            registry,
            domain="example.com",
            route=route,
            now=cooldown_end + timedelta(seconds=1),
            config_hash=CONFIG_HASH,
        )
        self.assertIsNotNone(event)
        assert event is not None
        self.assertEqual(event["event_type"], "circuit_close")
        self.assertEqual(registry.get(route).state, CircuitState.CLOSED)


@unittest.skipUnless(_postgres_available(), "Postgres unavailable (need .env)")
class RetryHandlingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.conn = connect()
        apply_migrations(cls.conn)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.conn.close()

    def setUp(self) -> None:
        self.conn.execute("SAVEPOINT n8_test_case")
        self.circuits = CircuitRegistry(config=FAST_CIRCUIT_CONFIG)

    def tearDown(self) -> None:
        self.conn.execute("ROLLBACK TO SAVEPOINT n8_test_case")

    def test_429_sets_retry_wait_with_cooldown(self) -> None:
        job_id = "job_n8_429"
        task_id = "tsk_n8_429"
        _insert_job(self.conn, job_id)
        _insert_running_task(
            self.conn,
            task_id=task_id,
            job_id=job_id,
            idempotency_key="sha256:" + ("r1" * 32),
        )

        result = handle_task_failure(
            self.conn,
            task_id=task_id,
            job_id=job_id,
            domain="youtube.com",
            route="youtube:ytdlp",
            lane="http",
            attempt=1,
            max_attempts=4,
            circuits=self.circuits,
            config_hash=CONFIG_HASH,
            status_code=429,
            retry_after_seconds=90.0,
            now=BASE_TIME,
        )

        state, attempt, retry_at = _fetch_task(self.conn, task_id)
        self.assertEqual(result.action, "RETRY_WAIT")
        self.assertEqual(result.failure_class, "HTTP_429")
        self.assertEqual(state, "RETRY_WAIT")
        self.assertEqual(attempt, 2)
        self.assertIsNotNone(retry_at)
        assert retry_at is not None
        expected = BASE_TIME + timedelta(seconds=90.0)
        self.assertAlmostEqual(retry_at.timestamp(), expected.timestamp(), delta=1.0)

    def test_403_sets_blocked_not_browser_escalation(self) -> None:
        job_id = "job_n8_403"
        task_id = "tsk_n8_403"
        _insert_job(self.conn, job_id)
        _insert_running_task(
            self.conn,
            task_id=task_id,
            job_id=job_id,
            idempotency_key="sha256:" + ("b1" * 32),
        )

        result = handle_task_failure(
            self.conn,
            task_id=task_id,
            job_id=job_id,
            domain="amazon.com",
            route="amazon.com:http",
            lane="http",
            attempt=1,
            max_attempts=4,
            circuits=self.circuits,
            config_hash=CONFIG_HASH,
            status_code=403,
            now=BASE_TIME,
        )

        state, _, _ = _fetch_task(self.conn, task_id)
        self.assertEqual(result.action, "BLOCKED")
        self.assertEqual(state, "BLOCKED")

    def test_circuit_open_forces_retry_wait_until_cooldown(self) -> None:
        job_id = "job_n8_circuit"
        task_id = "tsk_n8_circuit"
        route = "youtube:ytdlp"
        _insert_job(self.conn, job_id)
        _insert_running_task(
            self.conn,
            task_id=task_id,
            job_id=job_id,
            lane="media",
            idempotency_key="sha256:" + ("c1" * 32),
        )

        for _ in range(FAST_CIRCUIT_CONFIG.consecutive_threshold - 1):
            self.circuits.record_failure(route, now=BASE_TIME, failure_class="HTTP_429")

        result = handle_task_failure(
            self.conn,
            task_id=task_id,
            job_id=job_id,
            domain="youtube.com",
            route=route,
            lane="media",
            attempt=1,
            max_attempts=4,
            circuits=self.circuits,
            config_hash=CONFIG_HASH,
            status_code=429,
            retry_after_seconds=5.0,
            now=BASE_TIME,
        )

        state, attempt, retry_at = _fetch_task(self.conn, task_id)
        circuit = self.circuits.get(route)
        self.assertEqual(circuit.state, CircuitState.OPEN)
        self.assertEqual(state, "RETRY_WAIT")
        self.assertEqual(attempt, 2)
        self.assertIsNotNone(result.degradation_event)
        assert result.degradation_event is not None
        self.assertEqual(result.degradation_event["event_type"], "circuit_open")
        self.assertIsNotNone(retry_at)
        assert retry_at is not None
        assert circuit.cooldown_until is not None
        self.assertAlmostEqual(
            retry_at.timestamp(),
            circuit.cooldown_until.timestamp(),
            delta=1.0,
        )

    def test_dead_letter_when_attempts_exhausted(self) -> None:
        job_id = "job_n8_dl"
        task_id = "tsk_n8_dl"
        _insert_job(self.conn, job_id)
        _insert_running_task(
            self.conn,
            task_id=task_id,
            job_id=job_id,
            attempt=4,
            idempotency_key="sha256:" + ("d1" * 32),
        )

        result = handle_task_failure(
            self.conn,
            task_id=task_id,
            job_id=job_id,
            domain="example.com",
            route="example.com:http",
            lane="http",
            attempt=4,
            max_attempts=4,
            circuits=self.circuits,
            config_hash=CONFIG_HASH,
            status_code=503,
            now=BASE_TIME,
        )

        state, _, _ = _fetch_task(self.conn, task_id)
        self.assertEqual(result.action, "DEAD_LETTER")
        self.assertEqual(state, "DEAD_LETTER")

        dl = send_to_dead_letter(
            self.conn,
            task_id=task_id,
            failure_class="HTTP_503",
            reason="manual_review",
        )
        self.assertEqual(dl.previous_state, "DEAD_LETTER")


class IntegrationCheckRetries(unittest.TestCase):
    """Integration check hook for gate-verifier (429 cooldown; circuit open/close)."""

    @unittest.skipUnless(_postgres_available(), "Postgres unavailable (need .env)")
    def test_429_cooldown_and_circuit_open_close(self) -> None:
        suite = unittest.TestSuite()
        suite.addTest(ClassifierUnitTests("test_429_is_retryable"))
        suite.addTest(ClassifierUnitTests("test_403_routes_blocked_not_escalation"))
        suite.addTest(CircuitBreakerUnitTests("test_opens_on_consecutive_failures"))
        suite.addTest(CircuitBreakerUnitTests("test_closes_after_cooldown_on_probe_success"))
        suite.addTest(RetryHandlingTests("test_429_sets_retry_wait_with_cooldown"))
        suite.addTest(RetryHandlingTests("test_circuit_open_forces_retry_wait_until_cooldown"))
        suite.addTest(RetryHandlingTests("test_403_sets_blocked_not_browser_escalation"))
        result = unittest.TextTestRunner(verbosity=0).run(suite)
        self.assertTrue(result.wasSuccessful())


if __name__ == "__main__":
    unittest.main()
