"""N7 scheduler — admission, budgets, fairness integration tests."""

from __future__ import annotations

import json
import os
import unittest
from pathlib import Path

import psycopg

from control.scheduler import (
    BackpressureGate,
    admit_task,
    check_lane_budget,
    check_lane_concurrency,
    fetch_admission_candidates,
    run_admission_tick,
    select_fair_batch,
)
from control.scheduler.backpressure import BackpressureState
from control.scheduler.fairness import AdmissionCandidate
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

TIGHT_BUDGET = {
    **BUDGET,
    "max_fetched_urls": 2,
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


def _sample_job(job_id: str, budget: dict | None = None) -> tuple:
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
        json.dumps(budget or BUDGET),
        json.dumps(provenance),
    )


def _task_provenance() -> dict:
    return {
        "schema_version": "task.v1",
        "config_hash": CONFIG_HASH,
        "created_at": CREATED_AT,
    }


def _insert_job(conn: psycopg.Connection, job_id: str, budget: dict | None = None) -> None:
    conn.execute(
        """
        INSERT INTO research_jobs (
            job_id, job_kind, status, config_hash, budget, provenance
        )
        VALUES (%s, %s, %s, %s, %s::jsonb, %s::jsonb)
        ON CONFLICT (job_id) DO NOTHING
        """,
        _sample_job(job_id, budget),
    )


def _insert_pending_task(
    conn: psycopg.Connection,
    *,
    task_id: str,
    job_id: str,
    lane: str = "http",
    priority: int = 2,
    idempotency_key: str,
) -> None:
    conn.execute(
        """
        INSERT INTO tasks (
            task_id,
            job_id,
            lane,
            priority,
            state,
            idempotency_key,
            payload_ref,
            provenance,
            created_at
        )
        VALUES (%s, %s, %s, %s, 'PENDING', %s, %s, %s::jsonb, %s::timestamptz)
        """,
        (
            task_id,
            job_id,
            lane,
            priority,
            idempotency_key,
            f"postgres://tasks/{task_id}",
            json.dumps(_task_provenance()),
            CREATED_AT,
        ),
    )


