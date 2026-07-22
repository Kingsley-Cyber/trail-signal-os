"""Deterministic HTML/JSON extraction → page.v1 with lineage (N16)."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

import psycopg
from jsonschema import Draft202012Validator
from selectolax.parser import HTMLParser
import trafilatura

from db.repositories.constraints import insert_lineage_edge_idempotent
from db.repositories.persist_artifact import persist_artifact
from fixtures.load import FIXTURES_ROOT, SCHEMAS_DIR
from guards.runtime_guards import guard6_require_lineage_edge
from workers.http_worker import (
    FixtureSource,
    HttpFetchResult,
    domain_from_url,
    fetch_url,
    fixture_source_for_url,
    load_youtube_meta,
)

EXTRACT_CODE_VERSION = "extract-1.0.0"
PAGE_SCHEMA_VERSION = "page.v1"


@dataclass(frozen=True)
class PageExtractResult:
    page: dict[str, Any]
    artifact_id: str
    artifact_inserted: bool
    lineage_edge_inserted: bool
    fetch_source: str


def _load_page_schema() -> dict[str, Any]:
    return json.loads((SCHEMAS_DIR / "page.v1.schema.json").read_text(encoding="utf-8"))


def validate_page_v1(page: dict[str, Any]) -> None:
    Draft202012Validator(_load_page_schema()).validate(page)


def content_hash_for_text(text: str) -> str:
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def make_page_id(url: str, *, fixture_source: FixtureSource | None = None) -> str:
    if fixture_source is not None:
        return fixture_source.page_id
    digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]
    return f"pg_{digest}"


def _estimate_token_count(text: str) -> int:
    stripped = text.strip()
    if not stripped:
        return 0
    return max(1, len(stripped.split()))


def _canonical_url(url: str, html: HTMLParser | None = None) -> str:
    if html is not None:
        node = html.css_first('link[rel="canonical"]')
        if node is not None:
            href = node.attributes.get("href")
            if href:
                return urljoin(url, href)
    return url


def _extract_title(html: HTMLParser) -> str:
    node = html.css_first("h1")
    if node is not None:
        title = node.text(strip=True)
        if title:
            return title
    title_node = html.css_first("title")
    if title_node is not None:
        return title_node.text(strip=True)
    return ""


def _extract_author(html: HTMLParser, source_class: str) -> str | None:
    if source_class == "forum_thread":
        node = html.css_first(".post[data-author]")
        if node is not None:
            author = node.attributes.get("data-author")
            if author:
                return author
    if source_class == "article":
        byline = html.css_first(".byline")
        if byline is not None:
            match = re.search(r"By\s+([^·]+)", byline.text())
            if match:
                return match.group(1).strip()
    if source_class == "review_page":
        return "TrustCamp Reviews"
    return None


def _extract_links(html: HTMLParser, base_url: str) -> list[str]:
    links: list[str] = []
    for node in html.css("a[href]"):
        href = node.attributes.get("href")
        if not href or href.startswith("#"):
            continue
        absolute = urljoin(base_url, href)
        if absolute not in links:
            links.append(absolute)
    return links


def _markdown_from_html(html_bytes: bytes, url: str) -> str:
    downloaded = trafilatura.extract(
        html_bytes.decode("utf-8", errors="replace"),
        url=url,
        output_format="markdown",
        include_links=False,
        include_tables=True,
        include_comments=False,
    )
    if downloaded and downloaded.strip():
        return downloaded.strip()
    parser = HTMLParser(html_bytes)
    title = _extract_title(parser)
    paragraphs = [node.text(strip=True) for node in parser.css("p") if node.text(strip=True)]
    body = "\n\n".join(paragraphs)
    if title and body:
        return f"# {title}\n\n{body}"
    return body or title


def _platform_fields_tier_a(
    html: HTMLParser,
    *,
    source_class: str,
    text_md: str,
) -> dict[str, Any]:
    fields: dict[str, Any] = {"source_class": source_class}
    if source_class == "article":
        fields["word_count"] = len(text_md.split())
    elif source_class == "forum_thread":
        post = html.css_first(".post")
        if post is not None:
            match = re.search(r"(\d+)\s+replies", post.text())
            if match:
                fields["reply_count"] = int(match.group(1))
    elif source_class == "marketplace_listing":
        listing = html.css_first("#listing")
        if listing is not None:
            asin = listing.attributes.get("data-asin")
            if asin:
                fields["asin"] = asin
        price = html.css_first(".price")
        if price is not None:
            match = re.search(r"\$([0-9]+(?:\.[0-9]{2})?)", price.text())
            if match:
                fields["price_usd"] = float(match.group(1))
        rating = html.css_first(".rating")
        if rating is not None:
            match = re.search(r"([\d,]+)\s+ratings", rating.text())
            if match:
                fields["rating_count"] = int(match.group(1).replace(",", ""))
    elif source_class == "review_page":
        header = html.css_first("header")
        if header is not None:
            match = re.search(r"(\d+)\s+verified reviews", header.text())
            if match:
                fields["review_count"] = int(match.group(1))
    return fields


def _extract_html_page(
    *,
    html_bytes: bytes,
    url: str,
    fetch_task_id: str,
    config_hash: str,
    created_at: str,
    fetched_at: str,
    fixture_source: FixtureSource | None,
) -> dict[str, Any]:
    parser = HTMLParser(html_bytes)
    title = _extract_title(parser)
    text_md = _markdown_from_html(html_bytes, url)
    if title and not text_md.startswith("#"):
        text_md = f"# {title}\n\n{text_md}" if text_md else f"# {title}"
    source_class = fixture_source.source_class if fixture_source else "generic"
    platform_fields = (
        _platform_fields_tier_a(parser, source_class=source_class, text_md=text_md)
        if fixture_source is not None
        else {}
    )
    canonical = _canonical_url(url, parser)
    page_id = make_page_id(url, fixture_source=fixture_source)
    content_hash = content_hash_for_text(text_md)
    return {
        "page_id": page_id,
        "url": url,
        "canonical_url": canonical,
        "domain": domain_from_url(url),
        "fetched_at": fetched_at,
        "published_at": None,
        "title": title,
        "author": _extract_author(parser, source_class),
        "text_md": text_md,
        "links": _extract_links(parser, url),
        "media": [],
        "platform_fields": platform_fields,
        "content_hash": content_hash,
        "token_count": _estimate_token_count(text_md),
        "derived_from": [fetch_task_id],
        "provenance": {
            "code_version": EXTRACT_CODE_VERSION,
            "schema_version": PAGE_SCHEMA_VERSION,
            "config_hash": config_hash,
            "created_at": created_at,
        },
        "schema_version": PAGE_SCHEMA_VERSION,
    }


def _extract_youtube_page(
    *,
    meta: dict[str, Any],
    url: str,
    fetch_task_id: str,
    config_hash: str,
    created_at: str,
    fetched_at: str,
) -> dict[str, Any]:
    title = str(meta.get("title") or "")
    description = str(meta.get("description") or "")
    text_md = f"# {title}\n\n{description}".strip()
    video_id = str(meta.get("video_id") or "")
    page_id = make_page_id(url, fixture_source=fixture_source_for_url(url))
    content_hash = content_hash_for_text(text_md)
    return {
        "page_id": page_id,
        "url": url,
        "canonical_url": url,
        "domain": domain_from_url(url),
        "fetched_at": fetched_at,
        "published_at": meta.get("published_at"),
        "title": title,
        "author": meta.get("channel"),
        "text_md": text_md,
        "links": [],
        "media": [{"url": url, "kind": "video"}],
        "platform_fields": {
            "source_class": "youtube",
            "video_id": video_id,
            "view_count": meta.get("view_count"),
            "duration_seconds": meta.get("duration_seconds"),
            "transcript_path": "fixtures/pages/youtube_transcript.vtt",
        },
        "content_hash": content_hash,
        "token_count": _estimate_token_count(text_md),
        "derived_from": [fetch_task_id],
        "provenance": {
            "code_version": EXTRACT_CODE_VERSION,
            "schema_version": PAGE_SCHEMA_VERSION,
            "config_hash": config_hash,
            "created_at": created_at,
        },
        "schema_version": PAGE_SCHEMA_VERSION,
    }


def extract_page_from_fetch(
    fetch: HttpFetchResult,
    *,
    fetch_task_id: str,
    config_hash: str,
    created_at: str,
    fetched_at: str,
) -> dict[str, Any]:
    fixture_source = fetch.fixture_source or fixture_source_for_url(fetch.url)
    if fetch.media_type == "application/json" or (
        fixture_source is not None and fixture_source.source_class == "youtube"
    ):
        meta = load_youtube_meta(fetch.body)
        return _extract_youtube_page(
            meta=meta,
            url=fetch.url,
            fetch_task_id=fetch_task_id,
            config_hash=config_hash,
            created_at=created_at,
            fetched_at=fetched_at,
        )
    return _extract_html_page(
        html_bytes=fetch.body,
        url=fetch.url,
        fetch_task_id=fetch_task_id,
        config_hash=config_hash,
        created_at=created_at,
        fetched_at=fetched_at,
        fixture_source=fixture_source,
    )


def _lineage_edge_exists(
    conn: psycopg.Connection,
    *,
    page_id: str,
    fetch_task_id: str,
) -> bool:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT 1
            FROM lineage_edges
            WHERE child_kind = %s
              AND child_id = %s
              AND parent_kind = 'task'
              AND parent_id = %s
              AND relation = 'derived_from'
            """,
            (PAGE_SCHEMA_VERSION, page_id, fetch_task_id),
        )
        return cur.fetchone() is not None


