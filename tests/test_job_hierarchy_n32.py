"""N32 job hierarchy + VALIDATE fan-out — dossier parent/child jobs and sub-graph."""

from __future__ import annotations

import os
import unittest
import uuid
from pathlib import Path

import psycopg
from fastapi.testclient import TestClient

from control.api.app import create_app
from control.api.routes_jobs import (
    DOSSIER_EXPAND_KINDS,
    VALIDATE_FANOUT_SUBGRAPH_YAML,
    VALIDATE_FANOUT_WORKFLOW_ID,
    ensure_validate_fanout_subgraph,
    expand_dossier_job,
    shortlist_pain_points_from_opportunity,
    validate_fanout,
)
from control.api.readiness import ReconcilerReadiness
from control.api.settings import ControlApiSettings
from db.repositories.connection import connect
from db.repositories.migrate import apply_migrations
from graph.compiler import compile_workflow_yaml, render_mermaid
from lineage.edges import edges_for_child, edges_for_parent

REPO_ROOT = Path(__file__).resolve().parents[1]
ENV_FILE = REPO_ROOT / ".env"
TEST_TOKEN = "test-job-hierarchy-token"


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
        port=8100,
        bearer_token=TEST_TOKEN,
    )


def _auth_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {TEST_TOKEN}"}


def _ready_app() -> TestClient:
    readiness = ReconcilerReadiness()
    readiness.mark_ready()
    app = create_app(
        settings=_test_settings(),
        readiness=readiness,
        run_startup_reconciler=False,
    )
    return TestClient(app)


class ValidateFanoutSubgraphTests(unittest.TestCase):
    def test_validate_fanout_yaml_compiles_with_validator_prompt(self) -> None:
        compiled = compile_workflow_yaml(VALIDATE_FANOUT_SUBGRAPH_YAML)
        self.assertEqual(compiled.definition.workflow_id, VALIDATE_FANOUT_WORKFLOW_ID)
        validator = compiled.runtime_nodes["validator"]
        self.assertEqual(validator.kind, "llm")
        self.assertEqual(validator.prompt, "prompts/validator.md")
        self.assertEqual(validator.verifier, "claim_grounding")
        self.assertEqual(compiled.runtime_nodes["validation_gate"].kind, "deterministic")
        diagram = render_mermaid(compiled)
        self.assertIn("validator", diagram)
        self.assertIn("validation_gate", diagram)

    def test_validator_prompt_exists(self) -> None:
        prompt_path = REPO_ROOT / "prompts" / "validator.md"
        self.assertTrue(prompt_path.is_file())
        text = prompt_path.read_text(encoding="utf-8")
        self.assertIn("LAW 1", text)
        self.assertIn("claims", text)


class ShortlistPainTests(unittest.TestCase):
    def test_shortlist_pain_points_from_explanation_citations(self) -> None:
        opportunity = {
            "explanation": {
                "text": "Top pains cited.",
                "cited_record_ids": [
                    "ev_camping_pain_1042",
                    "ev_camping_pain_1108",
                    "ev_camping_pain_1155",
                ],
            }
        }
        specs = shortlist_pain_points_from_opportunity(opportunity)
        self.assertEqual(len(specs), 3)
        self.assertEqual(specs[0].pain_point_id, "pp_001")
        self.assertEqual(specs[0].record_ids, ["ev_camping_pain_1042"])


