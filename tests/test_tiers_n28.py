"""N28 tiers — discount + hostile-dependence cap; tier-loss → no evasion (g10)."""

from __future__ import annotations

import copy
import json
import os
import unittest
import uuid
from pathlib import Path

import psycopg

from fixtures.load import (
    CAMPING_EXPECTED_CONFIDENCE,
    CAMPING_EXPECTED_SCORE,
    FIXTURES_ROOT,
    load_fixtures,
)
from guards.exceptions import GuardViolation
from guards.runtime_guards import guard10_route_403_to_blocked
from guards.static_lint import lint_import_purity, lint_no_evasion_deps
from lineage.diff import diff_lineage
from lineage.replay import replay_lineage
from signal_engine.score import build_opportunity_v1, load_weights, score
from signal_engine.tiers import (
    CODE_VERSION,
    DEFAULT_TIER_WEIGHT,
    HOSTILE_DEPENDENCE_CONFIDENCE_CAP,
    apply_hostile_dependence_cap,
    compute_hostile_dependent,
    diff_opportunity_scores,
    evaluate_tier_effects,
    filter_signals_without_tiers,
    replay_opportunity_score,
    route_tier_loss_response,
    score_after_tier_loss,
    tier_loss_gaps_after_removal,
    tier_weight_for,
)
from workers.extract_worker import run_fetch_and_extract
from workers.search_worker import _fetch_task_id, run_search_from_fixture

REPO_ROOT = Path(__file__).resolve().parents[1]
AS_OF = "2026-07-21T12:00:00Z"
CONFIG_HASH = "sha256:" + ("a" * 64)
ENV_FILE = REPO_ROOT / ".env"
CAMPING_FIXTURE = FIXTURES_ROOT / "search" / "searxng_portable_camping_fan.json"
ARTICLE_URL = "https://trailgearlab.example/articles/portable-camping-fans"
CREATED_AT = "2026-07-21T14:00:00Z"
FETCHED_AT = "2026-07-21T14:05:00Z"

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
        from db.repositories.connection import connect

        with connect() as conn:
            conn.execute("SELECT 1")
        return True
    except (psycopg.Error, RuntimeError):
        return False


class TierDiscountTests(unittest.TestCase):
    def test_tier_weight_matches_doc_08_defaults(self) -> None:
        self.assertEqual(tier_weight_for("open"), 1.00)
        self.assertEqual(tier_weight_for("defended"), 0.85)
        self.assertEqual(tier_weight_for("hostile"), 0.50)
        self.assertLess(tier_weight_for("hostile"), tier_weight_for("open"))

    def test_tier_weight_reads_from_weights_yaml(self) -> None:
        weights = load_weights()
        self.assertEqual(
            tier_weight_for("hostile", tier_weight=weights["tier_weight"]),
            0.50,
        )


class HostileDependenceCapTests(unittest.TestCase):
    def test_cap_applies_when_hostile_dependent(self) -> None:
        capped = apply_hostile_dependence_cap(
            0.65,
            hostile_dependent=True,
            cap=HOSTILE_DEPENDENCE_CONFIDENCE_CAP,
        )
        self.assertEqual(capped, 0.50)

    def test_cap_skipped_when_not_hostile_dependent(self) -> None:
        self.assertEqual(
            apply_hostile_dependence_cap(0.65, hostile_dependent=False),
            0.65,
        )

    def test_evaluate_tier_effects_rounds_confidence(self) -> None:
        corpus = load_fixtures()
        signals = corpus.camping_signals["signals"]
        effects = evaluate_tier_effects(
            raw_confidence=0.654,
            niche_id="camping-fixture",
            signals=signals,
            as_of=AS_OF,
        )
        self.assertFalse(effects.hostile_dependent)
        self.assertEqual(effects.confidence, 0.65)
        self.assertTrue(
            any(gap["signal_type"] == "content" for gap in effects.tier_loss_gaps),
        )


class CampingFixtureTierTests(unittest.TestCase):
    def test_camping_fixture_not_hostile_dependent(self) -> None:
        corpus = load_fixtures()
        weights = load_weights()
        result = score(corpus.camping_signals, weights, as_of=AS_OF)
        self.assertFalse(result.hostile_dependent)
        self.assertFalse(
            compute_hostile_dependent(
                niche_id="camping-fixture",
                signals=corpus.camping_signals["signals"],
                as_of=AS_OF,
                min_cell_confidence=float(weights["min_cell_confidence"]),
            ),
        )


