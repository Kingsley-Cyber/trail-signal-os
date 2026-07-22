"""SearXNG search worker — fixture or live queries → query_specs + discovered URLs (N15)."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import httpx
import psycopg

from control.dispatcher import enqueue_ready_task
from db.repositories.constraints import insert_lineage_edge_idempotent, insert_task_idempotent
from fixtures.load import FIXTURES_ROOT

DEFAULT_SEARXNG_URL = "http://127.0.0.1:8080"
SEARCH_ENGINE = "searxng"
CODE_VERSION = "search_worker-1.0.0"


@dataclass(frozen=True)
class DiscoveredUrl:
    url: str
    title: str
    engine: str
    score: float | None
    category: str | None


@dataclass(frozen=True)
class QuerySpecRecord:
    query_spec_id: str
    job_id: str
    text: str
    engine: str
    params: dict[str, Any]


@dataclass(frozen=True)
class SearchResult:
    query_spec: QuerySpecRecord
    urls: tuple[DiscoveredUrl, ...]
    fetch_task_ids: tuple[str, ...]
    source: str


def slugify_query(query: str) -> str:
    return "_".join(query.lower().split())


def fixture_path_for_query(query: str, *, root: Path | None = None) -> Path:
    search_dir = (root or FIXTURES_ROOT) / "search"
    return search_dir / f"searxng_{slugify_query(query)}.json"


def load_searxng_fixture(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{path}: SearXNG fixture must be a JSON object")
    if "query" not in payload or "results" not in payload:
        raise ValueError(f"{path}: fixture must include query and results")
    results = payload["results"]
    if not isinstance(results, list) or not results:
        raise ValueError(f"{path}: results must be a non-empty array")
    return payload


def parse_searxng_response(payload: dict[str, Any]) -> tuple[str, tuple[DiscoveredUrl, ...]]:
    query = str(payload["query"])
    discovered: list[DiscoveredUrl] = []
    for item in payload["results"]:
        if not isinstance(item, dict):
            continue
        url = item.get("url")
        if not url:
            continue
        score = item.get("score")
        discovered.append(
            DiscoveredUrl(
                url=str(url),
                title=str(item.get("title") or ""),
                engine=str(item.get("engine") or "unknown"),
                score=float(score) if score is not None else None,
                category=str(item.get("category")) if item.get("category") is not None else None,
            )
        )
    if not discovered:
        raise ValueError("SearXNG response contained no usable result URLs")
    return query, tuple(discovered)


def query_searxng_live(
    query: str,
    *,
    base_url: str = DEFAULT_SEARXNG_URL,
    categories: list[str] | None = None,
    timeout: float = 30.0,
    client: httpx.Client | None = None,
) -> dict[str, Any]:
    params: dict[str, str] = {"q": query, "format": "json"}
    if categories:
        params["categories"] = ",".join(categories)
    url = f"{base_url.rstrip('/')}/search?{urlencode(params)}"
    owned = client is None
    http = client or httpx.Client(timeout=timeout)
    try:
        response = http.get(url)
        response.raise_for_status()
        payload = response.json()
    finally:
        if owned:
            http.close()
    if not isinstance(payload, dict):
        raise ValueError("SearXNG live response must be a JSON object")
    payload.setdefault("query", query)
    return payload


def make_query_spec_id(job_id: str, query: str, engine: str = SEARCH_ENGINE) -> str:
    digest = hashlib.sha256(f"{job_id}|{query}|{engine}".encode()).hexdigest()[:16]
    return f"qs_{digest}"


def insert_query_spec(conn: psycopg.Connection, record: QuerySpecRecord) -> bool:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO query_specs (
                query_spec_id,
                job_id,
                text,
                engine,
                params
            )
            VALUES (%s, %s, %s, %s, %s::jsonb)
            ON CONFLICT (query_spec_id) DO NOTHING
            RETURNING query_spec_id
            """,
            (
                record.query_spec_id,
                record.job_id,
                record.text,
                record.engine,
                json.dumps(record.params),
            ),
        )
        return cur.fetchone() is not None


def _fetch_task_id(job_id: str, query_spec_id: str, url: str) -> str:
    digest = hashlib.sha256(f"{job_id}|{query_spec_id}|{url}".encode()).hexdigest()[:12]
    return f"tsk_fetch_{digest}"


def _fetch_idempotency_key(query_spec_id: str, url: str) -> str:
    digest = hashlib.sha256(f"fetch|{query_spec_id}|{url}".encode()).hexdigest()
    return f"sha256:{digest}"


def _task_provenance(*, config_hash: str, created_at: str) -> dict[str, str]:
    return {
        "schema_version": "task.v1",
        "config_hash": config_hash,
        "created_at": created_at,
    }