def persist_page_v1(
    conn: psycopg.Connection,
    page: dict[str, Any],
    *,
    fetch_task_id: str,
    storage_root: Path | None = None,
) -> tuple[str, bool, bool]:
    validate_page_v1(page)
    artifact_inserted = persist_artifact(
        conn,
        artifact_id=page["page_id"],
        content_hash=page["content_hash"],
        artifact_kind=PAGE_SCHEMA_VERSION,
        payload=page,
        derived_from=list(page["derived_from"]),
        provenance=page["provenance"],
        created_by_task=fetch_task_id,
        schema_version=PAGE_SCHEMA_VERSION,
        storage_root=storage_root,
    )
    edge_inserted = insert_lineage_edge_idempotent(
        conn,
        child_kind=PAGE_SCHEMA_VERSION,
        child_id=page["page_id"],
        parent_kind="task",
        parent_id=fetch_task_id,
        relation="derived_from",
        version_tag=EXTRACT_CODE_VERSION,
    )
    guard6_require_lineage_edge(
        parent_refs=page["derived_from"],
        lineage_edge_written=edge_inserted or _lineage_edge_exists(
            conn,
            page_id=page["page_id"],
            fetch_task_id=fetch_task_id,
        ),
    )
    return page["page_id"], artifact_inserted, edge_inserted


def run_fetch_and_extract(
    conn: psycopg.Connection,
    *,
    fetch_task_id: str,
    url: str,
    config_hash: str,
    created_at: str,
    fetched_at: str,
    prefer_fixture: bool = True,
    fixtures_root: Path | None = None,
    storage_root: Path | None = None,
    client: Any | None = None,
) -> PageExtractResult:
    fetch = fetch_url(
        url,
        prefer_fixture=prefer_fixture,
        fixtures_root=fixtures_root or FIXTURES_ROOT,
        client=client,
    )
    page = extract_page_from_fetch(
        fetch,
        fetch_task_id=fetch_task_id,
        config_hash=config_hash,
        created_at=created_at,
        fetched_at=fetched_at,
    )
    artifact_id, artifact_inserted, edge_inserted = persist_page_v1(
        conn,
        page,
        fetch_task_id=fetch_task_id,
        storage_root=storage_root,
    )
    return PageExtractResult(
        page=page,
        artifact_id=artifact_id,
        artifact_inserted=artifact_inserted,
        lineage_edge_inserted=edge_inserted,
        fetch_source=fetch.source,
    )
