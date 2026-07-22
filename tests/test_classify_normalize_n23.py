"""N23 classify+normalize — LAW 1 split; write-guard g5; normalize invariants g11."""

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
from guards.exceptions import GuardViolation
from guards.runtime_guards import guard11_assert_normalize_invariants
from guards.schema_guards import guard5_reject_llm_score_provenance
from guards.static_lint import lint_import_purity
from harness.gateway import GatewayMode, LLMGateway
from harness.litellm_adapter import CassetteNotFoundError
from lineage.edges import edges_for_child
from signal_engine.classify import (
    CASSETTE_MODEL_ID,
    ClassifyRepairRoute,
    ClassifySuccess,
    PROMPT_VERSION,
    REPAIR_STREAM_NAME,
    assert_law1_classify_output,
    build_replay_request,
    classify_evidence,
    finalize_signal_raw,
    load_classify_prompt,
    run_classify_normalize_task,
    validate_signal_raw,
)
from signal_engine.confidence import confidence_for_signal_raw
from signal_engine.normalize import (
    CODE_VERSION,
    apply_direction,
    assert_normalize_invariants,
    normalize_signal_raw,
    percentile_rank,
    validate_signal_v1,
    winsorize,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
MODELS_PATH = REPO_ROOT / "config" / "models.yaml"
ENV_FILE = REPO_ROOT / ".env"
CONFIG_HASH = "sha256:" + ("a" * 64)
CREATED_AT = "2026-07-21T12:00:00Z"
POISON_G05 = REPO_ROOT / "tests" / "fault_injection" / "poison" / "g05_opportunity_model_id.json"

PAIN_COHORT = [0.20, 0.30, 0.40, 0.50, 0.61, 0.65]


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


class PromptTests(unittest.TestCase):
    def test_prompt_file_loads_and_forbids_scoring(self) -> None:
        prompt = load_classify_prompt()
        self.assertIn("evidence.v1", prompt)
        self.assertIn("Do **not** normalize", prompt)
        self.assertIn("normalized_score", prompt)


class ReplayRequestTests(unittest.TestCase):
    def setUp(self) -> None:
        corpus = load_fixtures()
        self.evidence = dict(corpus.cassettes["enrich"][0]["response"]["parsed"])
        self.cassette_request = dict(corpus.cassettes["classify"][0]["request"])

    def test_build_replay_request_matches_classify_cassette(self) -> None:
        request = build_replay_request(self.evidence)
        self.assertEqual(request["record_id"], self.cassette_request["record_id"])
        self.assertEqual(request["prompt_version"], PROMPT_VERSION)
        self.assertEqual(request["model_id"], CASSETTE_MODEL_ID)


class NormalizeUnitTests(unittest.TestCase):
    def test_winsorize_clips_outliers(self) -> None:
        cohort = [1.0, 2.0, 3.0, 4.0, 100.0]
        self.assertEqual(winsorize(100.0, cohort), 4.0)

    def test_percentile_rank_matches_doc_example_cohort(self) -> None:
        self.assertAlmostEqual(percentile_rank(PAIN_COHORT, 0.61), 0.8)

    def test_competition_direction_inverts(self) -> None:
        inverted, applied = apply_direction(0.4, signal_type="competition", metric_name="listing_count")
        self.assertTrue(applied)
        self.assertAlmostEqual(inverted, 0.6)

    def test_normalize_signal_raw_emits_valid_signal_v1(self) -> None:
        corpus = load_fixtures()
        signal_raw = dict(corpus.cassettes["classify"][0]["response"]["parsed"])
        signal = normalize_signal_raw(
            signal_raw,
            cohort_raw_values=PAIN_COHORT,
            config_hash=CONFIG_HASH,
            created_at=CREATED_AT,
        )
        validate_signal_v1(signal)
        assert_normalize_invariants(signal)
        self.assertAlmostEqual(signal["normalized_score"], 0.8)
        self.assertGreater(signal["confidence"], 0.0)
        self.assertAlmostEqual(
            signal["confidence"],
            confidence_for_signal_raw(signal_raw, as_of=CREATED_AT),
        )
        self.assertEqual(signal["provenance"]["code_version"], CODE_VERSION)

    def test_out_of_range_normalized_value_raises_guard11(self) -> None:
        with self.assertRaises(GuardViolation):
            guard11_assert_normalize_invariants(
                normalized_score=1.5,
                window={"from": CREATED_AT, "to": CREATED_AT},
                direction_applied=True,
            )


class Law1SplitTests(unittest.TestCase):
    def test_classify_output_rejects_scoring_fields(self) -> None:
        raw = {
            "niche_id": "camping-fixture",
            "signal_type": "pain",
            "source": {"domain": "example.com", "tier": "open"},
            "window": {"from": CREATED_AT, "to": CREATED_AT},
            "raw_metric": {"name": "complaint_theme_density", "value": 0.61, "unit": "pct", "sample_n": 10},
            "evidence_ids": ["ev_test"],
            "normalized_score": 0.8,
        }
        with self.assertRaises(Exception):
            validate_signal_raw(raw)

    def test_normalize_module_is_import_pure(self) -> None:
        source = (REPO_ROOT / "signal_engine" / "normalize.py").read_text(encoding="utf-8")
        lint_import_purity(source, path=Path("signal_engine/normalize.py"))

    def test_classify_module_may_import_gateway(self) -> None:
        source = (REPO_ROOT / "signal_engine" / "classify.py").read_text(encoding="utf-8")
        self.assertIn("harness.gateway", source)


class CassetteClassifyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.gateway = LLMGateway(models_path=MODELS_PATH, mode=GatewayMode.REPLAY)
        corpus = load_fixtures()
        self.evidence = dict(corpus.cassettes["enrich"][0]["response"]["parsed"])
        cassette = corpus.cassettes["classify"][0]
        self.replay_request = dict(cassette["request"])
        self.expected_raw = dict(cassette["response"]["parsed"])

    def test_classify_evidence_replays_cassette_without_scores(self) -> None:
        execution = classify_evidence(
            self.evidence,
            gateway=self.gateway,
            replay_request=self.replay_request,
        )
        self.assertEqual(execution.verdict, "pass")
        self.assertTrue(execution.replayed)
        self.assertIsNotNone(execution.output)
        assert_law1_classify_output(execution.output)
        self.assertEqual(execution.output["signal_type"], self.expected_raw["signal_type"])
        self.assertNotIn("normalized_score", execution.output)
        self.assertNotIn("score", execution.output)

    def test_finalize_signal_raw_adds_evidence_id(self) -> None:
        execution = classify_evidence(
            self.evidence,
            gateway=self.gateway,
            replay_request=self.replay_request,
        )
        signal_raw = finalize_signal_raw(
            execution.output,
            self.evidence,
            model_id=CASSETTE_MODEL_ID,
            classify_task_id="tsk_classify_fixture",
        )
        self.assertIn(self.evidence["record_id"], signal_raw["evidence_ids"])

    def test_missing_cassette_fails_without_live_call(self) -> None:
        with patch.object(httpx.Client, "post", side_effect=AssertionError("live LLM call attempted")):
            with self.assertRaises(CassetteNotFoundError):
                classify_evidence(
                    self.evidence,
                    gateway=self.gateway,
                    replay_request={
                        "record_id": "ev_missing",
                        "prompt_version": PROMPT_VERSION,
                        "model_id": CASSETTE_MODEL_ID,
                    },
                )


class Guard5WriteGuardTests(unittest.TestCase):
    def test_opportunity_with_model_id_in_provenance_rejected(self) -> None:
        poison = json.loads(POISON_G05.read_text(encoding="utf-8"))
        with self.assertRaises(GuardViolation) as ctx:
            guard5_reject_llm_score_provenance(poison)
        self.assertIn("model_id", str(ctx.exception))


@unittest.skipUnless(_postgres_available(), "Postgres unavailable (need .env POSTGRES_*)")
class PersistClassifyNormalizeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.conn = connect()
        apply_migrations(cls.conn)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.conn.close()

    def setUp(self) -> None:
        self.conn.execute("SAVEPOINT n23_test_case")
        self.job_id = f"job_n23_{uuid.uuid4().hex[:8]}"
        self.conn.execute(
            """
            INSERT INTO research_jobs (job_id, job_kind, status, config_hash, budget, provenance)
            VALUES (%s, 'dossier', 'ACQUIRING', %s, '{}'::jsonb, '{}'::jsonb)
            ON CONFLICT (job_id) DO NOTHING
            """,
            (self.job_id, CONFIG_HASH),
        )
        corpus = load_fixtures()
        self.evidence = dict(corpus.cassettes["enrich"][0]["response"]["parsed"])
        self.replay_request = dict(corpus.cassettes["classify"][0]["request"])
        self.gateway = LLMGateway(models_path=MODELS_PATH, mode=GatewayMode.REPLAY)

    def tearDown(self) -> None:
        self.conn.execute("ROLLBACK TO SAVEPOINT n23_test_case")

    def test_run_classify_normalize_persists_signal_and_lineage(self) -> None:
        classify_task_id = "tsk_classify_n23"
        self.conn.execute(
            """
            INSERT INTO tasks (
                task_id, job_id, task_kind, lane, state,
                idempotency_key, payload_ref, provenance
            )
            VALUES (%s, %s, 'signal_classify', 'signal', 'READY', %s, %s, %s::jsonb)
            ON CONFLICT (task_id) DO NOTHING
            """,
            (
                classify_task_id,
                self.job_id,
                f"sha256:{classify_task_id}",
                json.dumps({"record_id": self.evidence["record_id"]}),
                json.dumps(
                    {
                        "schema_version": "task.v1",
                        "config_hash": CONFIG_HASH,
                        "created_at": CREATED_AT,
                    }
                ),
            ),
        )
        with tempfile.TemporaryDirectory() as tmp:
            result = run_classify_normalize_task(
                self.conn,
                job_id=self.job_id,
                evidence=self.evidence,
                classify_task_id=classify_task_id,
                config_hash=CONFIG_HASH,
                created_at=CREATED_AT,
                cohort_raw_values=PAIN_COHORT,
                gateway=self.gateway,
                replay_request=self.replay_request,
                storage_root=Path(tmp),
            )
        self.assertIsInstance(result, ClassifySuccess)
        self.assertAlmostEqual(result.signal["normalized_score"], 0.8)
        validate_signal_v1(result.signal)
        edges = edges_for_child(
            self.conn,
            child_kind="signal.v1",
            child_id=result.signal["signal_id"],
        )
        self.assertGreaterEqual(len(edges), 1)
        parent_ids = {edge.parent_id for edge in edges}
        self.assertIn(self.evidence["record_id"], parent_ids)

    def test_invalid_classify_routes_repair_not_normalize(self) -> None:
        from signal_engine.classify import ClassifyExecutionResult

        classify_task_id = "tsk_classify_fail"
        bad = ClassifyExecutionResult(
            verdict="ceiling",
            attempts=2,
            output={"signal_type": "pain"},
            violations=("missing raw_metric",),
            replayed=True,
        )
        with patch("signal_engine.classify.classify_evidence", return_value=bad):
            result = run_classify_normalize_task(
                self.conn,
                job_id=self.job_id,
                evidence=self.evidence,
                classify_task_id=classify_task_id,
                config_hash=CONFIG_HASH,
                created_at=CREATED_AT,
                cohort_raw_values=PAIN_COHORT,
                gateway=self.gateway,
                replay_request=self.replay_request,
            )
        self.assertIsInstance(result, ClassifyRepairRoute)
        self.assertEqual(result.repair_stream, REPAIR_STREAM_NAME)


class IntegrationCheckClassifyNormalize(unittest.TestCase):
    """N23 integration_check: split (LAW 1); write-guard g5; normalize invariants g11."""

    def test_integration_check_classify_normalize(self) -> None:
        corpus = load_fixtures()
        evidence = dict(corpus.cassettes["enrich"][0]["response"]["parsed"])
        cassette = corpus.cassettes["classify"][0]
        request = dict(cassette["request"])
        expected_raw = dict(cassette["response"]["parsed"])

        gateway = LLMGateway(models_path=MODELS_PATH, mode=GatewayMode.REPLAY)
        execution = classify_evidence(evidence, gateway=gateway, replay_request=request)
        self.assertEqual(execution.verdict, "pass")
        self.assertTrue(execution.replayed)
        assert_law1_classify_output(execution.output)
        self.assertEqual(execution.output["signal_type"], expected_raw["signal_type"])
        self.assertNotIn("normalized_score", execution.output)

        signal_raw = finalize_signal_raw(
            execution.output,
            evidence,
            model_id=CASSETTE_MODEL_ID,
            classify_task_id="tsk_gate4_classify",
        )
        signal = normalize_signal_raw(
            signal_raw,
            cohort_raw_values=PAIN_COHORT,
            config_hash=CONFIG_HASH,
            created_at=CREATED_AT,
        )
        validate_signal_v1(signal)
        assert_normalize_invariants(signal)
        self.assertAlmostEqual(signal["normalized_score"], 0.8)

        normalize_source = (REPO_ROOT / "signal_engine" / "normalize.py").read_text(encoding="utf-8")
        lint_import_purity(normalize_source, path=Path("signal_engine/normalize.py"))

        poison = json.loads(POISON_G05.read_text(encoding="utf-8"))
        with self.assertRaises(GuardViolation):
            guard5_reject_llm_score_provenance(poison)

        with patch.object(httpx.Client, "post", side_effect=AssertionError("live LLM call attempted")):
            with self.assertRaises(CassetteNotFoundError):
                classify_evidence(
                    evidence,
                    gateway=gateway,
                    replay_request={
                        "record_id": "ev_missing",
                        "prompt_version": PROMPT_VERSION,
                        "model_id": CASSETTE_MODEL_ID,
                    },
                )


if __name__ == "__main__":
    unittest.main()
