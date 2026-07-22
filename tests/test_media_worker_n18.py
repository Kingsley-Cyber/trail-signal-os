"""N18 media_worker — yt-dlp auto-sub fixtures, trickle + degradation."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
import uuid
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import psycopg

from control.retries.circuit_breaker import CircuitRegistry, CircuitState
from control.retries.settings import CircuitConfig
from db.repositories.connection import connect
from db.repositories.migrate import apply_migrations
from fixtures.load import FIXTURES_ROOT
from workers.media_worker import (
    MEDIA_CODE_VERSION,
    MediaBlockedResult,
    MediaWorkerError,
    TrickleConfig,
    TrickleLimiter,
    acquire_youtube_media,
    build_youtube_page_from_media,
    fetch_youtube_fixture,
    fetch_youtube_media,
    load_trickle_config,
    parse_vtt,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
ENV_FILE = REPO_ROOT / ".env"
CONFIG_HASH = "sha256:" + ("c" * 64)
CREATED_AT = "2026-07-21T12:00:00Z"
FETCHED_AT = "2026-07-21T12:10:00Z"
YOUTUBE_URL = "https://www.youtube.com/watch?v=dQw4campfan"
VTT_FIXTURE = FIXTURES_ROOT / "pages" / "youtube_transcript.vtt"

FAST_CIRCUIT_CONFIG = CircuitConfig(
    consecutive_threshold=3,
    failure_rate_threshold=0.5,
    window_size=5,
    default_cooldown_seconds=(60, 120, 240),
)
BASE_TIME = datetime(2026, 7, 21, 12, 0, 0, tzinfo=timezone.utc)

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


def _insert_media_task(
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
        VALUES (%s, %s, 'discovered_url', 'media', 'READY', %s, %s, %s::jsonb)
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


class MediaWorkerUnitTests(unittest.TestCase):
    def test_parse_vtt_strips_timestamps(self) -> None:
        vtt = VTT_FIXTURE.read_text(encoding="utf-8")
        text = parse_vtt(vtt)
        self.assertIn("desert southwest", text)
        self.assertIn("portable camping fans", text)
        self.assertNotIn("-->", text)

    def test_load_trickle_config_reads_limits_yaml(self) -> None:
        config = load_trickle_config()
        self.assertAlmostEqual(config.min_interval_seconds, 12.5, places=1)
        self.assertEqual(config.daily_cap, 150)

    def test_fetch_youtube_fixture_returns_transcript(self) -> None:
        result = fetch_youtube_fixture(YOUTUBE_URL)
        self.assertEqual(result.status_code, 200)
        self.assertTrue(result.source.startswith("fixture:ytdlp:"))
        self.assertIn("clip-on model", result.transcript_text.lower())
        self.assertEqual(result.meta["video_id"], "dQw4campfan")

    def test_build_page_includes_transcript_in_text_md(self) -> None:
        fetch = fetch_youtube_fixture(YOUTUBE_URL)
        page = build_youtube_page_from_media(
            fetch=fetch,
            media_task_id="tsk_n18_fixture",
            config_hash=CONFIG_HASH,
            created_at=CREATED_AT,
            fetched_at=FETCHED_AT,
        )
        self.assertEqual(page["page_id"], "pg_camping_youtube")
        self.assertTrue(page["platform_fields"]["has_transcript"])
        self.assertIn("# I Tested Every Portable Camping Fan", page["text_md"])
        self.assertIn("desert southwest", page["text_md"])
        self.assertEqual(page["derived_from"], ["tsk_n18_fixture"])
        self.assertEqual(page["provenance"]["code_version"], MEDIA_CODE_VERSION)

    def test_mocked_ytdlp_runner_offline(self) -> None:
        meta = json.loads((FIXTURES_ROOT / "pages" / "youtube_meta.json").read_text())
        vtt_path = VTT_FIXTURE

        def fake_ytdlp(url: str, *, output_dir: Path) -> tuple[dict, Path]:
            self.assertEqual(url, YOUTUBE_URL)
            return meta, vtt_path

        result = fetch_youtube_media(
            YOUTUBE_URL,
            prefer_fixture=False,
            ytdlp_runner=fake_ytdlp,
        )
        self.assertTrue(result.source.startswith("ytdlp:"))
        self.assertIn("folding tripod fan", result.transcript_text.lower())

    def test_trickle_waits_between_requests(self) -> None:
        limiter = TrickleLimiter(TrickleConfig(min_interval_seconds=5.0, daily_cap=10))
        sleeps: list[float] = []

        def fake_sleep(seconds: float) -> None:
            sleeps.append(seconds)

        limiter.wait_trickle(monotonic_now=100.0, sleep_fn=fake_sleep)
        limiter.wait_trickle(monotonic_now=102.0, sleep_fn=fake_sleep)
        self.assertEqual(len(sleeps), 1)
        self.assertAlmostEqual(sleeps[0], 3.0, places=3)

    def test_daily_cap_blocks_acquire(self) -> None:
        if not _postgres_available():
            self.skipTest("Postgres unavailable")
        with connect() as conn:
            apply_migrations(conn)
            conn.execute("SAVEPOINT n18_cap")
            job_id = f"job_n18_cap_{uuid.uuid4().hex[:8]}"
            task_id = f"tsk_n18_cap_{uuid.uuid4().hex[:8]}"
            _insert_job(conn, job_id)
            _insert_media_task(conn, task_id=task_id, job_id=job_id, url=YOUTUBE_URL)
            limiter = TrickleLimiter(TrickleConfig(min_interval_seconds=0.0, daily_cap=0))
            result = acquire_youtube_media(
                conn,
                media_task_id=task_id,
                job_id=job_id,
                url=YOUTUBE_URL,
                config_hash=CONFIG_HASH,
                created_at=CREATED_AT,
                fetched_at=FETCHED_AT,
                trickle=limiter,
                sleep_fn=lambda _: None,
            )
            conn.execute("ROLLBACK TO SAVEPOINT n18_cap")
        self.assertIsInstance(result, MediaBlockedResult)
        blocked = result
        self.assertTrue(blocked.source_gap)
        self.assertEqual(blocked.failure_class, "MEDIA_DAILY_CAP")


@unittest.skipUnless(_postgres_available(), "Postgres unavailable (need .env POSTGRES_*)")
class MediaWorkerPersistenceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.conn = connect()
        apply_migrations(cls.conn)
        cls.storage_root = Path(tempfile.mkdtemp(prefix="n18_storage_"))

    @classmethod
    def tearDownClass(cls) -> None:
        cls.conn.close()

    def setUp(self) -> None:
        self.conn.execute("SAVEPOINT n18_test_case")
        self.job_id = f"job_n18_{uuid.uuid4().hex[:12]}"
        self.task_id = f"tsk_n18_{uuid.uuid4().hex[:12]}"
        self.trickle = TrickleLimiter(TrickleConfig(min_interval_seconds=0.0, daily_cap=10))

    def tearDown(self) -> None:
        self.conn.execute("ROLLBACK TO SAVEPOINT n18_test_case")

    def test_acquire_persists_page_and_lineage(self) -> None:
        _insert_job(self.conn, self.job_id)
        _insert_media_task(
            self.conn,
            task_id=self.task_id,
            job_id=self.job_id,
            url=YOUTUBE_URL,
        )
        result = acquire_youtube_media(
            self.conn,
            media_task_id=self.task_id,
            job_id=self.job_id,
            url=YOUTUBE_URL,
            config_hash=CONFIG_HASH,
            created_at=CREATED_AT,
            fetched_at=FETCHED_AT,
            trickle=self.trickle,
            storage_root=self.storage_root,
            sleep_fn=lambda _: None,
        )
        self.assertNotIsInstance(result, MediaBlockedResult)
        acquire = result
        self.assertTrue(acquire.artifact_inserted)
        self.assertTrue(acquire.lineage_edge_inserted)
        self.assertTrue(acquire.fetch_source.startswith("fixture:ytdlp:"))
        self.assertIn("has_transcript", acquire.page["platform_fields"])

        with self.conn.cursor() as cur:
            cur.execute(
                """
                SELECT artifact_kind, derived_from
                FROM artifacts
                WHERE artifact_id = %s
                """,
                (acquire.artifact_id,),
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
                (acquire.page["page_id"], self.task_id),
            )
            edge = cur.fetchone()
        self.assertIsNotNone(artifact)
        self.assertEqual(artifact[0], "page.v1")
        self.assertEqual(artifact[1], [self.task_id])
        self.assertIsNotNone(edge)
        self.assertEqual(edge[0], "derived_from")

    def test_fetch_failure_sets_retry_wait_with_source_gap(self) -> None:
        _insert_job(self.conn, self.job_id)
        _insert_media_task(
            self.conn,
            task_id=self.task_id,
            job_id=self.job_id,
            url=YOUTUBE_URL,
        )

        def failing_fetch(*_args: object, **_kwargs: object) -> None:
            raise MediaWorkerError("rate limited", status_code=429, error_code="HTTP_429")

        with patch(
            "workers.media_worker.fetch_youtube_media",
            side_effect=failing_fetch,
        ):
            result = acquire_youtube_media(
                self.conn,
                media_task_id=self.task_id,
                job_id=self.job_id,
                url=YOUTUBE_URL,
                config_hash=CONFIG_HASH,
                created_at=CREATED_AT,
                fetched_at=FETCHED_AT,
                trickle=self.trickle,
                sleep_fn=lambda _: None,
                now=BASE_TIME,
            )
        self.assertIsInstance(result, MediaBlockedResult)
        blocked = result
        self.assertTrue(blocked.source_gap)
        self.assertEqual(blocked.action, "RETRY_WAIT")
        with self.conn.cursor() as cur:
            cur.execute(
                "SELECT state, retry_at FROM tasks WHERE task_id = %s",
                (self.task_id,),
            )
            state, retry_at = cur.fetchone()
        self.assertEqual(state, "RETRY_WAIT")
        self.assertIsNotNone(retry_at)

    def test_open_circuit_blocks_acquire_with_retry_wait(self) -> None:
        _insert_job(self.conn, self.job_id)
        _insert_media_task(
            self.conn,
            task_id=self.task_id,
            job_id=self.job_id,
            url=YOUTUBE_URL,
        )
        circuits = CircuitRegistry(config=FAST_CIRCUIT_CONFIG)
        route = "youtube:ytdlp"
        for _ in range(FAST_CIRCUIT_CONFIG.consecutive_threshold):
            circuits.record_failure(route, now=BASE_TIME, failure_class="HTTP_429")
        circuit = circuits.get(route)
        self.assertEqual(circuit.state, CircuitState.OPEN)

        result = acquire_youtube_media(
            self.conn,
            media_task_id=self.task_id,
            job_id=self.job_id,
            url=YOUTUBE_URL,
            config_hash=CONFIG_HASH,
            created_at=CREATED_AT,
            fetched_at=FETCHED_AT,
            circuits=circuits,
            trickle=self.trickle,
            sleep_fn=lambda _: None,
            now=BASE_TIME,
        )
        self.assertIsInstance(result, MediaBlockedResult)
        blocked = result
        self.assertTrue(blocked.source_gap)
        self.assertEqual(blocked.action, "RETRY_WAIT")
        self.assertEqual(blocked.failure_class, "CIRCUIT_OPEN")
        self.assertIsNotNone(blocked.retry_at)
        with self.conn.cursor() as cur:
            cur.execute(
                "SELECT state, retry_at FROM tasks WHERE task_id = %s",
                (self.task_id,),
            )
            state, retry_at = cur.fetchone()
        self.assertEqual(state, "RETRY_WAIT")
        self.assertIsNotNone(retry_at)
        assert circuit.cooldown_until is not None
        self.assertAlmostEqual(
            retry_at.timestamp(),
            circuit.cooldown_until.timestamp(),
            delta=1.0,
        )


class IntegrationCheckMediaWorker(unittest.TestCase):
    """Offline integration check for N18 media_worker."""

    def test_fixture_auto_sub_yields_transcript_page_v1(self) -> None:
        fetch = fetch_youtube_fixture(YOUTUBE_URL)
        page = build_youtube_page_from_media(
            fetch=fetch,
            media_task_id="tsk_gate2_youtube_media",
            config_hash=CONFIG_HASH,
            created_at=CREATED_AT,
            fetched_at=FETCHED_AT,
        )
        self.assertEqual(page["page_id"], "pg_camping_youtube")
        self.assertTrue(page["platform_fields"]["has_transcript"])
        self.assertGreater(len(page["text_md"]), 120)
        self.assertEqual(page["derived_from"], ["tsk_gate2_youtube_media"])

    def test_trickle_and_degradation_contract(self) -> None:
        limiter = TrickleLimiter(TrickleConfig(min_interval_seconds=2.0, daily_cap=2))
        sleeps: list[float] = []
        limiter.wait_trickle(monotonic_now=0.0, sleep_fn=sleeps.append)
        limiter.wait_trickle(monotonic_now=1.0, sleep_fn=sleeps.append)
        self.assertEqual(len(sleeps), 1)

        circuits = CircuitRegistry(config=FAST_CIRCUIT_CONFIG)
        for _ in range(FAST_CIRCUIT_CONFIG.consecutive_threshold):
            circuits.record_failure("youtube:ytdlp", now=BASE_TIME, failure_class="HTTP_429")
        self.assertFalse(circuits.allow_request("youtube:ytdlp", now=BASE_TIME))


if __name__ == "__main__":
    unittest.main()
