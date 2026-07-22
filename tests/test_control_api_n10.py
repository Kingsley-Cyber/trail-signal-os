"""N10 control API — /readyz reconciler gate, bearer auth, route smoke tests."""

from __future__ import annotations

import os
import unittest
import uuid
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import psycopg
from fastapi.testclient import TestClient

from control.api.app import CONTROL_API_PORT, create_app
from control.api.config_hash import hash_config_files
from control.api.readiness import ReconcilerReadiness
from control.api.settings import ControlApiSettings
from control.reconciliation import ReconcilerPassResult, run_reconciler_pass
from db.repositories.connection import connect
from db.repositories.migrate import apply_migrations

REPO_ROOT = Path(__file__).resolve().parents[1]
ENV_FILE = REPO_ROOT / ".env"
TEST_TOKEN = "test-control-api-token"


def _load_dotenv() -> None:
    if not ENV_FILE.is_file():
        return
    for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())


def _test_settings() -> ControlApiSettings:
    return ControlApiSettings(
        host="127.0.0.1",
        port=CONTROL_API_PORT,
        bearer_token=TEST_TOKEN,
    )


def _auth_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {TEST_TOKEN}"}


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


class ReadyzGateTests(unittest.TestCase):
    def test_healthz_always_ok_without_auth(self) -> None:
        readiness = ReconcilerReadiness()
        app = create_app(
            settings=_test_settings(),
            readiness=readiness,
            run_startup_reconciler=False,
        )
        with TestClient(app) as client:
            response = client.get("/healthz")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ok"})

    def test_readyz_not_ready_before_first_pass(self) -> None:
        readiness = ReconcilerReadiness()
        app = create_app(
            settings=_test_settings(),
            readiness=readiness,
            run_startup_reconciler=False,
        )
        with TestClient(app) as client:
            response = client.get("/readyz")
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["detail"]["reason"], "reconciler_first_pass_pending")

    def test_readyz_ready_after_reconciler_first_pass(self) -> None:
        calls: list[str] = []

        def fake_run_pass(conn: Any, redis_client: Any = None, **kwargs: Any) -> ReconcilerPassResult:
            calls.append("ran")
            return ReconcilerPassResult()

        readiness = ReconcilerReadiness(
            connect_fn=lambda: MagicMock(close=MagicMock()),
            run_pass_fn=fake_run_pass,
        )
        app = create_app(
            settings=_test_settings(),
            readiness=readiness,
            run_startup_reconciler=True,
        )
        with TestClient(app) as client:
            response = client.get("/readyz")
        self.assertEqual(calls, ["ran"])
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "ready")
        self.assertTrue(response.json()["reconciler_first_pass"])

    def test_readyz_mark_ready_without_running_reconciler(self) -> None:
        readiness = ReconcilerReadiness()
        readiness.mark_ready()
        app = create_app(
            settings=_test_settings(),
            readiness=readiness,
            run_startup_reconciler=False,
        )
        with TestClient(app) as client:
            response = client.get("/readyz")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["reconciler_first_pass"], True)

    def test_control_api_port_is_8100(self) -> None:
        self.assertEqual(CONTROL_API_PORT, 8100)
        self.assertEqual(_test_settings().port, 8100)

    def test_readyz_stays_not_ready_when_reconciler_raises(self) -> None:
        def failing_run_pass(conn: Any, redis_client: Any = None, **kwargs: Any) -> ReconcilerPassResult:
            raise RuntimeError("reconciler boom")

        readiness = ReconcilerReadiness(
            connect_fn=lambda: MagicMock(close=MagicMock()),
            run_pass_fn=failing_run_pass,
        )
        app = create_app(
            settings=_test_settings(),
            readiness=readiness,
            run_startup_reconciler=True,
        )
        with TestClient(app) as client:
            response = client.get("/readyz")
        self.assertEqual(response.status_code, 503)
        self.assertEqual(
            response.json()["detail"]["reason"],
            "reconciler_first_pass_failed",
        )
        self.assertIn("reconciler boom", response.json()["detail"]["error"])


