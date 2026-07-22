"""N17 lineage — edges, trace, diff, replay (Guard 6 / Gate 1 slice)."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
import uuid
from pathlib import Path
from typing import Any

import psycopg
from fastapi.testclient import TestClient

from control.api.app import create_app
from control.api.deps import get_db
from control.api.settings import ControlApiSettings
from control.api.readiness import ReconcilerReadiness
from db.repositories.connection import connect
from db.repositories.migrate import apply_migrations
from fixtures.load import FIXTURES_ROOT
from lineage.diff import diff_lineage
from lineage.edges import list_edges, write_lineage_edge
from lineage.replay import replay_lineage
from lineage.trace import trace, trace_ancestors
from workers.extract_worker import run_fetch_and_extract
from workers.search_worker import (
    _fetch_task_id,
    run_search_from_fixture,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
ENV_FILE = REPO_ROOT / ".env"
CONFIG_HASH = "sha256:" + ("c" * 64)
CREATED_AT = "2026-07-21T14:00:00Z"
FETCHED_AT = "2026-07-21T14:05:00Z"
ARTICLE_URL = "https://trailgearlab.example/articles/portable-camping-fans"
CAMPING_FIXTURE = FIXTURES_ROOT / "search" / "searxng_portable_camping_fan.json"

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


def _test_settings() -> ControlApiSettings:
    return ControlApiSettings(
        host="127.0.0.1",
        port=8099,
        bearer_token="test-token-n17",
    )


def _insert_job(conn: psycopg.Connection, job_id: str) -> None:
    provenance = {
        "schema_version": "job.v1",
        "config_hash": CONFIG_HASH,
        "created_at": CREATED_AT,
    }
    conn.execute(
        """
        INSERT INTO research_jobs (
            job_id, job_kind, status, config_hash, budget, provenance
        )
        VALUES (%s, %s, %s, %s, %s::jsonb, %s::jsonb)
        ON CONFLICT (job_id) DO NOTHING
        """,
        (
            job_id,
            "dossier",
            "ACQUIRING",
            CONFIG_HASH,
            json.dumps(BUDGET),
            json.dumps(provenance),
        ),
    )


def _build_gate1_chain(conn: psycopg.Connection, job_id: str, storage_root: Path) -> dict[str, str]:
    search = run_search_from_fixture(
        conn,
        job_id=job_id,
        config_hash=CONFIG_HASH,
        created_at=CREATED_AT,
        fixture_path=CAMPING_FIXTURE,
        enqueue_fetch=False,
    )
    fetch_task_id = _fetch_task_id(
        job_id,
        search.query_spec.query_spec_id,
        ARTICLE_URL,
    )
    extract = run_fetch_and_extract(
        conn,
        fetch_task_id=fetch_task_id,
        url=ARTICLE_URL,
        config_hash=CONFIG_HASH,
        created_at=CREATED_AT,
        fetched_at=FETCHED_AT,
        storage_root=storage_root,
    )
    return {
        "job_id": job_id,
        "query_spec_id": search.query_spec.query_spec_id,
        "fetch_task_id": fetch_task_id,
        "page_id": extract.page["page_id"],
    }


@unittest.skipUnless(_postgres_available(), "Postgres unavailable (need .env POSTGRES_*)")
class LineagePostgresTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.conn = connect()
        apply_migrations(cls.conn)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.conn.close()

    def setUp(self) -> None:
        self.conn.execute("SAVEPOINT n17_test_case")
        self.job_id = f"job_n17_{uuid.uuid4().hex[:12]}"
        self.storage_dir = tempfile.TemporaryDirectory(prefix="n17-lineage-")
        self.storage_root = Path(self.storage_dir.name)
        _insert_job(self.conn, self.job_id)
        self.chain = _build_gate1_chain(self.conn, self.job_id, self.storage_root)

    def tearDown(self) -> None:
        self.storage_dir.cleanup()
        self.conn.execute("ROLLBACK TO SAVEPOINT n17_test_case")

    def test_list_edges_filters_by_child(self) -> None:
        page_id = self.chain["page_id"]
        edges = list_edges(
            self.conn,
            child_kind="page.v1",
            child_id=page_id,
        )
        self.assertEqual(len(edges), 1)
        self.assertEqual(edges[0].parent_kind, "task")
        self.assertEqual(edges[0].relation, "derived_from")

    def test_trace_reaches_query_spec_from_page(self) -> None:
        result = trace(self.conn, self.chain["page_id"])
        self.assertTrue(result.as_dict()["complete_to_query_spec"])
        leaf_ids = {leaf["query_spec_id"] for leaf in result.query_spec_leaves}
        self.assertIn(self.chain["query_spec_id"], leaf_ids)
        node_kinds = {node.kind for node in result.nodes}
        self.assertIn("page.v1", node_kinds)
        self.assertIn("task", node_kinds)
        self.assertIn("query_spec", node_kinds)

    def test_write_lineage_edge_is_idempotent(self) -> None:
        first = write_lineage_edge(
            self.conn,
            child_kind="page.v1",
            child_id="pg_test_idempotent",
            parent_kind="task",
            parent_id="tsk_test_idempotent",
            relation="derived_from",
            version_tag="extract-test",
        )
        second = write_lineage_edge(
            self.conn,
            child_kind="page.v1",
            child_id="pg_test_idempotent",
            parent_kind="task",
            parent_id="tsk_test_idempotent",
            relation="derived_from",
            version_tag="extract-test",
        )
        self.assertTrue(first)
        self.assertFalse(second)

    def test_diff_identical_roots(self) -> None:
        page_id = self.chain["page_id"]
        diff = diff_lineage(
            self.conn,
            left_kind="page.v1",
            left_id=page_id,
            right_kind="page.v1",
            right_id=page_id,
        )
        self.assertTrue(diff.as_dict()["identical"])

    def test_diff_detects_extra_branch(self) -> None:
        page_id = self.chain["page_id"]
        extra_qs = "qs_n17_extra_branch"
        self.conn.execute(
            """
            INSERT INTO query_specs (query_spec_id, job_id, text, engine, params)
            VALUES (%s, %s, %s, %s, '{}'::jsonb)
            """,
            (extra_qs, self.job_id, "extra query", "searxng"),
        )
        write_lineage_edge(
            self.conn,
            child_kind="task",
            child_id="tsk_n17_extra",
            parent_kind="query_spec",
            parent_id=extra_qs,
            relation="discovered_from",
            version_tag="search-test",
        )
        left = trace_ancestors(self.conn, root_kind="page.v1", root_id=page_id)
        right = trace_ancestors(self.conn, root_kind="task", root_id="tsk_n17_extra")
        diff = diff_lineage(
            self.conn,
            left_kind="page.v1",
            left_id=page_id,
            right_kind="task",
            right_id="tsk_n17_extra",
        )
        payload = diff.as_dict()
        self.assertFalse(payload["identical"])
        self.assertTrue(payload["nodes_only_left"])
        self.assertTrue(payload["nodes_only_right"])
        self.assertGreaterEqual(len(left.nodes), len(right.nodes))

    def test_replay_emits_query_specs_with_version_pins(self) -> None:
        plan = replay_lineage(
            self.conn,
            self.chain["page_id"],
            pin_versions=True,
        )
        payload = plan.as_dict()
        self.assertTrue(payload["replayable"])
        self.assertEqual(len(payload["query_specs"]), 1)
        spec = payload["query_specs"][0]
        self.assertEqual(spec["query_spec_id"], self.chain["query_spec_id"])
        self.assertTrue(spec["version_pins"])

    def test_lineage_api_trace_and_edges(self) -> None:
        readiness = ReconcilerReadiness()
        readiness.mark_ready()
        app = create_app(
            settings=_test_settings(),
            readiness=readiness,
            run_startup_reconciler=False,
        )

        def override_db() -> Any:
            yield self.conn

        app.dependency_overrides[get_db] = override_db
        page_id = self.chain["page_id"]
        try:
            with TestClient(app) as client:
                trace_resp = client.get(f"/v1/lineage/trace/{page_id}")
                self.assertEqual(trace_resp.status_code, 200)
                body = trace_resp.json()
                self.assertTrue(body["complete_to_query_spec"])
                self.assertEqual(
                    body["query_spec_leaves"][0]["query_spec_id"],
                    self.chain["query_spec_id"],
                )

                edges_resp = client.get(
                    "/v1/lineage/edges",
                    params={"child_kind": "page.v1", "child_id": page_id},
                )
                self.assertEqual(edges_resp.status_code, 200)
                self.assertEqual(edges_resp.json()["count"], 1)

                replay_resp = client.post(
                    "/v1/lineage/replay",
                    json={"artifact_id": page_id, "pin_versions": True},
                )
                self.assertEqual(replay_resp.status_code, 200)
                self.assertTrue(replay_resp.json()["replayable"])

                diff_resp = client.get(
                    "/v1/lineage/diff",
                    params={"left_id": page_id, "right_id": page_id},
                )
                self.assertEqual(diff_resp.status_code, 200)
                self.assertTrue(diff_resp.json()["identical"])
        finally:
            app.dependency_overrides.clear()


class IntegrationCheckLineage(unittest.TestCase):
    """N17 integration check: page.v1 trace reaches query_spec (Guard 6 slice)."""

    @unittest.skipUnless(_postgres_available(), "Postgres unavailable (need .env POSTGRES_*)")
    def test_page_trace_reaches_query_spec(self) -> None:
        suite = unittest.TestSuite()
        suite.addTest(LineagePostgresTests("test_trace_reaches_query_spec_from_page"))
        suite.addTest(LineagePostgresTests("test_lineage_api_trace_and_edges"))
        result = unittest.TextTestRunner(verbosity=0).run(suite)
        self.assertTrue(result.wasSuccessful())


if __name__ == "__main__":
    unittest.main()