def _insert_succeeded_task(
    conn: psycopg.Connection,
    *,
    task_id: str,
    job_id: str,
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
            state,
            idempotency_key,
            payload_ref,
            provenance,
            created_at,
            completed_at
        )
        VALUES (
            %s, %s, %s, 2, 'SUCCEEDED', %s, %s, %s::jsonb,
            %s::timestamptz, %s::timestamptz
        )
        """,
        (
            task_id,
            job_id,
            lane,
            idempotency_key,
            f"postgres://tasks/{task_id}",
            json.dumps(_task_provenance()),
            CREATED_AT,
            CREATED_AT,
        ),
    )


def _add_dependency(
    conn: psycopg.Connection,
    *,
    task_id: str,
    depends_on_task_id: str,
) -> None:
    conn.execute(
        """
        INSERT INTO task_dependencies (task_id, depends_on_task_id)
        VALUES (%s, %s)
        ON CONFLICT DO NOTHING
        """,
        (task_id, depends_on_task_id),
    )


def _insert_in_flight_task(
    conn: psycopg.Connection,
    *,
    task_id: str,
    job_id: str,
    lane: str = "browser",
    idempotency_key: str,
    state: str = "READY",
) -> None:
    conn.execute(
        """
        INSERT INTO tasks (
            task_id,
            job_id,
            lane,
            priority,
            state,
            idempotency_key,
            payload_ref,
            provenance,
            created_at
        )
        VALUES (%s, %s, %s, 2, %s, %s, %s, %s::jsonb, %s::timestamptz)
        """,
        (
            task_id,
            job_id,
            lane,
            state,
            idempotency_key,
            f"postgres://tasks/{task_id}",
            json.dumps(_task_provenance()),
            CREATED_AT,
        ),
    )


def _fetch_task_state(conn: psycopg.Connection, task_id: str) -> str:
    with conn.cursor() as cur:
        cur.execute("SELECT state FROM tasks WHERE task_id = %s", (task_id,))
        row = cur.fetchone()
    assert row is not None
    return row[0]


def _count_outbox(conn: psycopg.Connection, task_id: str) -> int:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) FROM outbox_events WHERE task_id = %s",
            (task_id,),
        )
        row = cur.fetchone()
    assert row is not None
    return int(row[0])


class FairnessUnitTests(unittest.TestCase):
    def test_weighted_round_robin_prefers_high_priority_jobs(self) -> None:
        candidates = [
            AdmissionCandidate(
                task_id=f"tsk_bulk_{idx}",
                job_id="job_bulk",
                lane="http",
                priority=3,
                created_at=idx,
            )
            for idx in range(8)
        ] + [
            AdmissionCandidate(
                task_id=f"tsk_high_{idx}",
                job_id="job_high",
                lane="http",
                priority=0,
                created_at=idx,
            )
            for idx in range(8)
        ]

        selected = select_fair_batch(candidates, batch_limit=9)
        high_count = sum(1 for item in selected if item.job_id == "job_high")
        bulk_count = sum(1 for item in selected if item.job_id == "job_bulk")

        self.assertEqual(len(selected), 9)
        self.assertGreater(high_count, bulk_count)
        self.assertEqual(high_count + bulk_count, 9)


@unittest.skipUnless(_postgres_available(), "Postgres unavailable (need .env)")
class SchedulerAdmissionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.conn = connect()
        apply_migrations(cls.conn)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.conn.close()

    def setUp(self) -> None:
        self.conn.execute("SAVEPOINT n7_test_case")

    def tearDown(self) -> None:
        self.conn.execute("ROLLBACK TO SAVEPOINT n7_test_case")

    def test_dependency_blocks_until_upstream_succeeds(self) -> None:
        job_id = "job_n7_dep"
        _insert_job(self.conn, job_id)
        _insert_succeeded_task(
            self.conn,
            task_id="tsk_dep_parent",
            job_id=job_id,
            idempotency_key="sha256:" + ("p1" * 32),
        )
        _insert_pending_task(
            self.conn,
            task_id="tsk_dep_child",
            job_id=job_id,
            idempotency_key="sha256:" + ("c1" * 32),
        )
        _add_dependency(
            self.conn,
            task_id="tsk_dep_child",
            depends_on_task_id="tsk_dep_parent",
        )

        result = admit_task(self.conn, task_id="tsk_dep_child")
        self.assertTrue(result.admitted)
        self.assertEqual(_fetch_task_state(self.conn, "tsk_dep_child"), "READY")
        self.assertEqual(_count_outbox(self.conn, "tsk_dep_child"), 1)

    def test_dependency_unsatisfied_stays_pending(self) -> None:
        job_id = "job_n7_blocked"
        _insert_job(self.conn, job_id)
        _insert_pending_task(
            self.conn,
            task_id="tsk_dep_parent_pending",
            job_id=job_id,
            idempotency_key="sha256:" + ("p2" * 32),
        )
        _insert_pending_task(
            self.conn,
            task_id="tsk_dep_child_blocked",
            job_id=job_id,
            idempotency_key="sha256:" + ("c2" * 32),
        )
        _add_dependency(
            self.conn,
            task_id="tsk_dep_child_blocked",
            depends_on_task_id="tsk_dep_parent_pending",
        )

        candidates = fetch_admission_candidates(self.conn, lane="http")
        blocked_ids = {item.task_id for item in candidates}
        self.assertNotIn("tsk_dep_child_blocked", blocked_ids)

        result = admit_task(self.conn, task_id="tsk_dep_child_blocked")
        self.assertFalse(result.admitted)
        self.assertEqual(result.reason, "dependencies_unsatisfied")
        self.assertEqual(_fetch_task_state(self.conn, "tsk_dep_child_blocked"), "PENDING")

    def test_admission_denied_when_budget_exhausted(self) -> None:
        job_id = "job_n7_budget"
        _insert_job(self.conn, job_id, TIGHT_BUDGET)
        _insert_succeeded_task(
            self.conn,
            task_id="tsk_budget_1",
            job_id=job_id,
            idempotency_key="sha256:" + ("b1" * 32),
        )
        _insert_succeeded_task(
            self.conn,
            task_id="tsk_budget_2",
            job_id=job_id,
            idempotency_key="sha256:" + ("b2" * 32),
        )
        _insert_pending_task(
            self.conn,
            task_id="tsk_budget_3",
            job_id=job_id,
            idempotency_key="sha256:" + ("b3" * 32),
        )

        budget = check_lane_budget(self.conn, job_id=job_id, lane="http")
        self.assertFalse(budget.allowed)
        self.assertEqual(budget.reason, "budget_exhausted")
        self.assertEqual(budget.spent, 2)
        self.assertEqual(budget.limit, 2)

        result = admit_task(self.conn, task_id="tsk_budget_3")
        self.assertFalse(result.admitted)
        self.assertEqual(result.reason, "budget_exhausted")
        self.assertEqual(_fetch_task_state(self.conn, "tsk_budget_3"), "PENDING")
        self.assertEqual(_count_outbox(self.conn, "tsk_budget_3"), 0)

    def test_fairness_tick_prefers_high_priority_job(self) -> None:
        _insert_job(self.conn, "job_n7_high", BUDGET)
        _insert_job(self.conn, "job_n7_bulk", BUDGET)
        for idx in range(6):
            _insert_pending_task(
                self.conn,
                task_id=f"tsk_high_{idx}",
                job_id="job_n7_high",
                lane="extract",
                priority=0,
                idempotency_key=f"sha256:{idx:064x}",
            )
            _insert_pending_task(
                self.conn,
                task_id=f"tsk_bulk_{idx}",
                job_id="job_n7_bulk",
                lane="extract",
                priority=3,
                idempotency_key=f"sha256:{idx+100:064x}",
            )

        tick = run_admission_tick(self.conn, batch_limit=9, lane="extract")
        admitted_ids = {item.task_id for item in tick.admitted}
        high_admitted = sum(1 for task_id in admitted_ids if task_id.startswith("tsk_high_"))
        bulk_admitted = sum(1 for task_id in admitted_ids if task_id.startswith("tsk_bulk_"))

        self.assertEqual(len(tick.admitted), 9)
        self.assertGreater(high_admitted, bulk_admitted)
        self.assertEqual(high_admitted + bulk_admitted, 9)

    def test_admission_denied_when_max_in_flight_exceeded(self) -> None:
        job_id = "job_n7_concurrency"
        _insert_job(self.conn, job_id)
        _insert_in_flight_task(
            self.conn,
            task_id="tsk_browser_if_1",
            job_id=job_id,
            lane="browser",
            idempotency_key="sha256:" + ("f1" * 32),
        )
        _insert_in_flight_task(
            self.conn,
            task_id="tsk_browser_if_2",
            job_id=job_id,
            lane="browser",
            idempotency_key="sha256:" + ("f2" * 32),
        )
        _insert_pending_task(
            self.conn,
            task_id="tsk_browser_pending",
            job_id=job_id,
            lane="browser",
            idempotency_key="sha256:" + ("f3" * 32),
        )

        concurrency = check_lane_concurrency(self.conn, lane="browser")
        self.assertFalse(concurrency.allowed)
        self.assertEqual(concurrency.reason, "max_in_flight_exceeded")
        self.assertEqual(concurrency.limit, 2)
        self.assertEqual(concurrency.in_flight, 2)

        result = admit_task(self.conn, task_id="tsk_browser_pending")
        self.assertFalse(result.admitted)
        self.assertEqual(result.reason, "max_in_flight_exceeded")
        self.assertEqual(_fetch_task_state(self.conn, "tsk_browser_pending"), "PENDING")

    def test_fetch_denied_when_backpressure_gate_paused(self) -> None:
        job_id = "job_n7_bp_pause"
        _insert_job(self.conn, job_id)
        _insert_pending_task(
            self.conn,
            task_id="tsk_bp_http",
            job_id=job_id,
            lane="http",
            idempotency_key="sha256:" + ("h1" * 32),
        )

        gate = BackpressureGate()
        backlogs = {
            "extract_backlog": 500,
            "enrich_backlog": 50,
            "index_backlog": 50,
        }
        gate.update(
            backlogs,
            high_limits={
                "extract_backlog": 500,
                "enrich_backlog": 300,
                "index_backlog": 1000,
            },
            low_limits={
                "extract_backlog": 200,
                "enrich_backlog": 100,
                "index_backlog": 400,
            },
        )
        state = BackpressureState(
            paused=True,
            reason="downstream_backpressure_paused",
            backlogs=backlogs,
            watermarks={},
        )

        result = admit_task(
            self.conn,
            task_id="tsk_bp_http",
            backpressure=state,
            backpressure_gate=gate,
        )
        self.assertFalse(result.admitted)
        self.assertEqual(result.reason, "backpressure_paused")
        self.assertEqual(_fetch_task_state(self.conn, "tsk_bp_http"), "PENDING")

    def test_non_fetch_lane_admits_when_backpressure_gate_paused(self) -> None:
        job_id = "job_n7_bp_extract"
        _insert_job(self.conn, job_id)
        _insert_pending_task(
            self.conn,
            task_id="tsk_bp_extract",
            job_id=job_id,
            lane="extract",
            idempotency_key="sha256:" + ("x1" * 32),
        )

        gate = BackpressureGate()
        gate.update(
            {
                "extract_backlog": 500,
                "enrich_backlog": 50,
                "index_backlog": 50,
            },
            high_limits={
                "extract_backlog": 500,
                "enrich_backlog": 300,
                "index_backlog": 1000,
            },
            low_limits={
                "extract_backlog": 200,
                "enrich_backlog": 100,
                "index_backlog": 400,
            },
        )
        state = BackpressureState(
            paused=True,
            reason="downstream_backpressure_paused",
            backlogs={},
            watermarks={},
        )

        result = admit_task(
            self.conn,
            task_id="tsk_bp_extract",
            backpressure=state,
            backpressure_gate=gate,
        )
        self.assertTrue(result.admitted)
        self.assertEqual(_fetch_task_state(self.conn, "tsk_bp_extract"), "READY")


class BackpressureHysteresisTests(unittest.TestCase):
    _HIGH = {
        "extract_backlog": 500,
        "enrich_backlog": 300,
        "index_backlog": 1000,
    }
    _LOW = {
        "extract_backlog": 200,
        "enrich_backlog": 100,
        "index_backlog": 400,
    }

    def test_between_watermarks_admits_when_not_paused(self) -> None:
        gate = BackpressureGate()
        backlogs = {
            "extract_backlog": 250,
            "enrich_backlog": 150,
            "index_backlog": 500,
        }
        gate.update(backlogs, high_limits=self._HIGH, low_limits=self._LOW)
        self.assertFalse(gate.fetch_paused)

    def test_pause_at_high_watermark(self) -> None:
        gate = BackpressureGate()
        gate.update(
            {
                "extract_backlog": 500,
                "enrich_backlog": 150,
                "index_backlog": 500,
            },
            high_limits=self._HIGH,
            low_limits=self._LOW,
        )
        self.assertTrue(gate.fetch_paused)

    def test_stays_paused_between_watermarks_until_all_below_low(self) -> None:
        gate = BackpressureGate()
        gate.update(
            {
                "extract_backlog": 500,
                "enrich_backlog": 150,
                "index_backlog": 500,
            },
            high_limits=self._HIGH,
            low_limits=self._LOW,
        )
        self.assertTrue(gate.fetch_paused)

        gate.update(
            {
                "extract_backlog": 250,
                "enrich_backlog": 150,
                "index_backlog": 500,
            },
            high_limits=self._HIGH,
            low_limits=self._LOW,
        )
        self.assertTrue(gate.fetch_paused)

        gate.update(
            {
                "extract_backlog": 200,
                "enrich_backlog": 100,
                "index_backlog": 400,
            },
            high_limits=self._HIGH,
            low_limits=self._LOW,
        )
        self.assertFalse(gate.fetch_paused)


class IntegrationCheckScheduler(unittest.TestCase):
    """Integration check hook for gate-verifier (admission + budgets + fairness)."""

    @unittest.skipUnless(_postgres_available(), "Postgres unavailable (need .env)")
    def test_admission_budgets_fairness(self) -> None:
        suite = unittest.TestSuite()
        suite.addTest(FairnessUnitTests("test_weighted_round_robin_prefers_high_priority_jobs"))
        suite.addTest(BackpressureHysteresisTests("test_between_watermarks_admits_when_not_paused"))
        suite.addTest(
            BackpressureHysteresisTests(
                "test_stays_paused_between_watermarks_until_all_below_low"
            )
        )
        suite.addTest(
            SchedulerAdmissionTests("test_admission_denied_when_budget_exhausted")
        )
        suite.addTest(
            SchedulerAdmissionTests("test_admission_denied_when_max_in_flight_exceeded")
        )
        suite.addTest(
            SchedulerAdmissionTests("test_fetch_denied_when_backpressure_gate_paused")
        )
        suite.addTest(
            SchedulerAdmissionTests(
                "test_non_fetch_lane_admits_when_backpressure_gate_paused"
            )
        )
        suite.addTest(
            SchedulerAdmissionTests("test_fairness_tick_prefers_high_priority_job")
        )
        suite.addTest(
            SchedulerAdmissionTests("test_dependency_blocks_until_upstream_succeeds")
        )
        result = unittest.TextTestRunner(verbosity=0).run(suite)
        self.assertTrue(result.wasSuccessful())


if __name__ == "__main__":
    unittest.main()