class TierLossTests(unittest.TestCase):
    def test_removing_hostile_signals_flags_content_gap(self) -> None:
        corpus = load_fixtures()
        signals = corpus.camping_signals["signals"]
        remaining = filter_signals_without_tiers(signals, ("hostile",))
        self.assertEqual(len(remaining), len(signals) - 1)

        gaps = tier_loss_gaps_after_removal(
            niche_id="camping-fixture",
            signals=remaining,
            as_of=AS_OF,
            excluded_tiers=(),
        )
        gap_types = {gap["signal_type"] for gap in gaps}
        self.assertIn("content", gap_types)

    def test_tier_loss_still_scores_on_open_sources(self) -> None:
        corpus = load_fixtures()
        weights = load_weights()
        signals = copy.deepcopy(corpus.camping_signals)
        signals["signals"].append(
            {
                "signal_id": "sig_camping_content_open",
                "niche_id": "camping-fixture",
                "signal_type": "content",
                "source": {"domain": "reddit.com", "tier": "open"},
                "window": {"from": "2026-06-01T00:00:00Z", "to": "2026-07-01T00:00:00Z"},
                "normalized_score": 0.55,
                "confidence": 0.45,
                "observed_at": AS_OF,
                "expires_at": "2026-08-11T12:00:00Z",
                "derived_from": ["ev_camping_content_open"],
                "provenance": {
                    "code_version": "normalize-1.0.0",
                    "schema_version": "signal.v1",
                    "config_hash": CONFIG_HASH,
                    "created_at": AS_OF,
                },
                "schema_version": "signal.v1",
            },
        )
        baseline = score(signals, weights, as_of=AS_OF)
        after_loss, gaps = score_after_tier_loss(
            signals,
            weights,
            niche_id="camping-fixture",
            as_of=AS_OF,
        )
        self.assertGreater(after_loss.score, 0.0)
        self.assertLess(after_loss.score, baseline.score)
        self.assertFalse(any(gap["signal_type"] == "content" for gap in gaps))

    def test_tier_loss_routes_blocked_not_stealth(self) -> None:
        self.assertEqual(
            route_tier_loss_response(status_code=403, escalation=None),
            "BLOCKED",
        )
        with self.assertRaises(GuardViolation):
            route_tier_loss_response(status_code=403, escalation="stealth_browser")
        self.assertEqual(
            guard10_route_403_to_blocked(status_code=403, escalation=None),
            "BLOCKED",
        )


class ReplayDiffTests(unittest.TestCase):
    def test_replay_reproduces_camping_fixture_score(self) -> None:
        corpus = load_fixtures()
        weights = load_weights()
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
        self.assertEqual(first, second)
        self.assertEqual(first.score, CAMPING_EXPECTED_SCORE)
        self.assertEqual(first.confidence, CAMPING_EXPECTED_CONFIDENCE)

    def test_diff_detects_weights_bump_delta(self) -> None:
        corpus = load_fixtures()
        weights = load_weights()
        expected = corpus.camping_expected

        baseline_result = score(corpus.camping_signals, weights, as_of=AS_OF)
        baseline = build_opportunity_v1(
            opportunity_id=str(expected["opportunity_id"]),
            niche_id="camping-fixture",
            candidate=dict(expected["candidate"]),
            score_result=baseline_result,
            weights=weights,
            config_hash=CONFIG_HASH,
            as_of=AS_OF,
        )

        bumped_weights = copy.deepcopy(weights)
        bumped_weights["version"] = "w-2026.07.21-bump"
        bumped_weights["axis_weights"]["demand"] = 0.35
        bumped_weights["axis_weights"]["pain"] = 0.15
        bumped_weights["axis_weights"]["growth"] = 0.10
        bumped_weights["axis_weights"]["competition"] = 0.20
        bumped_weights["axis_weights"]["content"] = 0.20
        bumped_result = score(corpus.camping_signals, bumped_weights, as_of=AS_OF)
        bumped = build_opportunity_v1(
            opportunity_id="opp_camping_fixture_bumped",
            niche_id="camping-fixture",
            candidate=dict(expected["candidate"]),
            score_result=bumped_result,
            weights=bumped_weights,
            config_hash=CONFIG_HASH,
            as_of=AS_OF,
        )

        diff = diff_opportunity_scores(baseline, bumped)
        self.assertFalse(diff["identical"])
        self.assertNotEqual(diff["score_delta"], 0.0)
        self.assertEqual(len(diff["version_tag_changes"]), 1)
        self.assertEqual(diff["version_tag_changes"][0]["field"], "weights_version")

        replay_diff = diff_opportunity_scores(baseline, baseline)
        self.assertTrue(replay_diff["identical"])


