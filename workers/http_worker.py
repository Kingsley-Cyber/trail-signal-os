"""HTTP fetch worker — offline fixture URLs and optional live httpx fetch (N16)."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx

from fixtures.load import FIXTURES_ROOT

HTTP_CODE_VERSION = "http_worker-1.0.0"
DEFAULT_TIMEOUT = 30.0


@dataclass(frozen=True)
class FixtureSource:
    relative_path: str
    media_type: str
    source_class: str
    page_id: str


@dataclass(frozen=True)
class HttpFetchResult:
    url: str
    body: bytes
    media_type: str
    status_code: int
    source: str
    fixture_source: FixtureSource | None = None


FIXTURE_SOURCES: dict[str, FixtureSource] = {
    "https://trailgearlab.example/articles/portable-camping-fans": FixtureSource(
        relative_path="article.html",
        media_type="text/html",
        source_class="article",
        page_id="pg_camping_article",
    ),
    "https://camptalk.example/threads/rechargeable-fan-died-overnight": FixtureSource(
        relative_path="forum_thread.html",
        media_type="text/html",
        source_class="forum_thread",
        page_id="pg_camping_forum",
    ),
    "https://market.example/dp/B0CAMPFAN1": FixtureSource(
        relative_path="marketplace_listing.html",
        media_type="text/html",
        source_class="marketplace_listing",
        page_id="pg_camping_marketplace",
    ),
    "https://trustcamp.example/review/flexbreeze-pro": FixtureSource(
        relative_path="review_page.html",
        media_type="text/html",
        source_class="review_page",
        page_id="pg_camping_review",
    ),
    "https://www.youtube.com/watch?v=dQw4campfan": FixtureSource(
        relative_path="youtube_meta.json",
        media_type="application/json",
        source_class="youtube",
        page_id="pg_camping_youtube",
    ),
}


def fixture_source_for_url(url: str) -> FixtureSource | None:
    return FIXTURE_SOURCES.get(url)


def fixture_path_for_url(url: str, *, root: Path | None = None) -> Path | None:
    source = fixture_source_for_url(url)
    if source is None:
        return None
    return (root or FIXTURES_ROOT) / "pages" / source.relative_path


def fetch_fixture_url(url: str, *, root: Path | None = None) -> HttpFetchResult:
    source = fixture_source_for_url(url)
    if source is None:
        raise ValueError(f"no offline fixture registered for URL {url!r}")
    path = (root or FIXTURES_ROOT) / "pages" / source.relative_path
    if not path.is_file():
        raise FileNotFoundError(f"missing fixture page {path}")
    body = path.read_bytes()
    return HttpFetchResult(
        url=url,
        body=body,
        media_type=source.media_type,
        status_code=200,
        source=f"fixture:{path.name}",
        fixture_source=source,
    )


def fetch_url_live(
    url: str,
    *,
    timeout: float = DEFAULT_TIMEOUT,
    client: httpx.Client | None = None,
) -> HttpFetchResult:
    owned = client is None
    http = client or httpx.Client(timeout=timeout, follow_redirects=True)
    try:
        response = http.get(url)
        media_type = response.headers.get("content-type", "application/octet-stream")
        if ";" in media_type:
            media_type = media_type.split(";", 1)[0].strip()
        return HttpFetchResult(
            url=str(response.url),
            body=response.content,
            media_type=media_type,
            status_code=response.status_code,
            source="live:httpx",
        )
    finally:
        if owned:
            http.close()


def fetch_url(
    url: str,
    *,
    prefer_fixture: bool = True,
    fixtures_root: Path | None = None,
    timeout: float = DEFAULT_TIMEOUT,
    client: httpx.Client | None = None,
) -> HttpFetchResult:
    if prefer_fixture and fixture_source_for_url(url) is not None:
        return fetch_fixture_url(url, root=fixtures_root)
    return fetch_url_live(url, timeout=timeout, client=client)


def domain_from_url(url: str) -> str:
    parsed = urlparse(url)
    host = parsed.netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    return host


def load_youtube_meta(body: bytes) -> dict[str, Any]:
    payload = json.loads(body.decode("utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("youtube fixture meta must be a JSON object")
    return payload
