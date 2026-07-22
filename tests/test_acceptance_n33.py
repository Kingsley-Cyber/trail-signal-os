"""N33 ACCEPTANCE — camping-fixture dossier path, lineage trace, score replay (Gate 7)."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
import uuid
from pathlib import Path

import psycopg

from db.repositories.connection import connect
from db.repositories.migrate import apply_migrations
from fixtures.load import (
    CAMPING_EXPECTED_CONFIDENCE,
    CAMPING_EXPECTED_SCORE,
    FIXTURES_ROOT,
    load_fixtures,
)
from graph.compiler import compile_workflow_file
from graph.executor import execute_compiled_node
from guards.runtime_guards import guard12_assert_score_reproducible
from lineage.replay import replay_lineage
from lineage.trace import trace
from signal_engine.score import load_weights, score, score_camping_fixture, validate_opportunity_v1
from signal_engine.tiers import diff_opportunity_scores, replay_opportunity_score
from workers.extract_worker import run_fetch_and_extract
from workers.search_worker import _fetch_task_id, run_search_from_fixture

REPO_ROOT = Path(__file__).resolve().parents[1]
DOSSIER_YAML = REPO_ROOT / "graph" / "defs" / "dossier.yaml"
CAMPING_FIXTURE = FIXTURES_ROOT / "search" / "searxng_portable_camping_fan.json"
EXPECTED_OPPORTUNITY_PATH = (
    REPO_ROOT / "fixtures" / "niches" / "camping-fixture" / "expected_opportunity.json"
)
ENV_FILE = REPO_ROOT / ".env"
AS_OF = "2026-07-21T12:00:00Z"
CONFIG_HASH = "sha256:" + ("c" * 64)
CREATED_AT = "2026-07-21T14:00:00Z"
FETCHED_AT = "2026-07-21T14:05:00Z"
ARTICLE_URL = "https://trailgearlab.example/articles/portable-camping-fans"

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


def _insert_job(conn: psycopg.Connection, job_id: str) -> None:
    provenance = {
        "schema_version": "job.v1",
        "config_hash": CONFIG_HASH,
        "created_at": CREATED_AT,
    }
    conn.execute(
        """
        INSERT INTO research_jobs (
            job_id, job_kind, status, config_hash, budget, provenance, niche_id
        )
        VALUES (%s, %s, %s, %s, %s::jsonb, %s::jsonb, %s)
        ON CONFLICT (job_id) DO NOTHING
        """,
        (
            job_id,
            "dossier",
            "ACQUIRING",
            CONFIG_HASH,
            json.dumps(BUDGET),
            json.dumps(provenance),
            "camping-fixture",
        ),
    )


def _build_camping_lineage_chain(
    conn: psycopg.Connection,
    job_id: str,
    storage_root: Path,
) -> dict[str, str]:
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


class CampingFixtureDossierTests(unittest.TestCase):
    """Offline camping-fixture → dossier score path."""

    def test_score_camping_fixture_matches_golden(self) -> None:
        corpus = load_fixtures()
        golden = json.loads(EXPECTED_OPPORTUNITY_PATH.read_text(encoding="utf-8"))
        opportunity = score_camping_fixture()
        validate_opportunity_v1(opportunity)

        self.assertEqual(opportunity["score"], CAMPING_EXPECTED_SCORE)
        self.assertEqual(opportunity["confidence"], CAMPING_EXPECTED_CONFIDENCE)
        self.assertEqual(opportunity["score"], golden["score"])
        self.assertEqual(opportunity["subscores"], golden["subscores"])
        self.assertEqual(opportunity["confidence"], golden["confidence"])
        self.assertEqual(opportunity["niche_id"], corpus.camping_signals["niche_id"])

    def test_dossier_workflow_score_node_reproduces_golden(self) -> None:
        compiled = compile_workflow_file(DOSSIER_YAML)
        expected = score_camping_fixture()
        validate_opportunity_v1(expected)
        corpus = load_fixtures()
        signal = dict(corpus.camping_signals["signals"][0])

        execution = execute_compiled_node(
            compiled,
            "score_opportunity",
            signal,
            deterministic_fn=lambda _packed: dict(expected),
        )
        self.assertEqual(execution.workflow_id, "wf_dossier")
        self.assertEqual(execution.node_id, "score_opportunity")
        self.assertEqual(execution.result.verdict, "pass")
        self.assertEqual(execution.result.output["score"], CAMPING_EXPECTED_SCORE)


class ScoreReplayTests(unittest.TestCase):
    """lineage.replay / deterministic replay reproduces camping-fixture score."""

    def test_replay_opportunity_score_is_byte_identical(self) -> None:
        corpus = load_fixtures()
        weights = load_weights()
        baseline = score(corpus.camping_signals, weights, as_of=AS_OF)
        self.assertEqual(baseline.score, CAMPING_EXPECTED_SCORE)

        first = replay_opportunity_score(
            corpus.camping_signals,
            weights,
            niche_id="camping-fixture",
            as_of=AS_OF,
        )
        second = replay_opportunity_score(
            corpus.camping_signals,
            weights,
            niche_id="camping-fixture",
            as_of=AS_OF,
        )
        self.assertEqual(first.score, second.score)
        self.assertEqual(first.confidence, second.confidence)
        self.assertEqual(first.subscores, second.subscores)

        guard12_assert_score_reproducible(
            lambda: replay_opportunity_score(
                corpus.camping_signals,
                weights,
                niche_id="camping-fixture",
                as_of=AS_OF,
            ).score,
            expected=CAMPING_EXPECTED_SCORE,
        )

    def test_replayed_opportunity_diff_is_identical(self) -> None:
        golden = json.loads(EXPECTED_OPPORTUNITY_PATH.read_text(encoding="utf-8"))
        opportunity = score_camping_fixture()
        diff = diff_opportunity_scores(golden, opportunity)
        self.assertTrue(diff["identical"])
        self.assertEqual(diff["score_delta"], 0.0)
        self.assertEqual(diff["confidence_delta"], 0.0)


@unittest.skipUnless(_postgres_available(), "Postgres unavailable (need .env POSTGRES_*)")
class LineageAcceptanceTests(unittest.TestCase):
    """lineage.trace complete to query_spec leaves on camping-fixture chain."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.conn = connect()
        apply_migrations(cls.conn)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.conn.close()

    def setUp(self) -> None:
        self.conn.execute("SAVEPOINT n33_acceptance")
        self.job_id = f"job_n33_{uuid.uuid4().hex[:12]}"
        self.storage_dir = tempfile.TemporaryDirectory(prefix="n33-acceptance-")
        self.storage_root = Path(self.storage_dir.name)
        _insert_job(self.conn, self.job_id)
        self.chain = _build_camping_lineage_chain(self.conn, self.job_id, self.storage_root)

    def tearDown(self) -> None:
        self.storage_dir.cleanup()
        self.conn.execute("ROLLBACK TO SAVEPOINT n33_acceptance")

    def test_trace_complete_to_query_spec(self) -> None:
        result = trace(self.conn, self.chain["page_id"])
        payload = result.as_dict()
        self.assertTrue(payload["complete_to_query_spec"])
        leaf_ids = {leaf["query_spec_id"] for leaf in result.query_spec_leaves}
        self.assertIn(self.chain["query_spec_id"], leaf_ids)
        node_kinds = {node.kind for node in result.nodes}
        self.assertIn("page.v1", node_kinds)
        self.assertIn("query_spec", node_kinds)

    def test_lineage_replay_emits_query_specs(self) -> None:
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


