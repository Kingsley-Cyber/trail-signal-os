"""YouTube media worker — yt-dlp auto-sub path → page.v1 with trickle + degradation (N18)."""

from __future__ import annotations

import json
import re
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import psycopg
import yaml

from control.retries.circuit_breaker import CircuitRegistry
from control.retries.handle import _set_retry_wait, handle_task_failure, record_route_success
from fixtures.load import FIXTURES_ROOT
from workers.extract_worker import (
    PAGE_SCHEMA_VERSION,
    content_hash_for_text,
    make_page_id,
    persist_page_v1,
    validate_page_v1,
)
from workers.http_worker import (
    domain_from_url,
    fetch_fixture_url,
    fixture_source_for_url,
    load_youtube_meta,
)

MEDIA_CODE_VERSION = "media_worker-1.0.0"
YOUTUBE_DOMAIN = "youtube.com"
YOUTUBE_ROUTE = "youtube:ytdlp"
MEDIA_LANE = "media"
DEFAULT_TRANSCRIPT_FIXTURE = "youtube_transcript.vtt"
YTDLP_AUTO_SUB_ARGS = (
    "--write-auto-sub",
    "--skip-download",
    "--no-warnings",
    "--sub-format",
    "vtt",
)

SleepFn = Callable[[float], None]


@dataclass(frozen=True)
class TrickleConfig:
    min_interval_seconds: float = 12.5
    daily_cap: int = 150


@dataclass(frozen=True)
class MediaFetchResult:
    url: str
    meta: dict[str, Any]
    transcript_text: str
    transcript_path: str | None
    status_code: int
    source: str


@dataclass(frozen=True)
class MediaAcquireResult:
    page: dict[str, Any]
    artifact_id: str
    artifact_inserted: bool
    lineage_edge_inserted: bool
    fetch_source: str


@dataclass(frozen=True)
class MediaBlockedResult:
    action: str
    failure_class: str
    source_gap: bool
    retry_at: datetime | None = None
    degradation_event: dict[str, Any] | None = None
    circuit_state: str | None = None