class ConfigHashTests(unittest.TestCase):
    def test_config_hash_is_deterministic_over_config_tree(self) -> None:
        first = hash_config_files()
        second = hash_config_files()
        self.assertEqual(first, second)
        self.assertTrue(first.startswith("sha256:"))
        self.assertNotEqual(first, "sha256:" + ("a" * 64))

    @unittest.skipUnless(_postgres_available(), "Postgres unavailable (need .env POSTGRES_*)")
    def test_create_job_stamps_real_config_hash(self) -> None:
        readiness = ReconcilerReadiness()
        readiness.mark_ready()
        app = create_app(
            settings=_test_settings(),
            readiness=readiness,
            run_startup_reconciler=False,
        )
        expected = hash_config_files()
        job_id = f"job_n10_{uuid.uuid4().hex[:12]}"
        with TestClient(app) as client:
            create = client.post(
                "/v1/research-jobs",
                json={"job_id": job_id, "job_kind": "dossier"},
                headers=_auth_headers(),
            )
        self.assertEqual(create.status_code, 201)
        self.assertEqual(create.json()["config_hash"], expected)
        self.assertEqual(create.json()["provenance"]["config_hash"], expected)


class BearerAuthTests(unittest.TestCase):
    def test_mutating_route_requires_bearer(self) -> None:
        readiness = ReconcilerReadiness()
        readiness.mark_ready()
        app = create_app(
            settings=_test_settings(),
            readiness=readiness,
            run_startup_reconciler=False,
        )
        with TestClient(app) as client:
            response = client.post(
                "/v1/research-jobs",
                json={"job_kind": "dossier"},
            )
        self.assertEqual(response.status_code, 401)

    def test_invalid_bearer_rejected(self) -> None:
        readiness = ReconcilerReadiness()
        readiness.mark_ready()
        app = create_app(
            settings=_test_settings(),
            readiness=readiness,
            run_startup_reconciler=False,
        )
        with TestClient(app) as client:
            response = client.post(
                "/v1/research-jobs",
                json={"job_kind": "dossier"},
                headers={"Authorization": "Bearer wrong-token"},
            )
        self.assertEqual(response.status_code, 401)


@unittest.skipUnless(_postgres_available(), "Postgres unavailable (need .env POSTGRES_*)")
class ControlApiRouteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.conn = connect()
        apply_migrations(cls.conn)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.conn.close()

    def setUp(self) -> None:
        self.conn.execute("SAVEPOINT n10_api_case")

    def tearDown(self) -> None:
        self.conn.execute("ROLLBACK TO SAVEPOINT n10_api_case")

    def test_create_and_get_job_with_bearer(self) -> None:
        readiness = ReconcilerReadiness()
        readiness.mark_ready()
        app = create_app(
            settings=_test_settings(),
            readiness=readiness,
            run_startup_reconciler=False,
        )
        job_id = f"job_n10_{uuid.uuid4().hex[:12]}"
        with TestClient(app) as client:
            create = client.post(
                "/v1/research-jobs",
                json={"job_id": job_id, "job_kind": "dossier"},
                headers=_auth_headers(),
            )
            self.assertEqual(create.status_code, 201)
            self.assertEqual(create.json()["job_id"], job_id)
            self.assertEqual(create.json()["status"], "CREATED")

            fetched = client.get(f"/v1/research-jobs/{job_id}")
            self.assertEqual(fetched.status_code, 200)
            self.assertEqual(fetched.json()["job_kind"], "dossier")

    def test_list_workers_and_domains(self) -> None:
        readiness = ReconcilerReadiness()
        readiness.mark_ready()
        app = create_app(
            settings=_test_settings(),
            readiness=readiness,
            run_startup_reconciler=False,
        )
        with TestClient(app) as client:
            workers = client.get("/v1/workers")
            self.assertEqual(workers.status_code, 200)
            self.assertIn("workers", workers.json())

            domain = client.get("/v1/domains/example.com")
            self.assertEqual(domain.status_code, 200)
            self.assertEqual(domain.json()["domain"], "example.com")
            self.assertFalse(domain.json()["profile_loaded"])

    def test_lineage_routes_are_registered(self) -> None:
        readiness = ReconcilerReadiness()
        readiness.mark_ready()
        app = create_app(
            settings=_test_settings(),
            readiness=readiness,
            run_startup_reconciler=False,
        )
        paths = app.openapi()["paths"]
        self.assertIn("/v1/lineage/trace/{artifact_id}", paths)
        self.assertIn("/v1/lineage/edges", paths)
        self.assertIn("/v1/lineage/diff", paths)
        self.assertIn("/v1/lineage/replay", paths)


