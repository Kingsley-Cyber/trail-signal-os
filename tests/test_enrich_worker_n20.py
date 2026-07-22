"""N20 enrich_worker — evidence.v1 via cassette; invalid→repair NOT index."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

import httpx
import psycopg

from db.repositories.connection import connect
from db.repositories.migrate import apply_migrations
from fixtures.load import load_fixtures
from harness.gateway import GatewayMode, LLMGateway
from harness.litellm_adapter import CassetteNotFoundError
from harness.node_executor import VerifierResult
from lineage.edges import edges_for_child
from workers.enrich_worker import (
    CASSETTE_MODEL_ID,
    EnrichRepairRoute,
    EnrichSuccess,
    INDEX_LANE,
    PROMPT_VERSION,
    REPAIR_STREAM_NAME,
    build_replay_request,
    enrich_page,
    finalize_evidence,
    load_enrich_prompt,
    run_enrich_task,
    validate_evidence_v1,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
MODELS_PATH = REPO_ROOT / "config" / "models.yaml"
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


def _sample_job(job_id: str) -> tuple:
    provenance = {
        "schema_version": "job.v1",
        "config_hash": CONFIG_HASH,
        "created_at": CREATED_AT,
    }
    return (
        job_id,
        "dossier",
        "ACQUIRING",
        CONFIG_HASH,
        json.dumps(BUDGET),
        json.dumps(provenance),
    )


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


class PromptTests(unittest.TestCase):
    def test_prompt_file_loads_and_mentions_evidence_schema(self) -> None:
        prompt = load_enrich_prompt()
        self.assertIn("evidence.v1", prompt)
        self.assertIn("Do **not** score", prompt)


class ReplayRequestTests(unittest.TestCase):
    def setUp(self) -> None:
        corpus = load_fixtures()
        self.page = dict(corpus.page_goldens["review_page.page.v1.json"])
        self.cassette_request = dict(corpus.cassettes["enrich"][0]["request"])

    def test_build_replay_request_matches_enrich_cassette(self) -> None:
        request = build_replay_request(self.page)
        self.assertEqual(request["page_id"], self.cassette_request["page_id"])
        self.assertEqual(request["prompt_version"], PROMPT_VERSION)
        self.assertEqual(request["model_id"], CASSETTE_MODEL_ID)


class CassetteEnrichTests(unittest.TestCase):
    def setUp(self) -> None:
        self.gateway = LLMGateway(models_path=MODELS_PATH, mode=GatewayMode.REPLAY)
        corpus = load_fixtures()
        self.page = dict(corpus.page_goldens["review_page.page.v1.json"])
        cassette = corpus.cassettes["enrich"][0]
        self.replay_request = dict(cassette["request"])
        self.expected = dict(cassette["response"]["parsed"])

    def test_enrich_page_replays_cassette_to_valid_evidence(self) -> None:
        execution = enrich_page(
            self.page,
            gateway=self.gateway,
            replay_request=self.replay_request,
        )
        self.assertEqual(execution.verdict, "pass")
        self.assertTrue(execution.replayed)
        evidence = finalize_evidence(
            execution.output,
            self.page,
            config_hash=CONFIG_HASH,
            created_at=CREATED_AT,
            model_id=CASSETTE_MODEL_ID,
            enrich_task_id="tsk_enrich_fixture",
        )
        validate_evidence_v1(evidence)
        self.assertEqual(evidence["record_id"], self.expected["record_id"])
        self.assertEqual(evidence["derived_from"], [self.page["page_id"]])

    def test_missing_cassette_fails_without_live_call(self) -> None:
        with patch.object(httpx.Client, "post", side_effect=AssertionError("live LLM call attempted")):
            with self.assertRaises(CassetteNotFoundError):
                enrich_page(
                    self.page,
                    gateway=self.gateway,
                    replay_request={
                        "page_id": "pg_missing",
                        "prompt_version": PROMPT_VERSION,
                        "model_id": CASSETTE_MODEL_ID,
                    },
                )


def _insert_task(
    conn: psycopg.Connection,
    *,
    task_id: str,
    job_id: str,
    lane: str = "enrich",
) -> None:
    provenance = {
        "schema_version": "task.v1",
        "config_hash": CONFIG_HASH,
        "created_at": CREATED_AT,
    }
    conn.execute(
        """
        INSERT INTO tasks (
            task_id, job_id, task_kind, lane, state,
            idempotency_key, payload_ref, provenance
        )
        VALUES (%s, %s, 'enrich_page', %s, 'READY', %s, %s, %s::jsonb)
        ON CONFLICT (task_id) DO NOTHING
        """,
        (
            task_id,
            job_id,
            lane,
            f"sha256:{task_id}",
            json.dumps({"page_id": "pg_camping_review"}),
            json.dumps(provenance),
        ),
    )


@unittest.skipUnless(_postgres_available(), "Postgres unavailable (need .env POSTGRES_*)")
class PersistEnrichTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.conn = connect()
        apply_migrations(cls.conn)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.conn.close()

    def setUp(self) -> None:
        self.conn.execute("SAVEPOINT n20_test_case")
        self.job_id = f"job_n20_{uuid.uuid4().hex[:8]}"
        _insert_job(self.conn, self.job_id)
        corpus = load_fixtures()
        self.page = dict(corpus.page_goldens["review_page.page.v1.json"])
        self.replay_request = dict(corpus.cassettes["enrich"][0]["request"])
        self.expected = dict(corpus.cassettes["enrich"][0]["response"]["parsed"])
        self.gateway = LLMGateway(models_path=MODELS_PATH, mode=GatewayMode.REPLAY)

    def tearDown(self) -> None:
        self.conn.execute("ROLLBACK TO SAVEPOINT n20_test_case")

    def test_run_enrich_task_persists_evidence_and_lineage(self) -> None:
        enrich_task_id = "tsk_enrich_review"
        _insert_task(self.conn, task_id=enrich_task_id, job_id=self.job_id)
        with tempfile.TemporaryDirectory() as tmp:
            result = run_enrich_task(
                self.conn,
                job_id=self.job_id,
                page=self.page,
                enrich_task_id=enrich_task_id,
                config_hash=CONFIG_HASH,
                created_at=CREATED_AT,
                gateway=self.gateway,
                replay_request=self.replay_request,
                storage_root=Path(tmp),
            )
        self.assertIsInstance(result, EnrichSuccess)
        self.assertEqual(result.evidence["record_id"], self.expected["record_id"])
        self.assertTrue(result.replayed)
        edges = edges_for_child(
            self.conn,
            child_kind="evidence.v1",
            child_id=result.evidence["record_id"],
        )
        self.assertEqual(len(edges), 1)
        self.assertEqual(edges[0].parent_id, self.page["page_id"])

    def test_invalid_output_routes_repair_not_index(self) -> None:
        from harness.node_executor import NodeExecutionResult

        enrich_task_id = "tsk_enrich_fail"
        _insert_task(self.conn, task_id=enrich_task_id, job_id=self.job_id)
        ceiling = NodeExecutionResult(
            node_id="enrich_page",
            verdict="ceiling",
            attempts=2,
            output={"record_id": "ev_invalid"},
            violations=("forced invalid evidence",),
            replayed=True,
        )
        with patch("workers.enrich_worker.enrich_page", return_value=ceiling):
            result = run_enrich_task(
                self.conn,
                job_id=self.job_id,
                page=self.page,
                enrich_task_id=enrich_task_id,
                config_hash=CONFIG_HASH,
                created_at=CREATED_AT,
                gateway=self.gateway,
                replay_request=self.replay_request,
            )

        self.assertIsInstance(result, EnrichRepairRoute)
        self.assertEqual(result.repair_stream, REPAIR_STREAM_NAME)
        with self.conn.cursor() as cur:
            cur.execute(
                """
                SELECT lane, state
                FROM tasks
                WHERE task_id = %s
                """,
                (result.repair_task_id,),
            )
            repair_row = cur.fetchone()
            cur.execute(
                """
                SELECT stream_name
                FROM outbox_events
                WHERE task_id = %s
                """,
                (result.repair_task_id,),
            )
            stream_row = cur.fetchone()
            cur.execute(
                """
                SELECT COUNT(*)
                FROM tasks
                WHERE job_id = %s AND lane = %s
                """,
                (self.job_id, INDEX_LANE),
            )
            index_count = cur.fetchone()[0]
        self.assertIsNotNone(repair_row)
        self.assertEqual(repair_row[0], "extract")
        self.assertEqual(stream_row[0], REPAIR_STREAM_NAME)
        self.assertEqual(index_count, 0)


class IntegrationCheckEnrichWorker(unittest.TestCase):
    """N20 integration_check: evidence.v1 via cassette validates; invalid→repair NOT index."""

    def test_integration_check_enrich_worker(self) -> None:
        corpus = load_fixtures()
        page = dict(corpus.page_goldens["review_page.page.v1.json"])
        cassette = corpus.cassettes["enrich"][0]
        request = dict(cassette["request"])
        expected = dict(cassette["response"]["parsed"])

        gateway = LLMGateway(models_path=MODELS_PATH, mode=GatewayMode.REPLAY)
        execution = enrich_page(page, gateway=gateway, replay_request=request)
        self.assertEqual(execution.verdict, "pass")
        self.assertTrue(execution.replayed)

        evidence = finalize_evidence(
            execution.output,
            page,
            config_hash=CONFIG_HASH,
            created_at=CREATED_AT,
            model_id=CASSETTE_MODEL_ID,
            enrich_task_id="tsk_gate3_enrich",
        )
        validate_evidence_v1(evidence)
        self.assertEqual(evidence["record_id"], expected["record_id"])
        self.assertIn(page["page_id"], evidence["derived_from"])

        with patch.object(httpx.Client, "post", side_effect=AssertionError("live LLM call attempted")):
            with self.assertRaises(CassetteNotFoundError):
                enrich_page(
                    page,
                    gateway=gateway,
                    replay_request={
                        "page_id": "pg_missing",
                        "prompt_version": PROMPT_VERSION,
                        "model_id": CASSETTE_MODEL_ID,
                    },
                )

        def always_fail(_output: dict, _packed: dict) -> VerifierResult:
            return VerifierResult(passed=False, violations=("integration forced failure",))

        ceiling = enrich_page(
            page,
            gateway=gateway,
            replay_request=request,
            verifier=always_fail,
        )
        self.assertEqual(ceiling.verdict, "ceiling")
        self.assertEqual(ceiling.attempts, 2)


if __name__ == "__main__":
    unittest.main()