class MediaWorkerError(Exception):
    """Raised when yt-dlp or fixture media fetch fails."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        error_code: str | None = None,
        retry_after_seconds: float | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.error_code = error_code
        self.retry_after_seconds = retry_after_seconds


class TrickleLimiter:
    """Rate-limit media fetches (doc 06 / limits.yaml youtube token bucket)."""

    def __init__(self, config: TrickleConfig | None = None) -> None:
        self._config = config or TrickleConfig()
        self._last_request_monotonic: float | None = None
        self._daily_count = 0

    @property
    def daily_count(self) -> int:
        return self._daily_count

    def daily_cap_reached(self) -> bool:
        return self._daily_count >= self._config.daily_cap

    def wait_trickle(
        self,
        *,
        monotonic_now: float | None = None,
        sleep_fn: SleepFn = time.sleep,
    ) -> None:
        if self._config.min_interval_seconds <= 0:
            return
        now = monotonic_now if monotonic_now is not None else time.monotonic()
        if self._last_request_monotonic is None:
            self._last_request_monotonic = now
            return
        elapsed = now - self._last_request_monotonic
        remaining = self._config.min_interval_seconds - elapsed
        if remaining > 0:
            sleep_fn(remaining)
        self._last_request_monotonic = time.monotonic()

    def record_fetch(self) -> None:
        self._daily_count += 1
        self._last_request_monotonic = time.monotonic()


def load_trickle_config(config_path: Path | None = None) -> TrickleConfig:
    """Load youtube trickle + daily media cap from config/limits.yaml."""
    path = config_path or Path(__file__).resolve().parents[1] / "config" / "limits.yaml"
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{path}: limits.yaml must be a mapping")
    buckets = payload.get("token_buckets") or {}
    youtube = buckets.get("youtube") if isinstance(buckets, dict) else {}
    rps = 0.08
    if isinstance(youtube, dict) and youtube.get("requests_per_second"):
        rps = float(youtube["requests_per_second"])
    min_interval = 1.0 / rps if rps > 0 else 0.0
    defaults = payload.get("default_budgets") or {}
    daily_cap = 150
    if isinstance(defaults, dict) and defaults.get("media_items") is not None:
        daily_cap = int(defaults["media_items"])
    return TrickleConfig(min_interval_seconds=min_interval, daily_cap=daily_cap)


def _estimate_token_count(text: str) -> int:
    stripped = text.strip()
    if not stripped:
        return 0
    return max(1, len(stripped.split()))


def parse_vtt(vtt_text: str) -> str:
    """Convert WEBVTT cues to plain transcript text."""
    lines: list[str] = []
    for raw_line in vtt_text.splitlines():
        line = raw_line.strip()
        if not line or line == "WEBVTT" or "-->" in line or re.fullmatch(r"\d+", line):
            continue
        lines.append(line)
    return "\n\n".join(lines)


def _transcript_fixture_path(
    *,
    fixtures_root: Path,
    video_id: str | None,
) -> Path:
    if video_id:
        candidate = fixtures_root / "pages" / f"{video_id}.vtt"
        if candidate.is_file():
            return candidate
    default = fixtures_root / "pages" / DEFAULT_TRANSCRIPT_FIXTURE
    if not default.is_file():
        raise MediaWorkerError(
            f"missing transcript fixture {default}",
            error_code="UNSUPPORTED_CONTENT",
        )
    return default


def fetch_youtube_fixture(
    url: str,
    *,
    fixtures_root: Path | None = None,
) -> MediaFetchResult:
    """Offline yt-dlp auto-sub stand-in using N3 youtube fixtures."""
    root = fixtures_root or FIXTURES_ROOT
    fetch = fetch_fixture_url(url, root=root)
    meta = load_youtube_meta(fetch.body)
    video_id = str(meta.get("video_id") or "")
    vtt_path = _transcript_fixture_path(fixtures_root=root, video_id=video_id)
    transcript_text = parse_vtt(vtt_path.read_text(encoding="utf-8"))
    if not transcript_text.strip():
        raise MediaWorkerError(
            f"empty transcript in {vtt_path}",
            error_code="UNSUPPORTED_CONTENT",
        )
    try:
        transcript_ref = str(vtt_path.relative_to(root.parent))
    except ValueError:
        transcript_ref = str(vtt_path)
    return MediaFetchResult(
        url=url,
        meta=meta,
        transcript_text=transcript_text,
        transcript_path=transcript_ref,
        status_code=200,
        source=f"fixture:ytdlp:{vtt_path.name}",
    )


def run_ytdlp_auto_sub(
    url: str,
    *,
    output_dir: Path,
    ytdlp_bin: str = "yt-dlp",
    timeout: float = 120.0,
) -> tuple[dict[str, Any], Path]:
    """Invoke yt-dlp --write-auto-sub --skip-download; returns meta + VTT path."""
    output_dir.mkdir(parents=True, exist_ok=True)
    info_path = output_dir / "info.json"
    cmd = [
        ytdlp_bin,
        *YTDLP_AUTO_SUB_ARGS,
        "--dump-json",
        "--write-info-json",
        "-o",
        str(output_dir / "%(id)s"),
        url,
    ]
    try:
        subprocess.run(
            cmd,
            check=True,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise MediaWorkerError(
            f"yt-dlp timed out for {url}",
            error_code="NETWORK_TIMEOUT",
        ) from exc
    except subprocess.CalledProcessError as exc:
        stderr = (exc.stderr or "").lower()
        if "429" in stderr or "too many requests" in stderr:
            raise MediaWorkerError(
                f"yt-dlp rate limited for {url}",
                status_code=429,
                error_code="HTTP_429",
            ) from exc
        if "403" in stderr or " forbidden" in stderr:
            raise MediaWorkerError(
                f"yt-dlp forbidden for {url}",
                status_code=403,
                error_code="HTTP_403",
            ) from exc
        raise MediaWorkerError(
            f"yt-dlp failed for {url}: {exc.stderr or exc.stdout}",
            error_code="EXTRACTOR_BROKEN",
        ) from exc

    if not info_path.is_file():
        json_files = sorted(output_dir.glob("*.info.json"))
        if not json_files:
            raise MediaWorkerError(
                f"yt-dlp produced no info json for {url}",
                error_code="EXTRACTOR_BROKEN",
            )
        info_path = json_files[0]

    meta = json.loads(info_path.read_text(encoding="utf-8"))
    if not isinstance(meta, dict):
        raise MediaWorkerError("yt-dlp info json must be an object", error_code="EXTRACTOR_BROKEN")

    vtt_files = sorted(output_dir.glob("*.vtt"))
    if not vtt_files:
        raise MediaWorkerError(
            f"yt-dlp auto-sub produced no VTT for {url}",
            error_code="UNSUPPORTED_CONTENT",
        )
    return meta, vtt_files[0]


def fetch_youtube_media(
    url: str,
    *,
    prefer_fixture: bool = True,
    fixtures_root: Path | None = None,
    output_dir: Path | None = None,
    ytdlp_runner: Callable[..., tuple[dict[str, Any], Path]] | None = None,
) -> MediaFetchResult:
    """Fetch YouTube metadata + auto-sub transcript via fixtures or yt-dlp."""
    if prefer_fixture and fixture_source_for_url(url) is not None:
        return fetch_youtube_fixture(url, fixtures_root=fixtures_root)

    runner = ytdlp_runner or run_ytdlp_auto_sub
    work_dir = output_dir or Path(".media_worker_ytdlp")
    meta, vtt_path = runner(url, output_dir=work_dir)
    transcript_text = parse_vtt(vtt_path.read_text(encoding="utf-8"))
    if not transcript_text.strip():
        raise MediaWorkerError(
            f"yt-dlp auto-sub empty for {url}",
            error_code="UNSUPPORTED_CONTENT",
        )
    return MediaFetchResult(
        url=url,
        meta=meta,
        transcript_text=transcript_text,
        transcript_path=str(vtt_path),
        status_code=200,
        source=f"ytdlp:{vtt_path.name}",
    )


def build_youtube_page_from_media(
    *,
    fetch: MediaFetchResult,
    media_task_id: str,
    config_hash: str,
    created_at: str,
    fetched_at: str,
) -> dict[str, Any]:
    """Merge yt-dlp metadata + auto-sub transcript into page.v1."""
    meta = fetch.meta
    title = str(meta.get("title") or "")
    description = str(meta.get("description") or "").strip()
    transcript = fetch.transcript_text.strip()
    body_parts = [part for part in (transcript, description) if part]
    body = "\n\n".join(body_parts)
    text_md = f"# {title}\n\n{body}".strip() if title else body
    video_id = str(meta.get("video_id") or meta.get("id") or "")
    fixture_source = fixture_source_for_url(fetch.url)
    page_id = make_page_id(fetch.url, fixture_source=fixture_source)
    content_hash = content_hash_for_text(text_md)
    platform_fields: dict[str, Any] = {
        "source_class": "youtube",
        "video_id": video_id,
        "view_count": meta.get("view_count"),
        "duration_seconds": meta.get("duration_seconds") or meta.get("duration"),
        "has_transcript": True,
        "subtitle_format": "vtt",
    }
    if fetch.transcript_path:
        platform_fields["transcript_path"] = fetch.transcript_path
    return {
        "page_id": page_id,
        "url": fetch.url,
        "canonical_url": str(meta.get("url") or fetch.url),
        "domain": domain_from_url(fetch.url),
        "fetched_at": fetched_at,
        "published_at": meta.get("published_at") or meta.get("upload_date"),
        "title": title,
        "author": meta.get("channel") or meta.get("uploader"),
        "text_md": text_md,
        "links": [],
        "media": [{"url": fetch.url, "kind": "video"}],
        "platform_fields": platform_fields,
        "content_hash": content_hash,
        "token_count": _estimate_token_count(text_md),
        "derived_from": [media_task_id],
        "provenance": {
            "code_version": MEDIA_CODE_VERSION,
            "schema_version": PAGE_SCHEMA_VERSION,
            "config_hash": config_hash,
            "created_at": created_at,
        },
        "schema_version": PAGE_SCHEMA_VERSION,
    }


def _utc_now(now: datetime | None) -> datetime:
    if now is None:
        return datetime.now(timezone.utc)
    if now.tzinfo is None:
        return now.replace(tzinfo=timezone.utc)
    return now


def acquire_youtube_media(
    conn: psycopg.Connection,
    *,
    media_task_id: str,
    job_id: str,
    url: str,
    config_hash: str,
    created_at: str,
    fetched_at: str,
    attempt: int = 1,
    max_attempts: int = 4,
    circuits: CircuitRegistry | None = None,
    trickle: TrickleLimiter | None = None,
    prefer_fixture: bool = True,
    fixtures_root: Path | None = None,
    storage_root: Path | None = None,
    ytdlp_runner: Callable[..., tuple[dict[str, Any], Path]] | None = None,
    now: datetime | None = None,
    sleep_fn: SleepFn = time.sleep,
) -> MediaAcquireResult | MediaBlockedResult:
    """Fetch auto-sub transcript, persist page.v1, honor trickle + degradation."""
    current_time = _utc_now(now)
    registry = circuits or CircuitRegistry()
    limiter = trickle or TrickleLimiter(load_trickle_config())

    if limiter.daily_cap_reached():
        return MediaBlockedResult(
            action="SOURCE_GAP",
            failure_class="MEDIA_DAILY_CAP",
            source_gap=True,
        )

    if not registry.allow_request(YOUTUBE_ROUTE, now=current_time):
        circuit = registry.get(YOUTUBE_ROUTE)
        retry_at = circuit.cooldown_until
        next_attempt = attempt + 1
        _set_retry_wait(
            conn,
            task_id=media_task_id,
            retry_at=retry_at,
            attempt=next_attempt,
        )
        return MediaBlockedResult(
            action="RETRY_WAIT",
            failure_class="CIRCUIT_OPEN",
            source_gap=True,
            retry_at=retry_at,
            circuit_state=circuit.state.value,
        )

    limiter.wait_trickle(sleep_fn=sleep_fn)

    try:
        fetch = fetch_youtube_media(
            url,
            prefer_fixture=prefer_fixture,
            fixtures_root=fixtures_root,
            ytdlp_runner=ytdlp_runner,
        )
    except MediaWorkerError as exc:
        handle_result = handle_task_failure(
            conn,
            task_id=media_task_id,
            job_id=job_id,
            domain=YOUTUBE_DOMAIN,
            route=YOUTUBE_ROUTE,
            lane=MEDIA_LANE,
            attempt=attempt,
            max_attempts=max_attempts,
            circuits=registry,
            config_hash=config_hash,
            status_code=exc.status_code,
            error_code=exc.error_code,
            retry_after_seconds=exc.retry_after_seconds,
            now=current_time,
            event_id_prefix="deg_n18",
        )
        return MediaBlockedResult(
            action=handle_result.action,
            failure_class=handle_result.failure_class,
            source_gap=True,
            retry_at=handle_result.retry_at,
            degradation_event=handle_result.degradation_event,
            circuit_state=handle_result.circuit_state,
        )

    limiter.record_fetch()
    record_route_success(
        registry,
        domain=YOUTUBE_DOMAIN,
        route=YOUTUBE_ROUTE,
        now=current_time,
        config_hash=config_hash,
        event_id="deg_n18_close",
    )

    page = build_youtube_page_from_media(
        fetch=fetch,
        media_task_id=media_task_id,
        config_hash=config_hash,
        created_at=created_at,
        fetched_at=fetched_at,
    )
    validate_page_v1(page)
    artifact_id, artifact_inserted, edge_inserted = persist_page_v1(
        conn,
        page,
        fetch_task_id=media_task_id,
        storage_root=storage_root,
    )
    return MediaAcquireResult(
        page=page,
        artifact_id=artifact_id,
        artifact_inserted=artifact_inserted,
        lineage_edge_inserted=edge_inserted,
        fetch_source=fetch.source,
    )