@unittest.skipUnless(_postgres_available(), "Postgres unavailable (need .env POSTGRES_*)")
class JobHierarchyRouteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.conn = connect()
        apply_migrations(cls.conn)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.conn.close()

    def setUp(self) -> None:
        self.conn.execute("SAVEPOINT n32_case")

    def tearDown(self) -> None:
        self.conn.execute("ROLLBACK TO SAVEPOINT n32_case")

    def test_create_child_job_with_parent_job_id(self) -> None:
        client = _ready_app()
        dossier_id = f"job_n32_parent_{uuid.uuid4().hex[:12]}"
        child_id = f"job_n32_child_{uuid.uuid4().hex[:12]}"
        with client:
            parent = client.post(
                "/v1/research-jobs",
                json={"job_id": dossier_id, "job_kind": "dossier", "niche_id": "camping-fixture"},
                headers=_auth_headers(),
            )
            self.assertEqual(parent.status_code, 201)
            self.assertIsNone(parent.json().get("parent_job_id"))

            child = client.post(
                "/v1/research-jobs",
                json={
                    "job_id": child_id,
                    "job_kind": "collection",
                    "parent_job_id": dossier_id,
                    "niche_id": "camping-fixture",
                },
                headers=_auth_headers(),
            )
            self.assertEqual(child.status_code, 201)
            self.assertEqual(child.json()["parent_job_id"], dossier_id)

            lineage = edges_for_child(self.conn, child_kind="job", child_id=child_id)
            self.assertTrue(
                any(
                    edge.parent_kind == "job"
                    and edge.parent_id == dossier_id
                    and edge.relation == "spawned_from"
                    for edge in lineage
                )
            )

    def test_expand_dossier_creates_skeleton_children(self) -> None:
        client = _ready_app()
        dossier_id = f"job_n32_expand_{uuid.uuid4().hex[:12]}"
        with client:
            create = client.post(
                "/v1/research-jobs",
                json={"job_id": dossier_id, "job_kind": "dossier", "niche_id": "camping-fixture"},
                headers=_auth_headers(),
            )
            self.assertEqual(create.status_code, 201)

            expanded = client.post(
                f"/v1/research-jobs/{dossier_id}/expand-dossier",
                headers=_auth_headers(),
            )
            self.assertEqual(expanded.status_code, 200)
            self.assertTrue(expanded.json()["expanded"])
            children = expanded.json()["children"]
            kinds = {child["job_kind"] for child in children}
            self.assertEqual(kinds, set(DOSSIER_EXPAND_KINDS) | {"collection"})
            self.assertEqual(sum(1 for child in children if child["job_kind"] == "collection"), 2)
            for child in children:
                self.assertEqual(child["parent_job_id"], dossier_id)

            children_resp = client.get(f"/v1/research-jobs/{dossier_id}/children")
            self.assertEqual(children_resp.status_code, 200)
            self.assertEqual(len(children_resp.json()["children"]), len(children))

            dossier = client.get(f"/v1/research-jobs/{dossier_id}")
            self.assertEqual(dossier.json()["status"], "PLANNING")

            second = client.post(
                f"/v1/research-jobs/{dossier_id}/expand-dossier",
                headers=_auth_headers(),
            )
            self.assertFalse(second.json()["expanded"])

    def test_validate_fanout_creates_validation_jobs_and_subgraph_runs(self) -> None:
        client = _ready_app()
        dossier_id = f"job_n32_fanout_{uuid.uuid4().hex[:12]}"
        pain_points = [
            {
                "pain_point_id": "pp_001",
                "record_ids": ["ev_camping_pain_1042"],
                "label": "battery life",
            },
            {
                "pain_point_id": "pp_002",
                "record_ids": ["ev_camping_pain_1108"],
                "label": "motor noise",
            },
        ]
        with client:
            create = client.post(
                "/v1/research-jobs",
                json={"job_id": dossier_id, "job_kind": "dossier", "niche_id": "camping-fixture"},
                headers=_auth_headers(),
            )
            self.assertEqual(create.status_code, 201)

            fanout = client.post(
                f"/v1/research-jobs/{dossier_id}/validate-fanout",
                json={
                    "opportunity_id": "opp_camping_fixture",
                    "pain_points": pain_points,
                },
                headers=_auth_headers(),
            )
            self.assertEqual(fanout.status_code, 200)
            payload = fanout.json()
            self.assertEqual(payload["workflow_id"], VALIDATE_FANOUT_WORKFLOW_ID)
            self.assertEqual(len(payload["validation_jobs"]), 2)
            self.assertEqual(len(payload["workflow_runs"]), 2)

            validation_jobs = [
                child
                for child in client.get(f"/v1/research-jobs/{dossier_id}/children").json()["children"]
                if child["job_kind"] == "validation"
            ]
            self.assertEqual(len(validation_jobs), 2)
            for job in validation_jobs:
                self.assertEqual(job["parent_job_id"], dossier_id)
                self.assertEqual(
                    job["provenance"].get("validate_subgraph"),
                    VALIDATE_FANOUT_WORKFLOW_ID,
                )

            with self.conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT COUNT(*)
                    FROM workflow_runs
                    WHERE workflow_id = %s
                      AND job_id = ANY(%s)
                    """,
                    (
                        VALIDATE_FANOUT_WORKFLOW_ID,
                        [job["job_id"] for job in validation_jobs],
                    ),
                )
                run_count = cur.fetchone()[0]
            self.assertEqual(run_count, 2)

            pain_edges = edges_for_parent(
                self.conn,
                parent_kind="evidence",
                parent_id="ev_camping_pain_1042",
            )
            self.assertTrue(
                any(edge.relation == "validates_pain" for edge in pain_edges)
            )

            repeat = client.post(
                f"/v1/research-jobs/{dossier_id}/validate-fanout",
                json={"pain_points": pain_points},
                headers=_auth_headers(),
            )
            self.assertEqual(len(repeat.json()["validation_jobs"]), 2)
            self.assertEqual(len(repeat.json()["workflow_runs"]), 2)

    def test_existing_create_and_lifecycle_routes_still_work(self) -> None:
        client = _ready_app()
        job_id = f"job_n32_legacy_{uuid.uuid4().hex[:12]}"
        with client:
            create = client.post(
                "/v1/research-jobs",
                json={"job_id": job_id, "job_kind": "dossier"},
                headers=_auth_headers(),
            )
            self.assertEqual(create.status_code, 201)
            pause = client.post(f"/v1/research-jobs/{job_id}/pause", headers=_auth_headers())
            self.assertEqual(pause.status_code, 200)
            self.assertEqual(pause.json()["status"], "PAUSED")


@unittest.skipUnless(_postgres_available(), "Postgres unavailable (need .env POSTGRES_*)")
class ValidateFanoutPersistTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.conn = connect()
        apply_migrations(cls.conn)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.conn.close()

    def setUp(self) -> None:
        self.conn.execute("SAVEPOINT n32_persist")

    def tearDown(self) -> None:
        self.conn.execute("ROLLBACK TO SAVEPOINT n32_persist")

    def test_ensure_validate_fanout_subgraph_persists_workflow_rows(self) -> None:
        workflow_id = ensure_validate_fanout_subgraph(self.conn)
        self.assertEqual(workflow_id, VALIDATE_FANOUT_WORKFLOW_ID)
        with self.conn.cursor() as cur:
            cur.execute(
                """
                SELECT node_id
                FROM workflow_nodes
                WHERE workflow_id = %s
                  AND node_id NOT IN ('__start__', '__end__')
                ORDER BY node_id
                """,
                (workflow_id,),
            )
            node_ids = [row[0] for row in cur.fetchall()]
        self.assertEqual(node_ids, ["validation_gate", "validator"])


class IntegrationCheckJobHierarchy(unittest.TestCase):
    """N32 integration_check: dossier parent/child jobs; VALIDATE fan-out sub-graph."""

    def test_offline_subgraph_and_prompt(self) -> None:
        suite = unittest.TestSuite()
        suite.addTest(ValidateFanoutSubgraphTests("test_validate_fanout_yaml_compiles_with_validator_prompt"))
        suite.addTest(ValidateFanoutSubgraphTests("test_validator_prompt_exists"))
        suite.addTest(ShortlistPainTests("test_shortlist_pain_points_from_explanation_citations"))
        result = unittest.TextTestRunner(verbosity=0).run(suite)
        self.assertTrue(result.wasSuccessful())

    @unittest.skipUnless(_postgres_available(), "Postgres unavailable (need .env POSTGRES_*)")
    def test_live_dossier_hierarchy_and_validate_fanout(self) -> None:
        suite = unittest.TestSuite()
        suite.addTest(JobHierarchyRouteTests("test_expand_dossier_creates_skeleton_children"))
        suite.addTest(JobHierarchyRouteTests("test_validate_fanout_creates_validation_jobs_and_subgraph_runs"))
        suite.addTest(ValidateFanoutPersistTests("test_ensure_validate_fanout_subgraph_persists_workflow_rows"))
        result = unittest.TextTestRunner(verbosity=0).run(suite)
        self.assertTrue(result.wasSuccessful())


if __name__ == "__main__":
    unittest.main()
