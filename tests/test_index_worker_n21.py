"""N21 index_worker — evidence.v1 → Qdrant with ts_ prefix; search returns (mocked offline)."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

import psycopg

from db.repositories.connection import connect
from db.repositories.migrate import apply_migrations
from db.repositories.persist_artifact import persist_artifact
from fixtures.load import load_fixtures
from workers.enrich_worker import finalize_evidence, validate_evidence_v1
from workers.index_worker import (
    COLLECTION_PREFIX,
    EVIDENCE_COLLECTION_SUFFIX,
    InMemoryQdrantClient,
    IndexResult,
    build_index_text,
    collection_name,
    deterministic_embed,
    evidence_collection_name,
    index_evidence_v1,
    load_evidence_v1,
    point_id_for_record,
    qdrant_url,
    run_index_task,
    search_evidence,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
ENV_FILE = REPO_ROOT / ".env"
CONFIG_HASH = "sha256:" + ("a" * 64)
CREATED_AT = "2026-07-21T12:00:00Z"


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


def _sample_evidence(page_id: str = "pg_camping_review") -> dict:
    corpus = load_fixtures()
    page = dict(corpus.page_goldens["review_page.page.v1.json"])
    if page_id != page["page_id"]:
        page["page_id"] = page_id
    raw = dict(corpus.cassettes["enrich"][0]["response"]["parsed"])
    return finalize_evidence(
        raw,
        page,
        config_hash=CONFIG_HASH,
        created_at=CREATED_AT,
        model_id="nomic-embed-text",
        enrich_task_id="tsk_enrich_fixture",
    )


class CollectionNamingTests(unittest.TestCase):
    def test_collection_name_uses_ts_prefix(self) -> None:
        self.assertEqual(collection_name("evidence"), "ts_evidence")
        self.assertEqual(evidence_collection_name(), "ts_evidence")
        self.assertTrue(evidence_collection_name().startswith(COLLECTION_PREFIX))

    def test_collection_name_idempotent_for_prefixed_input(self) -> None:
        self.assertEqual(collection_name("ts_evidence"), "ts_evidence")

    def test_point_id_is_stable_for_record(self) -> None:
        first = point_id_for_record("ev_camping_pain_1042")
        second = point_id_for_record("ev_camping_pain_1042")
        self.assertEqual(first, second)


class IndexTextTests(unittest.TestCase):
    def test_build_index_text_includes_observation_and_pain_points(self) -> None:
        evidence = _sample_evidence()
        text = build_index_text(evidence)
        self.assertIn(evidence["observation"], text)
        for pain_point in evidence["pain_points"]:
            self.assertIn(pain_point, text)


class MockedQdrantIndexTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = InMemoryQdrantClient()
        self.evidence = _sample_evidence()

    def test_index_and_search_returns_record(self) -> None:
        result = index_evidence_v1(
            self.evidence,
            client=self.client,
            embed_fn=deterministic_embed,
        )
        self.assertIsInstance(result, IndexResult)
        self.assertTrue(result.indexed)
        self.assertEqual(result.collection_name, evidence_collection_name())
        self.assertTrue(result.collection_name.startswith(COLLECTION_PREFIX))

        hits = search_evidence(
            self.evidence["observation"],
            client=self.client,
            embed_fn=deterministic_embed,
            limit=3,
        )
        self.assertGreaterEqual(len(hits), 1)
        self.assertEqual(hits[0].record_id, self.evidence["record_id"])
        self.assertIsInstance(hits[0].score, float)

    def test_search_empty_collection_returns_no_hits(self) -> None:
        hits = search_evidence(
            "nothing indexed yet",
            client=self.client,
            embed_fn=deterministic_embed,
            collection=collection_name("empty_probe"),
        )
        self.assertEqual(hits, [])


@unittest.skipUnless(_postgres_available(), "Postgres unavailable (need .env POSTGRES_*)")
class PersistedEvidenceIndexTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.conn = connect()
        apply_migrations(cls.conn)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.conn.close()

    def setUp(self) -> None:
        self.conn.execute("SAVEPOINT n21_test_case")
        self.client = InMemoryQdrantClient()
        self.evidence = _sample_evidence(page_id=f"pg_n21_{uuid.uuid4().hex[:8]}")
        self.job_id = f"job_n21_{uuid.uuid4().hex[:8]}"
        self.conn.execute(
            """
            INSERT INTO research_jobs (job_id, job_kind, status, config_hash, budget, provenance)
            VALUES (%s, 'dossier', 'INDEXING', %s, '{}'::jsonb, '{}'::jsonb)
            ON CONFLICT (job_id) DO NOTHING
            """,
            (self.job_id, CONFIG_HASH),
        )

    def tearDown(self) -> None:
        self.conn.execute("ROLLBACK TO SAVEPOINT n21_test_case")

    def test_run_index_task_loads_persisted_evidence(self) -> None:
        enrich_task_id = "tsk_enrich_n21"
        self.conn.execute(
            """
            INSERT INTO tasks (
                task_id, job_id, lane, state, idempotency_key, payload_ref, provenance
            )
            VALUES (%s, %s, 'enrich', 'SUCCEEDED', %s, %s, '{}'::jsonb)
            ON CONFLICT (idempotency_key) DO NOTHING
            """,
            (
                enrich_task_id,
                self.job_id,
                f"sha256:{uuid.uuid4().hex}",
                json.dumps({"page_id": self.evidence["derived_from"][0]}),
            ),
        )
        with tempfile.TemporaryDirectory() as tmp:
            storage_root = Path(tmp)
            persist_artifact(
                self.conn,
                artifact_id=self.evidence["record_id"],
                content_hash=self.evidence["content_hash"],
                artifact_kind="evidence.v1",
                payload=self.evidence,
                derived_from=list(self.evidence["derived_from"]),
                provenance=self.evidence["provenance"],
                created_by_task=enrich_task_id,
                schema_version="evidence.v1",
                storage_root=storage_root,
            )
            loaded = load_evidence_v1(
                self.conn,
                self.evidence["record_id"],
                storage_root=storage_root,
            )
            validate_evidence_v1(loaded)

            result = run_index_task(
                self.conn,
                record_id=self.evidence["record_id"],
                index_task_id="tsk_index_n21",
                client=self.client,
                embed_fn=deterministic_embed,
                storage_root=storage_root,
            )
            self.assertEqual(result.record_id, self.evidence["record_id"])

            hits = search_evidence(
                self.evidence["observation"],
                client=self.client,
                embed_fn=deterministic_embed,
            )
            self.assertEqual(hits[0].record_id, self.evidence["record_id"])


class IntegrationCheckIndexWorker(unittest.TestCase):
    """N21 integration_check: Qdrant search returns (ts_ prefix per environment_profile)."""

    def test_integration_check_index_worker(self) -> None:
        client = InMemoryQdrantClient()
        evidence = _sample_evidence()
        collection = evidence_collection_name()
        self.assertTrue(collection.startswith(COLLECTION_PREFIX))
        self.assertIn(EVIDENCE_COLLECTION_SUFFIX, collection)

        index_evidence_v1(evidence, client=client, embed_fn=deterministic_embed)
        hits = search_evidence(
            "motor noise inside the tent",
            client=client,
            embed_fn=deterministic_embed,
            collection=collection,
        )
        self.assertGreaterEqual(len(hits), 1)
        self.assertEqual(hits[0].record_id, evidence["record_id"])
        self.assertEqual(hits[0].payload.get("schema_version"), "evidence.v1")

        with patch("workers.index_worker.create_qdrant_client") as create_client:
            create_client.side_effect = AssertionError("live Qdrant must not be required offline")
            offline_hits = search_evidence(
                evidence["observation"],
                client=client,
                embed_fn=deterministic_embed,
            )
        self.assertEqual(offline_hits[0].record_id, evidence["record_id"])


@unittest.skipUnless(os.environ.get("QDRANT_URL"), "optional live Qdrant probe — set QDRANT_URL to run")
class LiveQdrantProbeTests(unittest.TestCase):
    def test_live_qdrant_round_trip_when_configured(self) -> None:
        from workers.index_worker import create_qdrant_client, index_evidence_v1, search_evidence

        client = create_qdrant_client(url=qdrant_url())
        evidence = _sample_evidence(page_id=f"pg_live_{uuid.uuid4().hex[:8]}")
        index_evidence_v1(evidence, client=client, embed_fn=deterministic_embed)
        hits = search_evidence(
            evidence["observation"],
            client=client,
            embed_fn=deterministic_embed,
        )
        self.assertGreaterEqual(len(hits), 1)


if __name__ == "__main__":
    unittest.main()