def _mark_discovered_url_task(conn: psycopg.Connection, task_id: str) -> None:
    conn.execute(
        "UPDATE tasks SET task_kind = %s WHERE task_id = %s",
        ("discovered_url", task_id),
    )


def persist_discovered_urls(
    conn: psycopg.Connection,
    *,
    job_id: str,
    query_spec_id: str,
    urls: tuple[DiscoveredUrl, ...],
    config_hash: str,
    created_at: str,
    enqueue_fetch: bool = True,
) -> tuple[str, ...]:
    task_ids: list[str] = []
    provenance = _task_provenance(config_hash=config_hash, created_at=created_at)
    for discovered in urls:
        task_id = _fetch_task_id(job_id, query_spec_id, discovered.url)
        task_ids.append(task_id)
        insert_lineage_edge_idempotent(
            conn,
            child_kind="task",
            child_id=task_id,
            parent_kind="query_spec",
            parent_id=query_spec_id,
            relation="discovered_from",
            version_tag=CODE_VERSION,
        )
        idempotency_key = _fetch_idempotency_key(query_spec_id, discovered.url)
        if enqueue_fetch:
            try:
                with conn.transaction():
                    enqueue_ready_task(
                        conn,
                        task_id=task_id,
                        job_id=job_id,
                        lane="http",
                        idempotency_key=idempotency_key,
                        payload_ref=discovered.url,
                        provenance=provenance,
                    )
                    _mark_discovered_url_task(conn, task_id)
            except psycopg.errors.UniqueViolation:
                continue
            continue
        inserted = insert_task_idempotent(
            conn,
            task_id=task_id,
            job_id=job_id,
            lane="http",
            idempotency_key=idempotency_key,
            payload_ref=discovered.url,
            provenance=provenance,
        )
        if inserted:
            _mark_discovered_url_task(conn, task_id)
    return tuple(task_ids)


def _build_query_spec(
    *,
    job_id: str,
    query: str,
    params: dict[str, Any],
) -> QuerySpecRecord:
    return QuerySpecRecord(
        query_spec_id=make_query_spec_id(job_id, query),
        job_id=job_id,
        text=query,
        engine=SEARCH_ENGINE,
        params=params,
    )


def run_search_from_fixture(
    conn: psycopg.Connection,
    *,
    job_id: str,
    config_hash: str,
    created_at: str,
    fixture_path: Path | None = None,
    query: str | None = None,
    enqueue_fetch: bool = True,
) -> SearchResult:
    path = fixture_path
    if path is None:
        if query is None:
            raise ValueError("fixture_path or query is required")
        path = fixture_path_for_query(query)
    path = path.resolve()
    payload = load_searxng_fixture(path)
    parsed_query, urls = parse_searxng_response(payload)
    params = {
        "source": "fixture",
        "fixture_file": path.name,
        "code_version": CODE_VERSION,
    }
    record = _build_query_spec(job_id=job_id, query=parsed_query, params=params)
    insert_query_spec(conn, record)
    fetch_task_ids = persist_discovered_urls(
        conn,
        job_id=job_id,
        query_spec_id=record.query_spec_id,
        urls=urls,
        config_hash=config_hash,
        created_at=created_at,
        enqueue_fetch=enqueue_fetch,
    )
    return SearchResult(
        query_spec=record,
        urls=urls,
        fetch_task_ids=fetch_task_ids,
        source=f"fixture:{path.name}",
    )


def run_search_live(
    conn: psycopg.Connection,
    *,
    job_id: str,
    query: str,
    config_hash: str,
    created_at: str,
    base_url: str = DEFAULT_SEARXNG_URL,
    categories: list[str] | None = None,
    enqueue_fetch: bool = True,
    client: httpx.Client | None = None,
) -> SearchResult:
    payload = query_searxng_live(
        query,
        base_url=base_url,
        categories=categories,
        client=client,
    )
    parsed_query, urls = parse_searxng_response(payload)
    params = {
        "source": "live",
        "base_url": base_url.rstrip("/"),
        "code_version": CODE_VERSION,
    }
    if categories:
        params["categories"] = categories
    record = _build_query_spec(job_id=job_id, query=parsed_query, params=params)
    insert_query_spec(conn, record)
    fetch_task_ids = persist_discovered_urls(
        conn,
        job_id=job_id,
        query_spec_id=record.query_spec_id,
        urls=urls,
        config_hash=config_hash,
        created_at=created_at,
        enqueue_fetch=enqueue_fetch,
    )
    return SearchResult(
        query_spec=record,
        urls=urls,
        fetch_task_ids=fetch_task_ids,
        source=f"live:{base_url.rstrip('/')}",
    )