class IntegrationCheckAcceptance(unittest.TestCase):
    """N33 integration_check: camping-fixture → dossier → 0.72; trace complete; replay reproduces."""

    def test_offline_dossier_score_and_replay(self) -> None:
        suite = unittest.TestSuite()
        suite.addTest(CampingFixtureDossierTests("test_score_camping_fixture_matches_golden"))
        suite.addTest(CampingFixtureDossierTests("test_dossier_workflow_score_node_reproduces_golden"))
        suite.addTest(ScoreReplayTests("test_replay_opportunity_score_is_byte_identical"))
        suite.addTest(ScoreReplayTests("test_replayed_opportunity_diff_is_identical"))
        result = unittest.TextTestRunner(verbosity=0).run(suite)
        self.assertTrue(result.wasSuccessful())

    @unittest.skipUnless(_postgres_available(), "Postgres unavailable (need .env POSTGRES_*)")
    def test_lineage_trace_and_replay_on_camping_fixture(self) -> None:
        suite = unittest.TestSuite()
        suite.addTest(LineageAcceptanceTests("test_trace_complete_to_query_spec"))
        suite.addTest(LineageAcceptanceTests("test_lineage_replay_emits_query_specs"))
        result = unittest.TextTestRunner(verbosity=0).run(suite)
        self.assertTrue(result.wasSuccessful())


if __name__ == "__main__":
    unittest.main()
