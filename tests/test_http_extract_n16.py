"""N16 http+extract — fixture URL fetch → page.v1 → lineage edge."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import MagicMock

import httpx
import psycopg

from db.repositories.connection import connect
from db.repositories.migrate import apply_migrations
from fixtures.load import FIXTURES_ROOT
from workers.extract_worker import (
    EXTRACT_CODE_VERSION,
    extract_page_from_fetch,
    persist_page_v1,
    run_fetch_and_extract,
    validate_page_v1,
)
from workers.http_worker import (
    fetch_fixture_url,
    fetch_url,
    fetch_url_live,
    fixture_path_for_url,
    fixture_source_for_url,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
ENV_FILE = REPO_ROOT / ".env"
CONFIG_HASH = "sha256:" + ("b" * 64)
CREATED_AT = "2026-07-21T12:00:00Z"
FETCHED_AT = "2026-07-21T12:05:00Z"
ARTICLE_URL = "https://trailgearlab.example/articles/portable-camping-fans"
ARTICLE_GOLDEN = FIXTURES_ROOT / "pages" / "golden" / "article.page.v1.json"

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


def _insert_fetch_task(
    conn: psycopg.Connection,
    *,
    task_id: str,
    job_id: str,
    url: str,
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
        VALUES (%s, %s, 'discovered_url', 'http', 'READY', %s, %s, %s::jsonb)
        ON CONFLICT (task_id) DO NOTHING
        """,
        (
            task_id,
            job_id,
            f"sha256:{task_id}",
            url,
            json.dumps(provenance),
        ),
    )


class HttpWorkerFixtureTests(unittest.TestCase):
    def test_fixture_path_for_article_url(self) -> None:
        path = fixture_path_for_url(ARTICLE_URL)
        self.assertIsNotNone(path)
        assert path is not None
        self.assertEqual(path.name, "article.html")
        self.assertTrue(path.is_file())

    def test_fetch_fixture_url_returns_html(self) -> None:
        result = fetch_fixture_url(ARTICLE_URL)
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.media_type, "text/html")
        self.assertTrue(result.body.startswith(b"<!DOCTYPE html>"))
        self.assertTrue(result.source.startswith("fixture:"))
        self.assertEqual(result.fixture_source.page_id, "pg_camping_article")

    def test_fetch_url_prefers_offline_fixture(self) -> None:
        result = fetch_url(ARTICLE_URL)
        self.assertTrue(result.source.startswith("fixture:"))


class ExtractWorkerUnitTests(unittest.TestCase):
    def test_extract_article_fixture_validates_against_schema(self) -> None:
        fetch = fetch_fixture_url(ARTICLE_URL)
        page = extract_page_from_fetch(
            fetch,
            fetch_task_id="tsk_n16_article_fetch",
            config_hash=CONFIG_HASH,
            created_at=CREATED_AT,
            fetched_at=FETCHED_AT,
        )
        validate_page_v1(page)
        golden = json.loads(ARTICLE_GOLDEN.read_text(encoding="utf-8"))
        self.assertEqual(page["page_id"], golden["page_id"])
        self.assertEqual(page["url"], ARTICLE_URL)
        self.assertEqual(page["domain"], golden["domain"])
        self.assertEqual(page["title"], golden["title"])
        self.assertEqual(page["author"], golden["author"])
        self.assertEqual(page["platform_fields"]["source_class"], "article")
        self.assertEqual(page["derived_from"], ["tsk_n16_article_fetch"])
        self.assertEqual(page["provenance"]["code_version"], EXTRACT_CODE_VERSION)

    def test_live_fetch_path_uses_mocked_http(self) -> None:
        live_url = "https://live.example/articles/portable-camping-fans"
        html = (FIXTURES_ROOT / "pages" / "article.html").read_bytes()
        mock_response = MagicMock()
        mock_response.url = live_url
        mock_response.content = html
        mock_response.headers = {"content-type": "text/html; charset=utf-8"}
        mock_client = MagicMock(spec=httpx.Client)
        mock_client.get.return_value = mock_response

        result = fetch_url_live(live_url, client=mock_client)
        page = extract_page_from_fetch(
            result,
            fetch_task_id="tsk_n16_live_fetch",
            config_hash=CONFIG_HASH,
            created_at=CREATED_AT,
            fetched_at=FETCHED_AT,
        )
        validate_page_v1(page)
        self.assertEqual(page["platform_fields"], {})
        self.assertEqual(result.url, live_url)
        mock_client.get.assert_called_once()