@unittest.skipUnless(_postgres_available(), "Postgres unavailable (need .env POSTGRES_*)")
class ReadyzLiveReconcilerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.conn = connect()
        apply_migrations(cls.conn)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.conn.close()

    def test_startup_runs_reconciler_before_ready(self) -> None:
        calls: list[str] = []

        def counting_run_pass(conn: Any, redis_client: Any = None, **kwargs: Any) -> ReconcilerPassResult:
            calls.append("live-pass")
            return run_reconciler_pass(conn, redis_client)

        readiness = ReconcilerReadiness(
            connect_fn=connect,
            run_pass_fn=counting_run_pass,
        )
        app = create_app(
            settings=_test_settings(),
            readiness=readiness,
            run_startup_reconciler=True,
        )
        with TestClient(app) as client:
            before = client.get("/readyz")
        self.assertEqual(calls, ["live-pass"])
        self.assertEqual(before.status_code, 200)
        self.assertTrue(before.json()["reconciler_first_pass"])


class IntegrationCheckControlApi(unittest.TestCase):
    def test_readyz_gate_offline(self) -> None:
        suite = unittest.TestSuite()
        suite.addTest(ReadyzGateTests("test_readyz_not_ready_before_first_pass"))
        suite.addTest(ReadyzGateTests("test_readyz_ready_after_reconciler_first_pass"))
        suite.addTest(ReadyzGateTests("test_readyz_stays_not_ready_when_reconciler_raises"))
        result = unittest.TextTestRunner(verbosity=0).run(suite)
        self.assertTrue(result.wasSuccessful())

    def test_config_hash_offline(self) -> None:
        suite = unittest.TestSuite()
        suite.addTest(ConfigHashTests("test_config_hash_is_deterministic_over_config_tree"))
        result = unittest.TextTestRunner(verbosity=0).run(suite)
        self.assertTrue(result.wasSuccessful())

    def test_bearer_auth_offline(self) -> None:
        suite = unittest.TestSuite()
        suite.addTest(BearerAuthTests("test_mutating_route_requires_bearer"))
        result = unittest.TextTestRunner(verbosity=0).run(suite)
        self.assertTrue(result.wasSuccessful())

    @unittest.skipUnless(_postgres_available(), "Postgres unavailable (need .env POSTGRES_*)")
    def test_create_job_live(self) -> None:
        suite = unittest.TestSuite()
        suite.addTest(ControlApiRouteTests("test_create_and_get_job_with_bearer"))
        suite.addTest(ConfigHashTests("test_create_job_stamps_real_config_hash"))
        result = unittest.TextTestRunner(verbosity=0).run(suite)
        self.assertTrue(result.wasSuccessful())

    @unittest.skipUnless(_postgres_available(), "Postgres unavailable (need .env POSTGRES_*)")
    def test_live_reconciler_readyz(self) -> None:
        suite = unittest.TestSuite()
        suite.addTest(ReadyzLiveReconcilerTests("test_startup_runs_reconciler_before_ready"))
        result = unittest.TextTestRunner(verbosity=0).run(suite)
        self.assertTrue(result.wasSuccessful())


if __name__ == "__main__":
    unittest.main()
