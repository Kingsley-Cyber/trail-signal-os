"""N19 MCP server — create_job/status/bundle round-trip and idempotent dup-message."""

from __future__ import annotations

import asyncio
import importlib.util
import os
import sys
import unittest
import uuid
from pathlib import Path
from typing import Any

import psycopg
from fastapi.testclient import TestClient

from control.api.app import create_app
from control.api.readiness import ReconcilerReadiness
from control.api.settings import ControlApiSettings
from db.repositories.connection import connect
from db.repositories.migrate import apply_migrations

REPO_ROOT = Path(__file__).resolve().parents[1]
SERVER_PATH = REPO_ROOT / "mcp" / "server.py"
TEST_TOKEN = "test-mcp-control-api-token"


def _load_mcp_server_module():
    spec = importlib.util.spec_from_file_location("trail_mcp_server", SERVER_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"unable to load MCP server module from {SERVER_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


_mcp = _load_mcp_server_module()
BUNDLE_DEFAULT_TOKENS = _mcp.BUNDLE_DEFAULT_TOKENS
ControlApiClient = _mcp.ControlApiClient
IdempotencyCache = _mcp.IdempotencyCache
MCP_PORT = _mcp.MCP_PORT
create_server = _mcp.create_server


def _load_dotenv() -> None:
    env_file = os.path.join(os.path.dirname(__file__), "..", ".env")
    if not os.path.isfile(env_file):
        return
    with open(env_file, encoding="utf-8") as handle:
        for line in handle:
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


def _build_test_stack() -> tuple[Any, ControlApiClient, IdempotencyCache]:
    readiness = ReconcilerReadiness()
    readiness.mark_ready()
    app = create_app(
        settings=_test_settings(),
        readiness=readiness,
        run_startup_reconciler=False,
    )
    test_client = TestClient(app)
    api_client = ControlApiClient(
        base_url="http://testserver",
        bearer_token=TEST_TOKEN,
        http_client=_TestClientAdapter(test_client),
    )
    cache = IdempotencyCache()
    server = create_server(api_client, idempotency_cache=cache)
    return server, api_client, cache


class _TestClientAdapter:
    """Adapt FastAPI TestClient to the httpx.Client interface used in production."""

    def __init__(self, test_client: TestClient) -> None:
        self._test_client = test_client

    def post(self, url: str, *, json: dict[str, Any], headers: dict[str, str]) -> _TestResponse:
        response = self._test_client.post(url, json=json, headers=headers)
        return _TestResponse(response)

    def get(self, url: str) -> _TestResponse:
        response = self._test_client.get(url)
        return _TestResponse(response)


class _TestResponse:
    def __init__(self, response: Any) -> None:
        self.status_code = response.status_code
        self._response = response

    @property
    def is_success(self) -> bool:
        return 200 <= self.status_code < 300

    def json(self) -> Any:
        return self._response.json()

    @property
    def text(self) -> str:
        return self._response.text


async def _call_tool(server: Any, name: str, arguments: dict[str, Any]) -> Any:
    result = await server.call_tool(name, arguments)
    if result.is_error:
        raise AssertionError(f"tool {name} failed: {result.content}")
    if result.structured_content is not None:
        if "result" in result.structured_content:
            return result.structured_content["result"]
        return result.structured_content
    if result.content:
        return result.content[0].text
    return None


class McpPortTests(unittest.TestCase):
    def test_mcp_port_is_8766(self) -> None:
        self.assertEqual(MCP_PORT, 8766)


class McpOfflineTests(unittest.TestCase):
    def test_duplicate_create_job_returns_cached_result(self) -> None:
        server, _, cache = _build_test_stack()
        job_id = f"job_mcp_offline_{uuid.uuid4().hex[:12]}"
        idempotency_key = f"idem_{uuid.uuid4().hex}"

        async def run() -> tuple[Any, Any]:
            first = await _call_tool(
                server,
                "research.create_job",
                {
                    "job_kind": "dossier",
                    "job_id": job_id,
                    "idempotency_key": idempotency_key,
                },
            )
            second = await _call_tool(
                server,
                "research.create_job",
                {
                    "job_kind": "dossier",
                    "job_id": job_id,
                    "idempotency_key": idempotency_key,
                },
            )
            return first, second

        first, second = asyncio.run(run())
        self.assertEqual(first, second)
        self.assertEqual(first["job_id"], job_id)
        self.assertIn(idempotency_key, cache._entries)

    def test_duplicate_bundle_returns_cached_result(self) -> None:
        server, _, _ = _build_test_stack()
        job_id = f"job_mcp_bundle_{uuid.uuid4().hex[:12]}"

        async def run() -> tuple[Any, Any]:
            await _call_tool(
                server,
                "research.create_job",
                {"job_kind": "collection", "job_id": job_id},
            )
            idempotency_key = f"bundle_{job_id}"
            first = await _call_tool(
                server,
                "evidence.bundle",
                {
                    "job_id": job_id,
                    "max_tokens": BUNDLE_DEFAULT_TOKENS,
                    "idempotency_key": idempotency_key,
                },
            )
            second = await _call_tool(
                server,
                "evidence.bundle",
                {
                    "job_id": job_id,
                    "max_tokens": BUNDLE_DEFAULT_TOKENS,
                    "idempotency_key": idempotency_key,
                },
            )
            return first, second

        first, second = asyncio.run(run())
        self.assertEqual(first, second)
        self.assertEqual(first["manifest"]["included"], 0)


@unittest.skipUnless(_postgres_available(), "Postgres unavailable (need .env POSTGRES_*)")
class McpRoundTripTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.conn = connect()
        apply_migrations(cls.conn)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.conn.close()

    def setUp(self) -> None:
        self.conn.execute("SAVEPOINT n19_mcp_case")

    def tearDown(self) -> None:
        self.conn.execute("ROLLBACK TO SAVEPOINT n19_mcp_case")

    def test_create_job_status_bundle_round_trip(self) -> None:
        server, _, _ = _build_test_stack()
        job_id = f"job_mcp_rt_{uuid.uuid4().hex[:12]}"

        async def run() -> tuple[Any, Any, Any]:
            created = await _call_tool(
                server,
                "research.create_job",
                {"job_kind": "dossier", "job_id": job_id},
            )
            status = await _call_tool(
                server,
                "research.status",
                {"job_id": job_id},
            )
            bundle = await _call_tool(
                server,
                "evidence.bundle",
                {"job_id": job_id},
            )
            return created, status, bundle

        created, status, bundle = asyncio.run(run())
        self.assertEqual(created["job_id"], job_id)
        self.assertEqual(created["status"], "CREATED")
        self.assertEqual(status["job_id"], job_id)
        self.assertEqual(status["status"], "CREATED")
        self.assertEqual(status["total_tasks"], 0)
        self.assertEqual(bundle["job_id"], job_id)
        self.assertEqual(bundle["records"], [])
        self.assertEqual(bundle["manifest"]["included"], 0)
        self.assertEqual(bundle["manifest"]["max_tokens"], BUNDLE_DEFAULT_TOKENS)

    def test_duplicate_message_creates_one_job(self) -> None:
        server, _, _ = _build_test_stack()
        job_id = f"job_mcp_dup_{uuid.uuid4().hex[:12]}"
        idempotency_key = f"dup_{uuid.uuid4().hex}"

        async def run() -> tuple[Any, Any]:
            first = await _call_tool(
                server,
                "research.create_job",
                {
                    "job_kind": "dossier",
                    "job_id": job_id,
                    "idempotency_key": idempotency_key,
                },
            )
            second = await _call_tool(
                server,
                "research.create_job",
                {
                    "job_kind": "dossier",
                    "job_id": job_id,
                    "idempotency_key": idempotency_key,
                },
            )
            return first, second

        first, second = asyncio.run(run())
        self.assertEqual(first["job_id"], job_id)
        self.assertEqual(second["job_id"], job_id)

        with self.conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM research_jobs WHERE job_id = %s",
                (job_id,),
            )
            count = cur.fetchone()[0]
        self.assertEqual(count, 1)

        readiness = ReconcilerReadiness()
        readiness.mark_ready()
        app = create_app(
            settings=_test_settings(),
            readiness=readiness,
            run_startup_reconciler=False,
        )
        with TestClient(app) as client:
            response = client.get(f"/v1/research-jobs/{job_id}")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["job_id"], job_id)


class IntegrationCheckMcp(unittest.TestCase):
    def test_offline_idempotency(self) -> None:
        suite = unittest.TestSuite()
        suite.addTest(McpPortTests("test_mcp_port_is_8766"))
        suite.addTest(McpOfflineTests("test_duplicate_create_job_returns_cached_result"))
        suite.addTest(McpOfflineTests("test_duplicate_bundle_returns_cached_result"))
        result = unittest.TextTestRunner(verbosity=0).run(suite)
        self.assertTrue(result.wasSuccessful())

    @unittest.skipUnless(_postgres_available(), "Postgres unavailable (need .env POSTGRES_*)")
    def test_round_trip_live(self) -> None:
        suite = unittest.TestSuite()
        suite.addTest(McpRoundTripTests("test_create_job_status_bundle_round_trip"))
        suite.addTest(McpRoundTripTests("test_duplicate_message_creates_one_job"))
        result = unittest.TextTestRunner(verbosity=0).run(suite)
        self.assertTrue(result.wasSuccessful())


if __name__ == "__main__":
    unittest.main()
