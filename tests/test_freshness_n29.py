"""N29 freshness — expiry drops signals from scoring and triggers re-collection."""

from __future__ import annotations

import ast
import json
import os
import unittest
import uuid
from pathlib import Path

import psycopg

from db.repositories.connection import connect
from db.repositories.migrate import apply_migrations
from fixtures.load import CAMPING_EXPECTED_SCORE, load_fixtures
from signal_engine.freshness import (
    CODE_VERSION,
    DEFAULT_OPPORTUNITY_TTL_DAYS,
    apply_opportunity_expiry,
    compute_expires_at,
    evaluate_freshness,
    filter_active_signals,
    is_opportunity_stale,
    is_signal_expired,
    load_half_life_days,
    score_active_signals,
    trigger_recollection_for_expiry,
)
from signal_engine.score import build_opportunity_v1, load_weights, score, validate_opportunity_v1
from workers.search_worker import QuerySpecRecord, insert_query_spec, make_query_spec_id

REPO_ROOT = Path(__file__).resolve().parents[1]
AS_OF = "2026-07-21T12:00:00Z"
CONTENT_EXPIRED_AS_OF = "2026-08-12T12:00:00Z"
CONFIG_HASH = "sha256:" + ("c" * 64)
CREATED_AT = "2026-07-21T14:00:00Z"
ENV_FILE = REPO_ROOT / ".env"

FRESHNESS_FORBIDDEN_IMPORT_PREFIXES = (
    "harness.gateway",
    "harness.litellm_adapter",
    "niche_research.gateway",
    "litellm",
    "openai",
    "anthropic",
    "requests",
    "httpx",
    "curl_cffi",
)

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