@unittest.skipUnless(_postgres_available(), "Postgres unavailable (need .env POSTGRES_*)")
class HttpExtractPersistenceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.conn = connect()
        apply_migrations(cls.conn)
        cls.storage_root = Path(tempfile.mkdtemp(prefix="n16_storage_"))

    @classmethod
    def tearDownClass(cls) -> None:
        cls.conn.close()

    def setUp(self) -> None:
        self.conn.execute("SAVEPOINT n16_test_case")
        self.job_id = f"job_n16_{uuid.uuid4().hex[:12]}"
        self.fetch_task_id = f"tsk_n16_{uuid.uuid4().hex[:12]}"

    def tearDown(self) -> None:
        self.conn.execute("ROLLBACK TO SAVEPOINT n16_test_case")

    def test_run_fetch_and_extract_persists_page_and_lineage(self) -> None:
        _insert_job(self.conn, self.job_id)
        _insert_fetch_task(
            self.conn,
            task_id=self.fetch_task_id,
            job_id=self.job_id,
            url=ARTICLE_URL,
        )
        result = run_fetch_and_extract(
            self.conn,
            fetch_task_id=self.fetch_task_id,
            url=ARTICLE_URL,
            config_hash=CONFIG_HASH,
            created_at=CREATED_AT,
            fetched_at=FETCHED_AT,
            storage_root=self.storage_root,
        )
        self.assertTrue(result.artifact_inserted)
        self.assertTrue(result.lineage_edge_inserted)
        self.assertTrue(result.fetch_source.startswith("fixture:"))
        validate_page_v1(result.page)

        with self.conn.cursor() as cur:
            cur.execute(
                """
                SELECT artifact_kind, content_hash, derived_from
                FROM artifacts
                WHERE artifact_id = %s
                """,
                (result.artifact_id,),
            )
            artifact = cur.fetchone()
            cur.execute(
                """
                SELECT relation, version_tag
                FROM lineage_edges
                WHERE child_kind = 'page.v1'
                  AND child_id = %s
                  AND parent_kind = 'task'
                  AND parent_id = %s
                """,
                (result.page["page_id"], self.fetch_task_id),
            )
            edge = cur.fetchone()
        self.assertIsNotNone(artifact)
        self.assertEqual(artifact[0], "page.v1")
        self.assertEqual(artifact[2], [self.fetch_task_id])
        self.assertIsNotNone(edge)
        self.assertEqual(edge[0], "derived_from")
        self.assertEqual(edge[1], EXTRACT_CODE_VERSION)

    def test_persist_is_idempotent_for_same_page(self) -> None:
        _insert_job(self.conn, self.job_id)
        _insert_fetch_task(
            self.conn,
            task_id=self.fetch_task_id,
            job_id=self.job_id,
            url=ARTICLE_URL,
        )
        first = run_fetch_and_extract(
            self.conn,
            fetch_task_id=self.fetch_task_id,
            url=ARTICLE_URL,
            config_hash=CONFIG_HASH,
            created_at=CREATED_AT,
            fetched_at=FETCHED_AT,
            storage_root=self.storage_root,
        )
        second = run_fetch_and_extract(
            self.conn,
            fetch_task_id=self.fetch_task_id,
            url=ARTICLE_URL,
            config_hash=CONFIG_HASH,
            created_at=CREATED_AT,
            fetched_at=FETCHED_AT,
            storage_root=self.storage_root,
        )
        self.assertEqual(first.page["page_id"], second.page["page_id"])
        self.assertFalse(second.artifact_inserted)
        self.assertFalse(second.lineage_edge_inserted)
        with self.conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM artifacts WHERE artifact_id = %s",
                (first.artifact_id,),
            )
            artifact_count = cur.fetchone()[0]
            cur.execute(
                """
                SELECT COUNT(*)
                FROM lineage_edges
                WHERE child_id = %s AND parent_id = %s
                """,
                (first.page["page_id"], self.fetch_task_id),
            )
            edge_count = cur.fetchone()[0]
        self.assertEqual(artifact_count, 1)
        self.assertEqual(edge_count, 1)


class IntegrationCheckHttpExtract(unittest.TestCase):
    """Offline integration check for N16 gate-1 slice."""

    def test_fixture_url_maps_to_page_v1_and_lineage_contract(self) -> None:
        source = fixture_source_for_url(ARTICLE_URL)
        self.assertIsNotNone(source)
        fetch = fetch_fixture_url(ARTICLE_URL)
        page = extract_page_from_fetch(
            fetch,
            fetch_task_id="tsk_gate1_article_fetch",
            config_hash=CONFIG_HASH,
            created_at=CREATED_AT,
            fetched_at=FETCHED_AT,
        )
        validate_page_v1(page)
        self.assertEqual(page["page_id"], "pg_camping_article")
        self.assertEqual(page["derived_from"], ["tsk_gate1_article_fetch"])


if __name__ == "__main__":
    unittest.main()