@unittest.skipUnless(_postgres_available(), "Postgres unavailable (need .env POSTGRES_*)")
class LineageReplayDiffPostgresTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        from db.repositories.connection import connect
        from db.repositories.migrate import apply_migrations

        cls.conn = connect()
        apply_migrations(cls.conn)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.conn.close()

    def setUp(self) -> None:
        self.conn.execute("SAVEPOINT n28_test_case")
        self.job_id = f"job_n28_{uuid.uuid4().hex[:12]}"
        self._insert_job()
        self.chain = self._build_chain()

    def tearDown(self) -> None:
        self.conn.execute("ROLLBACK TO SAVEPOINT n28_test_case")

    def _insert_job(self) -> None:
        provenance = {
            "schema_version": "job.v1",
            "config_hash": CONFIG_HASH,
            "created_at": CREATED_AT,
        }
        self.conn.execute(
            """
            INSERT INTO research_jobs (
                job_id, job_kind, status, config_hash, budget, provenance
            )
            VALUES (%s, %s, %s, %s, %s::jsonb, %s::jsonb)
            ON CONFLICT (job_id) DO NOTHING
            """,
            (
                self.job_id,
                "dossier",
                "ACQUIRING",
                CONFIG_HASH,
                json.dumps(BUDGET),
                json.dumps(provenance),
            ),
        )

    def _build_chain(self) -> dict[str, str]:
        import tempfile

        storage_dir = tempfile.TemporaryDirectory(prefix="n28-lineage-")
        self.addCleanup(storage_dir.cleanup)
        storage_root = Path(storage_dir.name)
        search = run_search_from_fixture(
            self.conn,
            job_id=self.job_id,
            config_hash=CONFIG_HASH,
            created_at=CREATED_AT,
            fixture_path=CAMPING_FIXTURE,
            enqueue_fetch=False,
        )
        fetch_task_id = _fetch_task_id(
            self.job_id,
            search.query_spec.query_spec_id,
            ARTICLE_URL,
        )
        extract = run_fetch_and_extract(
            self.conn,
            fetch_task_id=fetch_task_id,
            url=ARTICLE_URL,
            config_hash=CONFIG_HASH,
            created_at=CREATED_AT,
            fetched_at=FETCHED_AT,
            storage_root=storage_root,
        )
        return {
            "query_spec_id": search.query_spec.query_spec_id,
            "page_id": extract.page["page_id"],
        }

    def test_lineage_replay_and_diff_on_fixture_chain(self) -> None:
        page_id = self.chain["page_id"]
        plan = replay_lineage(self.conn, page_id, pin_versions=True)
        self.assertTrue(plan.as_dict()["replayable"])

        diff = diff_lineage(
            self.conn,
            left_kind="page.v1",
            left_id=page_id,
            right_kind="page.v1",
            right_id=page_id,
        )
        self.assertTrue(diff.as_dict()["identical"])


class ImportPurityTests(unittest.TestCase):
    def test_tiers_module_is_import_pure(self) -> None:
        source = (REPO_ROOT / "signal_engine" / "tiers.py").read_text(encoding="utf-8")
        lint_import_purity(source, path=Path("signal_engine/tiers.py"))
        lint_no_evasion_deps(source, path=Path("signal_engine/tiers.py"))


class IntegrationCheckTiers(unittest.TestCase):
    """N28 integration_check: discount + hostile cap; tier-loss → no evasion (g10); replay/diff."""

    def test_integration_check_tiers(self) -> None:
        corpus = load_fixtures()
        weights = load_weights()
        expected = corpus.camping_expected

        self.assertEqual(DEFAULT_TIER_WEIGHT["hostile"], 0.50)
        self.assertLess(tier_weight_for("hostile"), tier_weight_for("open"))

        baseline = score(corpus.camping_signals, weights, as_of=AS_OF)
        self.assertEqual(baseline.score, 0.72)
        self.assertEqual(baseline.confidence, 0.65)
        self.assertFalse(baseline.hostile_dependent)

        replayed = replay_opportunity_score(
            corpus.camping_signals,
            weights,
            niche_id="camping-fixture",
            as_of=AS_OF,
        )
        self.assertEqual(replayed.score, baseline.score)
        self.assertEqual(replayed.confidence, baseline.confidence)

        remaining = filter_signals_without_tiers(
            corpus.camping_signals["signals"],
            ("hostile",),
        )
        loss_gaps = tier_loss_gaps_after_removal(
            niche_id="camping-fixture",
            signals=remaining,
            as_of=AS_OF,
            excluded_tiers=(),
        )
        self.assertTrue(any(gap["signal_type"] == "content" for gap in loss_gaps))
        self.assertEqual(route_tier_loss_response(status_code=403, escalation=None), "BLOCKED")

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
        golden = json.loads(
            (
                REPO_ROOT
                / "fixtures"
                / "niches"
                / "camping-fixture"
                / "expected_opportunity.json"
            ).read_text(encoding="utf-8"),
        )
        self.assertEqual(opportunity["score"], golden["score"])
        self.assertEqual(opportunity["confidence"], golden["confidence"])
        self.assertFalse(opportunity["hostile_dependent"])

        self.assertTrue(
            diff_opportunity_scores(opportunity, opportunity)["identical"],
        )
        self.assertEqual(CODE_VERSION, "tiers-1.0.0")

        tiers_source = (REPO_ROOT / "signal_engine" / "tiers.py").read_text(
            encoding="utf-8",
        )
        lint_import_purity(tiers_source, path=Path("signal_engine/tiers.py"))
        lint_no_evasion_deps(tiers_source, path=Path("signal_engine/tiers.py"))

    @unittest.skipUnless(_postgres_available(), "Postgres unavailable (need .env POSTGRES_*)")
    def test_integration_lineage_replay_diff_postgres(self) -> None:
        suite = unittest.TestSuite()
        suite.addTest(
            LineageReplayDiffPostgresTests("test_lineage_replay_and_diff_on_fixture_chain"),
        )
        result = unittest.TextTestRunner(verbosity=0).run(suite)
        self.assertTrue(result.wasSuccessful())


if __name__ == "__main__":
    unittest.main()
