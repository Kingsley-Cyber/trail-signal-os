"""N22 governor — phase gating + backpressure under memory pressure."""

from __future__ import annotations

import json
import os
import unittest
from dataclasses import dataclass
from pathlib import Path

import psycopg

from control.resources import (
    BackpressureGate,
    GovernorState,
    MemoryMetrics,
    PressureLevel,
    admit_task_with_governor,
    classify_pressure,
    evaluate_governor,
    lane_admission_allowed,
    lane_enabled_for_phase,
    load_phase_profile,
    pressure_actions,
)
from control.scheduler.admit import admit_task
from control.scheduler.backpressure import BackpressureState
from db.repositories.connection import connect
from db.repositories.migrate import apply_migrations

REPO_ROOT = Path(__file__).resolve().parents[1]
ENV_FILE = REPO_ROOT / ".env"
CONFIG_HASH = "sha256:" + ("a" * 64)
CREATED_AT = "2026-07-21T12:00:00Z"
VM_TOTAL_BYTES = int(25.4 * (1024**3))

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


@dataclass(frozen=True)
class StaticMetricsProvider:
    metrics: MemoryMetrics

    def read_memory_metrics(self) -> MemoryMetrics:
        return self.metrics


def _metrics_at_pct(pct: float) -> MemoryMetrics:
    used = int(VM_TOTAL_BYTES * (pct / 100.0))
    return MemoryMetrics(
        vm_total_bytes=VM_TOTAL_BYTES,
        vm_used_bytes=used,
        vm_used_pct=pct,
        macos_pressure="normal",
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


def _insert_pending_task(
    conn: psycopg.Connection,
    *,
    task_id: str,
    job_id: str,
    lane: str,
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
        VALUES (%s, %s, %s, 2, 'PENDING', %s, %s, %s::jsonb, %s::timestamptz)
        """,
        (
            task_id,
            job_id,
            lane,
            idempotency_key,
            f"postgres://tasks/{task_id}",
            json.dumps(_task_provenance()),
            CREATED_AT,
        ),
    )


def _insert_backlog_tasks(
    conn: psycopg.Connection,
    *,
    lane: str,
    count: int,
    job_id: str,
    prefix: str,
) -> None:
    for idx in range(count):
        _insert_pending_task(
            conn,
            task_id=f"{prefix}_{idx}",
            job_id=job_id,
            lane=lane,
            idempotency_key=f"sha256:{idx:064x}",
        )
        conn.execute(
            "UPDATE tasks SET state = 'READY' WHERE task_id = %s",
            (f"{prefix}_{idx}",),
        )


def _fetch_task_state(conn: psycopg.Connection, task_id: str) -> str:
    with conn.cursor() as cur:
        cur.execute("SELECT state FROM tasks WHERE task_id = %s", (task_id,))
        row = cur.fetchone()
    assert row is not None
    return row[0]


class MemoryPressureUnitTests(unittest.TestCase):
    def test_classify_green_orange_red_boundaries(self) -> None:
        self.assertEqual(
            classify_pressure(_metrics_at_pct(69.9)),
            PressureLevel.GREEN,
        )
        self.assertEqual(
            classify_pressure(_metrics_at_pct(70.0)),
            PressureLevel.ORANGE,
        )
        self.assertEqual(
            classify_pressure(_metrics_at_pct(84.9)),
            PressureLevel.ORANGE,
        )
        self.assertEqual(
            classify_pressure(_metrics_at_pct(85.0)),
            PressureLevel.RED,
        )

    def test_orange_throttles_browser_and_llm(self) -> None:
        profile = load_phase_profile("ACQUIRE")
        actions = pressure_actions(PressureLevel.ORANGE, profile)
        self.assertFalse(actions.allow_browser)
        self.assertFalse(actions.allow_llm_admission)
        self.assertTrue(actions.allow_fetch_lanes)
        self.assertEqual(actions.parser_processes, 1)

    def test_red_pauses_fetch_and_parsers(self) -> None:
        profile = load_phase_profile("ENRICH")
        actions = pressure_actions(PressureLevel.RED, profile)
        self.assertFalse(actions.allow_fetch_lanes)
        self.assertEqual(actions.parser_processes, 0)


class PhaseGatingUnitTests(unittest.TestCase):
    def test_acquire_enables_browser_disables_enrich(self) -> None:
        profile = load_phase_profile("ACQUIRE")
        self.assertTrue(lane_enabled_for_phase("browser", profile))
        self.assertTrue(lane_enabled_for_phase("http", profile))
        self.assertFalse(lane_enabled_for_phase("enrich", profile))
        self.assertFalse(lane_enabled_for_phase("index", profile))

    def test_enrich_enables_llm_lane_disables_browser(self) -> None:
        profile = load_phase_profile("ENRICH")
        self.assertFalse(lane_enabled_for_phase("browser", profile))
        self.assertTrue(lane_enabled_for_phase("enrich", profile))
        self.assertFalse(lane_enabled_for_phase("index", profile))

    def test_index_enables_index_lane_only(self) -> None:
        profile = load_phase_profile("INDEX")
        self.assertTrue(lane_enabled_for_phase("index", profile))
        self.assertFalse(lane_enabled_for_phase("enrich", profile))
        self.assertFalse(lane_enabled_for_phase("browser", profile))


class GovernorDecisionUnitTests(unittest.TestCase):
    def test_orange_blocks_browser_even_in_acquire(self) -> None:
        profile = load_phase_profile("ACQUIRE")
        actions = pressure_actions(PressureLevel.ORANGE, profile)
        governor = GovernorState(
            phase="ACQUIRE",
            profile=profile,
            memory=_metrics_at_pct(75.0),
            pressure=PressureLevel.ORANGE,
            actions=actions,
            backpressure=BackpressureState(
                paused=False,
                reason="downstream_below_high_watermark",
                backlogs={},
                watermarks={},
            ),
            fetch_paused=False,
        )
        allowed, reason = lane_admission_allowed(lane="browser", governor=governor)
        self.assertFalse(allowed)
        self.assertEqual(reason, "pressure_orange_no_browser")

    def test_enrich_phase_blocks_index_lane(self) -> None:
        profile = load_phase_profile("ENRICH")
        actions = pressure_actions(PressureLevel.GREEN, profile)
        governor = GovernorState(
            phase="ENRICH",
            profile=profile,
            memory=_metrics_at_pct(40.0),
            pressure=PressureLevel.GREEN,
            actions=actions,
            backpressure=BackpressureState(
                paused=False,
                reason="downstream_below_high_watermark",
                backlogs={},
                watermarks={},
            ),
            fetch_paused=False,
        )
        allowed, reason = lane_admission_allowed(lane="index", governor=governor)
        self.assertFalse(allowed)
        self.assertEqual(reason, "phase_enrich_lane_disabled")


@unittest.skipUnless(_postgres_available(), "Postgres unavailable (need .env)")
class GovernorAdmissionIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.conn = connect()
        apply_migrations(cls.conn)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.conn.close()

    def setUp(self) -> None:
        self.conn.execute("SAVEPOINT n22_test_case")

    def tearDown(self) -> None:
        self.conn.execute("ROLLBACK TO SAVEPOINT n22_test_case")

    def test_acquire_green_admits_http(self) -> None:
        job_id = "job_n22_acquire_http"
        _insert_job(self.conn, job_id)
        _insert_pending_task(
            self.conn,
            task_id="tsk_n22_http",
            job_id=job_id,
            lane="http",
            idempotency_key="sha256:" + ("h1" * 32),
        )

        result = admit_task_with_governor(
            self.conn,
            task_id="tsk_n22_http",
            phase="ACQUIRE",
            metrics=_metrics_at_pct(50.0),
        )
        self.assertTrue(result.admitted)
        self.assertEqual(_fetch_task_state(self.conn, "tsk_n22_http"), "READY")

    def test_orange_denies_browser_in_acquire(self) -> None:
        job_id = "job_n22_orange_browser"
        _insert_job(self.conn, job_id)
        _insert_pending_task(
            self.conn,
            task_id="tsk_n22_browser",
            job_id=job_id,
            lane="browser",
            idempotency_key="sha256:" + ("b1" * 32),
        )

        result = admit_task_with_governor(
            self.conn,
            task_id="tsk_n22_browser",
            phase="ACQUIRE",
            metrics=_metrics_at_pct(75.0),
        )
        self.assertFalse(result.admitted)
        self.assertEqual(result.reason, "pressure_orange_no_browser")
        self.assertEqual(_fetch_task_state(self.conn, "tsk_n22_browser"), "PENDING")

    def test_red_denies_http_fetch(self) -> None:
        job_id = "job_n22_red_http"
        _insert_job(self.conn, job_id)
        _insert_pending_task(
            self.conn,
            task_id="tsk_n22_red_http",
            job_id=job_id,
            lane="http",
            idempotency_key="sha256:" + ("r1" * 32),
        )

        result = admit_task_with_governor(
            self.conn,
            task_id="tsk_n22_red_http",
            phase="ACQUIRE",
            metrics=_metrics_at_pct(90.0),
        )
        self.assertFalse(result.admitted)
        self.assertEqual(result.reason, "pressure_red_pause")
        self.assertEqual(_fetch_task_state(self.conn, "tsk_n22_red_http"), "PENDING")

    def test_backpressure_denies_fetch_under_green_memory(self) -> None:
        job_id = "job_n22_bp"
        _insert_job(self.conn, job_id)
        _insert_backlog_tasks(
            self.conn,
            lane="extract",
            count=500,
            job_id=job_id,
            prefix="tsk_n22_extract_backlog",
        )
        _insert_pending_task(
            self.conn,
            task_id="tsk_n22_bp_http",
            job_id=job_id,
            lane="http",
            idempotency_key="sha256:" + ("p1" * 32),
        )

        governor = evaluate_governor(
            self.conn,
            phase="ACQUIRE",
            metrics=_metrics_at_pct(40.0),
        )
        self.assertTrue(governor.combined_fetch_paused)

        result = admit_task_with_governor(
            self.conn,
            task_id="tsk_n22_bp_http",
            phase="ACQUIRE",
            metrics=_metrics_at_pct(40.0),
        )
        self.assertFalse(result.admitted)
        self.assertEqual(result.reason, governor.backpressure.reason)

    def test_enrich_phase_admits_enrich_not_index(self) -> None:
        job_id = "job_n22_enrich_phase"
        _insert_job(self.conn, job_id)
        _insert_pending_task(
            self.conn,
            task_id="tsk_n22_enrich",
            job_id=job_id,
            lane="enrich",
            idempotency_key="sha256:" + ("e1" * 32),
        )
        _insert_pending_task(
            self.conn,
            task_id="tsk_n22_index_blocked",
            job_id=job_id,
            lane="index",
            idempotency_key="sha256:" + ("i1" * 32),
        )

        enrich_result = admit_task_with_governor(
            self.conn,
            task_id="tsk_n22_enrich",
            phase="ENRICH",
            metrics=_metrics_at_pct(40.0),
        )
        index_result = admit_task_with_governor(
            self.conn,
            task_id="tsk_n22_index_blocked",
            phase="ENRICH",
            metrics=_metrics_at_pct(40.0),
        )

        self.assertTrue(enrich_result.admitted)
        self.assertFalse(index_result.admitted)
        self.assertEqual(index_result.reason, "phase_enrich_lane_disabled")

    def test_orange_denies_enrich_llm_admission(self) -> None:
        job_id = "job_n22_orange_enrich"
        _insert_job(self.conn, job_id)
        _insert_pending_task(
            self.conn,
            task_id="tsk_n22_orange_enrich",
            job_id=job_id,
            lane="enrich",
            idempotency_key="sha256:" + ("e2" * 32),
        )

        result = admit_task_with_governor(
            self.conn,
            task_id="tsk_n22_orange_enrich",
            phase="ENRICH",
            metrics=_metrics_at_pct(75.0),
        )
        self.assertFalse(result.admitted)
        self.assertEqual(result.reason, "pressure_orange_no_llm")

    def test_governor_wraps_scheduler_backpressure_gate(self) -> None:
        job_id = "job_n22_gate"
        _insert_job(self.conn, job_id)
        _insert_backlog_tasks(
            self.conn,
            lane="extract",
            count=500,
            job_id=job_id,
            prefix="tsk_n22_gate_extract",
        )
        _insert_pending_task(
            self.conn,
            task_id="tsk_n22_gate_http",
            job_id=job_id,
            lane="http",
            idempotency_key="sha256:" + ("g1" * 32),
        )

        gate = BackpressureGate()
        governor = evaluate_governor(
            self.conn,
            phase="ACQUIRE",
            metrics=_metrics_at_pct(40.0),
            backpressure_gate=gate,
        )
        self.assertTrue(governor.combined_fetch_paused)

        direct = admit_task(
            self.conn,
            task_id="tsk_n22_gate_http",
            backpressure=governor.backpressure,
            backpressure_gate=gate,
        )
        self.assertFalse(direct.admitted)
        self.assertEqual(direct.reason, "backpressure_paused")

        governed = admit_task_with_governor(
            self.conn,
            task_id="tsk_n22_gate_http",
            phase="ACQUIRE",
            metrics=_metrics_at_pct(40.0),
            backpressure_gate=gate,
        )
        self.assertFalse(governed.admitted)
        self.assertEqual(governed.reason, governor.backpressure.reason)


class IntegrationCheckGovernor(unittest.TestCase):
    """Integration check hook: phase gating + backpressure under memory pressure."""

    def test_phase_gating_and_pressure_offline(self) -> None:
        suite = unittest.TestSuite()
        suite.addTest(MemoryPressureUnitTests("test_classify_green_orange_red_boundaries"))
        suite.addTest(MemoryPressureUnitTests("test_orange_throttles_browser_and_llm"))
        suite.addTest(MemoryPressureUnitTests("test_red_pauses_fetch_and_parsers"))
        suite.addTest(PhaseGatingUnitTests("test_acquire_enables_browser_disables_enrich"))
        suite.addTest(PhaseGatingUnitTests("test_enrich_enables_llm_lane_disables_browser"))
        suite.addTest(PhaseGatingUnitTests("test_index_enables_index_lane_only"))
        suite.addTest(GovernorDecisionUnitTests("test_orange_blocks_browser_even_in_acquire"))
        suite.addTest(GovernorDecisionUnitTests("test_enrich_phase_blocks_index_lane"))
        result = unittest.TextTestRunner(verbosity=0).run(suite)
        self.assertTrue(result.wasSuccessful())

    @unittest.skipUnless(_postgres_available(), "Postgres unavailable (need .env)")
    def test_backpressure_and_admission_with_postgres(self) -> None:
        suite = unittest.TestSuite()
        suite.addTest(GovernorAdmissionIntegrationTests("test_acquire_green_admits_http"))
        suite.addTest(GovernorAdmissionIntegrationTests("test_orange_denies_browser_in_acquire"))
        suite.addTest(GovernorAdmissionIntegrationTests("test_red_denies_http_fetch"))
        suite.addTest(GovernorAdmissionIntegrationTests("test_backpressure_denies_fetch_under_green_memory"))
        suite.addTest(GovernorAdmissionIntegrationTests("test_enrich_phase_admits_enrich_not_index"))
        suite.addTest(GovernorAdmissionIntegrationTests("test_orange_denies_enrich_llm_admission"))
        suite.addTest(GovernorAdmissionIntegrationTests("test_governor_wraps_scheduler_backpressure_gate"))
        result = unittest.TextTestRunner(verbosity=0).run(suite)
        self.assertTrue(result.wasSuccessful())


if __name__ == "__main__":
    unittest.main()