def assert_freshness_import_purity(source: str, *, path: Path) -> None:
    """Freshness is deterministic — must not import LLM or network clients."""
    tree = ast.parse(source, filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                _reject_freshness_import(alias.name, path)
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if module:
                _reject_freshness_import(module, path)
            for alias in node.names:
                full = f"{module}.{alias.name}" if module else alias.name
                _reject_freshness_import(full, path)


def _reject_freshness_import(module_name: str, path: Path) -> None:
    for prefix in FRESHNESS_FORBIDDEN_IMPORT_PREFIXES:
        if module_name == prefix or module_name.startswith(f"{prefix}."):
            raise AssertionError(
                f"{path}: freshness module must not import `{module_name}`",
            )


class HalfLifeTests(unittest.TestCase):
    def test_half_life_days_match_weights_yaml(self) -> None:
        weights = load_weights()
        half_life = load_half_life_days()
        self.assertEqual(half_life, weights["half_life_days"])

    def test_compute_expires_at_uses_half_life(self) -> None:
        expires_at = compute_expires_at(AS_OF, "content")
        self.assertEqual(expires_at, "2026-08-11T12:00:00Z")


class SignalExpiryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.corpus = load_fixtures()
        self.signals = self.corpus.camping_signals

    def test_all_camping_signals_fresh_at_as_of(self) -> None:
        evaluation = evaluate_freshness(self.signals, as_of=AS_OF)
        self.assertEqual(len(evaluation.active_signals), 5)
        self.assertEqual(len(evaluation.expired_signals), 0)
        self.assertFalse(evaluation.needs_recollection)

    def test_content_signal_expires_first(self) -> None:
        evaluation = evaluate_freshness(self.signals, as_of=CONTENT_EXPIRED_AS_OF)
        self.assertEqual(evaluation.expired_signal_ids, ("sig_camping_content",))
        self.assertTrue(evaluation.needs_recollection)
        self.assertEqual(len(evaluation.active_signals), 4)

    def test_filter_active_signals_excludes_expired(self) -> None:
        active = filter_active_signals(self.signals, as_of=CONTENT_EXPIRED_AS_OF)
        active_ids = {item["signal_id"] for item in active}
        self.assertNotIn("sig_camping_content", active_ids)
        self.assertIn("sig_camping_demand", active_ids)

    def test_is_signal_expired_boundary(self) -> None:
        content = next(
            item for item in self.signals["signals"] if item["signal_id"] == "sig_camping_content"
        )
        self.assertFalse(is_signal_expired(content, as_of="2026-08-11T11:59:59Z"))
        self.assertTrue(is_signal_expired(content, as_of="2026-08-11T12:00:00Z"))


class ScoringFreshnessTests(unittest.TestCase):
    def test_active_signals_score_matches_golden_before_expiry(self) -> None:
        corpus = load_fixtures()
        weights = load_weights()
        result, evaluation = score_active_signals(corpus.camping_signals, weights, as_of=AS_OF)
        self.assertFalse(evaluation.needs_recollection)
        self.assertEqual(result.score, CAMPING_EXPECTED_SCORE)


class OpportunityExpiryTests(unittest.TestCase):
    def test_opportunity_not_stale_before_ttl_without_signal_expiry(self) -> None:
        corpus = load_fixtures()
        weights = load_weights()
        expected = corpus.camping_expected
        result = score(corpus.camping_signals, weights, as_of=AS_OF)
        opportunity = build_opportunity_v1(
            opportunity_id=str(expected["opportunity_id"]),
            niche_id="camping-fixture",
            candidate=dict(expected["candidate"]),
            score_result=result,
            weights=weights,
            config_hash=CONFIG_HASH,
            as_of=AS_OF,
            generating_queries=list(expected["generating_queries"]),
            created_at=str(expected["provenance"]["created_at"]),
        )
        self.assertFalse(
            is_opportunity_stale(
                opportunity,
                as_of=AS_OF,
                signals=corpus.camping_signals,
            ),
        )

    def test_opportunity_marked_expired_when_scored_from_signal_expires(self) -> None:
        corpus = load_fixtures()
        weights = load_weights()
        expected = corpus.camping_expected
        result = score(corpus.camping_signals, weights, as_of=AS_OF)
        opportunity = build_opportunity_v1(
            opportunity_id=str(expected["opportunity_id"]),
            niche_id="camping-fixture",
            candidate=dict(expected["candidate"]),
            score_result=result,
            weights=weights,
            config_hash=CONFIG_HASH,
            as_of=AS_OF,
            generating_queries=list(expected["generating_queries"]),
            created_at=str(expected["provenance"]["created_at"]),
        )
        expired = apply_opportunity_expiry(
            opportunity,
            as_of=CONTENT_EXPIRED_AS_OF,
            signals=corpus.camping_signals,
        )
        self.assertEqual(expired.get("status"), "EXPIRED")
        validate_opportunity_v1({**expired, "status": "EXPIRED"})


@unittest.skipUnless(_postgres_available(), "Postgres unavailable (need .env POSTGRES_*)")
class RecollectionPostgresTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        _load_dotenv()
        cls.conn = connect()
        apply_migrations(cls.conn)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.conn.close()

    def setUp(self) -> None:
        self.conn.execute("SAVEPOINT n29_test_case")
        self.job_id = f"job_n29_{uuid.uuid4().hex[:8]}"
        self.query_spec_id = make_query_spec_id(self.job_id, "portable camping fan")
        provenance = {
            "schema_version": "job.v1",
            "config_hash": CONFIG_HASH,
            "created_at": CREATED_AT,
        }
        self.conn.execute(
            """
            INSERT INTO research_jobs (
                job_id, job_kind, status, config_hash, budget, provenance, niche_id
            )
            VALUES (%s, %s, %s, %s, %s::jsonb, %s::jsonb, %s)
            """,
            (
                self.job_id,
                "dossier",
                "ACQUIRING",
                CONFIG_HASH,
                json.dumps(BUDGET),
                json.dumps(provenance),
                "camping-fixture",
            ),
        )
        insert_query_spec(
            self.conn,
            QuerySpecRecord(
                query_spec_id=self.query_spec_id,
                job_id=self.job_id,
                text="portable camping fan",
                engine="searxng",
                params={"source": "fixture"},
            ),
        )

    def tearDown(self) -> None:
        self.conn.execute("ROLLBACK TO SAVEPOINT n29_test_case")

    def _fetch_task_state(self, task_id: str) -> str:
        row = self.conn.execute(
            "SELECT state FROM tasks WHERE task_id = %s",
            (task_id,),
        ).fetchone()
        assert row is not None
        return str(row[0])

    def test_expiry_enqueues_and_admits_recollection_task(self) -> None:
        corpus = load_fixtures()
        evaluation = evaluate_freshness(corpus.camping_signals, as_of=CONTENT_EXPIRED_AS_OF)
        self.assertTrue(evaluation.needs_recollection)

        results = trigger_recollection_for_expiry(
            self.conn,
            evaluation=evaluation,
            job_id=self.job_id,
            generating_queries=[self.query_spec_id],
            config_hash=CONFIG_HASH,
            created_at=CREATED_AT,
        )
        self.assertEqual(len(results), 1)
        result = results[0]
        self.assertTrue(result.admitted)
        self.assertEqual(self._fetch_task_state(result.task_id), "READY")

        edge_count = self.conn.execute(
            """
            SELECT COUNT(*)
            FROM lineage_edges
            WHERE child_kind = 'task'
              AND child_id = %s
              AND relation = 'recollect_for'
            """,
            (result.task_id,),
        ).fetchone()
        assert edge_count is not None
        self.assertGreaterEqual(edge_count[0], 2)

    def test_recollection_is_idempotent(self) -> None:
        corpus = load_fixtures()
        evaluation = evaluate_freshness(corpus.camping_signals, as_of=CONTENT_EXPIRED_AS_OF)
        first = trigger_recollection_for_expiry(
            self.conn,
            evaluation=evaluation,
            job_id=self.job_id,
            generating_queries=[self.query_spec_id],
            config_hash=CONFIG_HASH,
            created_at=CREATED_AT,
        )
        second = trigger_recollection_for_expiry(
            self.conn,
            evaluation=evaluation,
            job_id=self.job_id,
            generating_queries=[self.query_spec_id],
            config_hash=CONFIG_HASH,
            created_at=CREATED_AT,
        )
        self.assertEqual(first[0].task_id, second[0].task_id)
        self.assertFalse(second[0].admitted)
        self.assertEqual(second[0].admission_reason, "duplicate_idempotency_key")


class ImportPurityTests(unittest.TestCase):
    def test_freshness_module_is_import_pure(self) -> None:
        source = (REPO_ROOT / "signal_engine" / "freshness.py").read_text(encoding="utf-8")
        assert_freshness_import_purity(source, path=Path("signal_engine/freshness.py"))

    def test_freshness_import_purity_rejects_forbidden_clients(self) -> None:
        poison = "from harness.gateway import LLMGateway\n"
        with self.assertRaises(AssertionError):
            assert_freshness_import_purity(poison, path=Path("signal_engine/freshness.py"))


class IntegrationCheckFreshness(unittest.TestCase):
    """N29 integration_check: expiry → re-collection."""

    def test_integration_check_freshness_offline(self) -> None:
        corpus = load_fixtures()
        weights = load_weights()
        expected = corpus.camping_expected

        fresh = evaluate_freshness(corpus.camping_signals, as_of=AS_OF)
        self.assertFalse(fresh.needs_recollection)
        active_score, _ = score_active_signals(corpus.camping_signals, weights, as_of=AS_OF)
        self.assertEqual(active_score.score, CAMPING_EXPECTED_SCORE)

        expired = evaluate_freshness(corpus.camping_signals, as_of=CONTENT_EXPIRED_AS_OF)
        self.assertTrue(expired.needs_recollection)
        self.assertIn("sig_camping_content", expired.expired_signal_ids)

        baseline = score(corpus.camping_signals, weights, as_of=AS_OF)
        opportunity = build_opportunity_v1(
            opportunity_id=str(expected["opportunity_id"]),
            niche_id="camping-fixture",
            candidate=dict(expected["candidate"]),
            score_result=baseline,
            weights=weights,
            config_hash=CONFIG_HASH,
            as_of=AS_OF,
            generating_queries=list(expected["generating_queries"]),
            created_at=str(expected["provenance"]["created_at"]),
        )
        marked = apply_opportunity_expiry(
            opportunity,
            as_of=CONTENT_EXPIRED_AS_OF,
            signals=corpus.camping_signals,
        )
        self.assertEqual(marked["status"], "EXPIRED")
        self.assertEqual(load_half_life_days()["content"], 21)
        self.assertEqual(DEFAULT_OPPORTUNITY_TTL_DAYS, 14)
        self.assertEqual(CODE_VERSION, "freshness-1.0.0")

        freshness_source = (REPO_ROOT / "signal_engine" / "freshness.py").read_text(
            encoding="utf-8",
        )
        assert_freshness_import_purity(freshness_source, path=Path("signal_engine/freshness.py"))

    @unittest.skipUnless(_postgres_available(), "Postgres unavailable (need .env POSTGRES_*)")
    def test_integration_check_freshness_postgres(self) -> None:
        suite = unittest.TestSuite()
        suite.addTest(
            RecollectionPostgresTests("test_expiry_enqueues_and_admits_recollection_task"),
        )
        result = unittest.TextTestRunner(verbosity=0).run(suite)
        self.assertTrue(result.wasSuccessful())


if __name__ == "__main__":
    unittest.main()
