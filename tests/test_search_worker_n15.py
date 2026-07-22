"""N15 search_worker — SearXNG fixture/live → query_specs + discovered URLs."""

from __future__ import annotations

import json
import os
import unittest
import uuid
from pathlib import Path
from unittest.mock import MagicMock

import httpx
import psycopg

from db.repositories.connection import connect
from db.repositories.migrate import apply_migrations
from fixtures.load import FIXTURES_ROOT
from workers.search_worker import (
    DEFAULT_SEARXNG_URL,
    fixture_path_for_query,
    load_searxng_fixture,
    make_query_spec_id,
    parse_searxng_response,
    query_searxng_live,
    run_search_from_fixture,
    run_search_live,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
ENV_FILE = REPO_ROOT / ".env"
CONFIG_HASH = "sha256:" + ("b" * 64)
CREATED_AT = "2026-07-21T12:00:00Z"
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


def _sample_job(job_id: str) -> tuple:
    provenance = {
        "schema_version": "job.v1",
        "config_hash": CONFIG_HASH,
        "created_at": CREATED_AT,
    }
    return (
        job_id,
        "dossier",
        "DISCOVERING",
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


class SearchWorkerParseTests(unittest.TestCase):
    def test_fixture_path_for_query(self) -> None:
        path = fixture_path_for_query("portable camping fan")
        self.assertEqual(path.name, "searxng_portable_camping_fan.json")
        self.assertTrue(path.is_file())

    def test_parse_camping_fixture_urls(self) -> None:
        payload = load_searxng_fixture(CAMPING_FIXTURE)
        query, urls = parse_searxng_response(payload)
        self.assertEqual(query, "portable camping fan")
        self.assertEqual(len(urls), 4)
        self.assertEqual(urls[0].url, "https://trailgearlab.example/articles/portable-camping-fans")
        self.assertEqual(urls[3].engine, "youtube")

    def test_query_spec_id_is_deterministic(self) -> None:
        first = make_query_spec_id("job_test", "portable camping fan")
        second = make_query_spec_id("job_test", "portable camping fan")
        self.assertEqual(first, second)
        self.assertTrue(first.startswith("qs_"))


class SearchWorkerLiveClientTests(unittest.TestCase):
    def test_live_query_uses_searxng_json_endpoint(self) -> None:
        fixture_payload = load_searxng_fixture(CAMPING_FIXTURE)
        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()
        mock_response.json.return_value = fixture_payload
        mock_client = MagicMock(spec=httpx.Client)
        mock_client.get.return_value = mock_response

        payload = query_searxng_live(
            "portable camping fan",
            base_url=DEFAULT_SEARXNG_URL,
            client=mock_client,
        )

        mock_client.get.assert_called_once()
        called_url = mock_client.get.call_args.args[0]
        self.assertIn("/search?", called_url)
        self.assertIn("format=json", called_url)
        self.assertEqual(payload["query"], "portable camping fan")
        _, urls = parse_searxng_response(payload)
        self.assertEqual(len(urls), 4)


@unittest.skipUnless(_postgres_available(), "Postgres unavailable (need .env POSTGRES_*)")
class SearchWorkerPersistenceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.conn = connect()
        apply_migrations(cls.conn)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.conn.close()

    def setUp(self) -> None:
        self.conn.execute("SAVEPOINT n15_test_case")
        self.job_id = f"job_n15_{uuid.uuid4().hex[:12]}"

    def tearDown(self) -> None:
        self.conn.execute("ROLLBACK TO SAVEPOINT n15_test_case")

    def test_fixture_persists_query_specs_and_urls(self) -> None:
        _insert_job(self.conn, self.job_id)
        result = run_search_from_fixture(
            self.conn,
            job_id=self.job_id,
            config_hash=CONFIG_HASH,
            created_at=CREATED_AT,
            fixture_path=CAMPING_FIXTURE,
            enqueue_fetch=False,
        )

        self.assertEqual(result.query_spec.text, "portable camping fan")
        self.assertEqual(len(result.urls), 4)
        self.assertEqual(len(result.fetch_task_ids), 4)
        self.assertTrue(result.source.startswith("fixture:"))

        with self.conn.cursor() as cur:
            cur.execute(
                """
                SELECT text, engine, params
                FROM query_specs
                WHERE query_spec_id = %s
                """,
                (result.query_spec.query_spec_id,),
            )
            row = cur.fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row[0], "portable camping fan")
        self.assertEqual(row[1], "searxng")
        params = row[2]
        self.assertEqual(params["source"], "fixture")
        self.assertEqual(params["fixture_file"], CAMPING_FIXTURE.name)

        with self.conn.cursor() as cur:
            cur.execute(
                """
                SELECT COUNT(*)
                FROM tasks
                WHERE job_id = %s AND task_kind = 'discovered_url'
                """,
                (self.job_id,),
            )
            task_count = cur.fetchone()[0]
            cur.execute(
                """
                SELECT COUNT(*)
                FROM lineage_edges
                WHERE parent_kind = 'query_spec'
                  AND parent_id = %s
                  AND relation = 'discovered_from'
                """,
                (result.query_spec.query_spec_id,),
            )
            edge_count = cur.fetchone()[0]
        self.assertEqual(task_count, 4)
        self.assertEqual(edge_count, 4)

    def test_fixture_search_is_idempotent(self) -> None:
        _insert_job(self.conn, self.job_id)
        first = run_search_from_fixture(
            self.conn,
            job_id=self.job_id,
            config_hash=CONFIG_HASH,
            created_at=CREATED_AT,
            fixture_path=CAMPING_FIXTURE,
            enqueue_fetch=False,
        )
        second = run_search_from_fixture(
            self.conn,
            job_id=self.job_id,
            config_hash=CONFIG_HASH,
            created_at=CREATED_AT,
            fixture_path=CAMPING_FIXTURE,
            enqueue_fetch=False,
        )
        self.assertEqual(first.query_spec.query_spec_id, second.query_spec.query_spec_id)
        with self.conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM query_specs WHERE job_id = %s",
                (self.job_id,),
            )
            spec_count = cur.fetchone()[0]
            cur.execute(
                "SELECT COUNT(*) FROM tasks WHERE job_id = %s",
                (self.job_id,),
            )
            task_count = cur.fetchone()[0]
        self.assertEqual(spec_count, 1)
        self.assertEqual(task_count, 4)

    def test_live_path_persists_with_mocked_http(self) -> None:
        _insert_job(self.conn, self.job_id)
        fixture_payload = load_searxng_fixture(CAMPING_FIXTURE)
        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()
        mock_response.json.return_value = fixture_payload
        mock_client = MagicMock(spec=httpx.Client)
        mock_client.get.return_value = mock_response

        result = run_search_live(
            self.conn,
            job_id=self.job_id,
            query="portable camping fan",
            config_hash=CONFIG_HASH,
            created_at=CREATED_AT,
            client=mock_client,
            enqueue_fetch=False,
        )
        self.assertEqual(result.query_spec.params["source"], "live")
        self.assertEqual(len(result.urls), 4)


class IntegrationCheckSearchWorker(unittest.TestCase):
    """Offline + Postgres integration check for N15 searxng-fixture verifier."""

    def test_searxng_fixture_maps_to_query_spec_and_urls(self) -> None:
        payload = load_searxng_fixture(CAMPING_FIXTURE)
        query, urls = parse_searxng_response(payload)
        self.assertEqual(query, "portable camping fan")
        self.assertEqual(len(urls), 4)
        spec_id = make_query_spec_id("job_gate1_fixture", query)
        self.assertTrue(spec_id.startswith("qs_"))


if __name__ == "__main__":
    unittest.main()
