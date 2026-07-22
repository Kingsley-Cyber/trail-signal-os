"""Index worker — evidence.v1 → Qdrant vectors with ts_ collection prefix (N21)."""

from __future__ import annotations

import json
import math
import os
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional, Protocol

import psycopg

from db.repositories.persist_artifact import DEFAULT_STORAGE_ROOT, REPO_ROOT
from harness.gateway import LLMGateway
from workers.enrich_worker import EVIDENCE_SCHEMA_VERSION, validate_evidence_v1

EMBED_ROLE = "embed.primary"
EMBED_CASSETTE_KIND = "embed"
CODE_VERSION = "index_worker-1.0.0"
COLLECTION_PREFIX = "ts_"
EVIDENCE_COLLECTION_SUFFIX = "evidence"
DEFAULT_QDRANT_URL = "http://127.0.0.1:6333"
DEFAULT_VECTOR_SIZE = 768

EmbedFn = Callable[[list[str], Optional[dict[str, Any]]], list[list[float]]]


@dataclass(frozen=True)
class IndexResult:
    record_id: str
    collection_name: str
    point_id: str
    vector_size: int
    indexed: bool
    replayed: bool


@dataclass(frozen=True)
class IndexSearchHit:
    record_id: str
    score: float
    payload: dict[str, Any]
    point_id: str


class QdrantClientProtocol(Protocol):
    def collection_exists(self, collection_name: str) -> bool: ...

    def create_collection(self, collection_name: str, vectors_config: Any) -> bool: ...

    def upsert(self, collection_name: str, points: list[Any]) -> Any: ...

    def search(
        self,
        collection_name: str,
        query_vector: list[float],
        *,
        limit: int = 10,
        with_payload: bool = True,
    ) -> list[Any]: ...


def qdrant_url() -> str:
    return os.environ.get("QDRANT_URL", DEFAULT_QDRANT_URL).strip()


def collection_name(suffix: str) -> str:
    """Return a Qdrant collection name with the ts_ prefix (environment_profile §3)."""
    cleaned = suffix.strip().removeprefix(COLLECTION_PREFIX)
    if not cleaned:
        raise ValueError("collection suffix must be non-empty")
    return f"{COLLECTION_PREFIX}{cleaned}"


def evidence_collection_name() -> str:
    return collection_name(EVIDENCE_COLLECTION_SUFFIX)


def point_id_for_record(record_id: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, record_id))


def build_embed_replay_request(evidence: dict[str, Any]) -> dict[str, Any]:
    return {
        "record_id": evidence["record_id"],
        "role": EMBED_ROLE,
        "content_hash": evidence["content_hash"],
    }


def build_index_text(evidence: dict[str, Any]) -> str:
    parts: list[str] = [str(evidence.get("observation") or "")]
    for field in ("pain_points", "desired_outcomes", "product_terms", "quotes"):
        values = evidence.get(field)
        if isinstance(values, list):
            parts.extend(str(value) for value in values if value)
    entities = evidence.get("entities")
    if isinstance(entities, list):
        for entity in entities:
            if isinstance(entity, dict) and entity.get("name"):
                parts.append(str(entity["name"]))
    return "\n".join(part.strip() for part in parts if part and part.strip())


def build_point_payload(evidence: dict[str, Any]) -> dict[str, Any]:
    return {
        "record_id": evidence["record_id"],
        "evidence_type": evidence["evidence_type"],
        "polarity": evidence["polarity"],
        "observation": evidence["observation"],
        "content_hash": evidence["content_hash"],
        "source_domain": (evidence.get("source") or {}).get("domain"),
        "schema_version": evidence["schema_version"],
        "indexed_by": CODE_VERSION,
    }


def default_embed_fn(
    gateway: LLMGateway,
    texts: list[str],
    replay_request: Optional[dict[str, Any]] = None,
) -> tuple[list[list[float]], bool]:
    if not texts:
        return [], False
    result = gateway.embed(
        EMBED_ROLE,
        texts,
        cassette_kind=EMBED_CASSETTE_KIND,
        replay_request=replay_request,
    )
    return result.vectors, bool(result.replayed)


@dataclass(frozen=True)
class VectorConfig:
    size: int


def ensure_evidence_collection(
    client: QdrantClientProtocol,
    *,
    collection: str,
    vector_size: int,
) -> None:
    if client.collection_exists(collection):
        return
    if isinstance(client, InMemoryQdrantClient):
        client.create_collection(
            collection_name=collection,
            vectors_config=VectorConfig(size=vector_size),
        )
        return
    from qdrant_client.models import Distance, VectorParams

    client.create_collection(
        collection_name=collection,
        vectors_config=VectorParams(size=vector_size, distance=Distance.COSINE),
    )


def index_evidence_v1(
    evidence: dict[str, Any],
    *,
    client: QdrantClientProtocol,
    gateway: Optional[LLMGateway] = None,
    embed_fn: Optional[EmbedFn] = None,
    replay_request: Optional[dict[str, Any]] = None,
    collection: Optional[str] = None,
) -> IndexResult:
    """Embed and upsert one evidence.v1 record into Qdrant."""
    validate_evidence_v1(evidence)
    target_collection = collection or evidence_collection_name()
    if not target_collection.startswith(COLLECTION_PREFIX):
        raise ValueError(f"collection {target_collection!r} must use {COLLECTION_PREFIX!r} prefix")

    text = build_index_text(evidence)
    request = replay_request or build_embed_replay_request(evidence)
    replayed = False
    if embed_fn is not None:
        vectors = embed_fn([text], request)
    else:
        if gateway is None:
            raise ValueError("gateway or embed_fn is required")
        vectors, replayed = default_embed_fn(gateway, [text], request)
    if not vectors or not vectors[0]:
        raise ValueError("embedding returned no vectors")

    vector = [float(value) for value in vectors[0]]
    ensure_evidence_collection(client, collection=target_collection, vector_size=len(vector))

    point_id = point_id_for_record(evidence["record_id"])
    payload = build_point_payload(evidence)
    if isinstance(client, InMemoryQdrantClient):
        client.upsert(
            collection_name=target_collection,
            points=[
                type(
                    "PointStruct",
                    (),
                    {"id": point_id, "vector": vector, "payload": payload},
                )()
            ],
        )
    else:
        from qdrant_client.models import PointStruct

        client.upsert(
            collection_name=target_collection,
            points=[
                PointStruct(
                    id=point_id,
                    vector=vector,
                    payload=payload,
                )
            ],
        )
    return IndexResult(
        record_id=evidence["record_id"],
        collection_name=target_collection,
        point_id=point_id,
        vector_size=len(vector),
        indexed=True,
        replayed=replayed,
    )


def _hit_from_scored_point(point: Any) -> IndexSearchHit:
    payload = dict(getattr(point, "payload", None) or {})
    record_id = str(payload.get("record_id") or "")
    return IndexSearchHit(
        record_id=record_id,
        score=float(getattr(point, "score", 0.0)),
        payload=payload,
        point_id=str(getattr(point, "id", "")),
    )


def search_evidence(
    query: str,
    *,
    client: QdrantClientProtocol,
    gateway: Optional[LLMGateway] = None,
    embed_fn: Optional[EmbedFn] = None,
    replay_request: Optional[dict[str, Any]] = None,
    collection: Optional[str] = None,
    limit: int = 5,
) -> list[IndexSearchHit]:
    """Semantic search over indexed evidence.v1 vectors."""
    target_collection = collection or evidence_collection_name()
    if not target_collection.startswith(COLLECTION_PREFIX):
        raise ValueError(f"collection {target_collection!r} must use {COLLECTION_PREFIX!r} prefix")
    if not client.collection_exists(target_collection):
        return []

    request = replay_request or {"query": query, "role": EMBED_ROLE}
    replayed = False
    if embed_fn is not None:
        vectors = embed_fn([query], request)
    else:
        if gateway is None:
            raise ValueError("gateway or embed_fn is required")
        vectors, replayed = default_embed_fn(gateway, [query], request)
    if not vectors or not vectors[0]:
        return []

    vector = [float(value) for value in vectors[0]]
    raw_hits = client.search(
        collection_name=target_collection,
        query_vector=vector,
        limit=limit,
        with_payload=True,
    )
    _ = replayed
    return [_hit_from_scored_point(point) for point in raw_hits]


def _resolve_storage_path(storage_uri: str, *, storage_root: Path) -> Path:
    if storage_uri.startswith("file://"):
        relative = storage_uri.removeprefix("file://")
        candidate = (REPO_ROOT / relative).resolve()
        if candidate.is_file():
            return candidate
    path = Path(storage_uri)
    if path.is_file():
        return path.resolve()
    under_root = (storage_root / storage_uri.removeprefix("file://")).resolve()
    if under_root.is_file():
        return under_root
    raise FileNotFoundError(f"artifact bytes missing for {storage_uri}")


def load_evidence_v1(
    conn: psycopg.Connection,
    record_id: str,
    *,
    storage_root: Optional[Path] = None,
) -> dict[str, Any]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT artifact_kind, storage_uri, schema_version
            FROM artifacts
            WHERE artifact_id = %s
            LIMIT 1
            """,
            (record_id,),
        )
        row = cur.fetchone()
    if row is None:
        raise ValueError(f"unknown record_id {record_id!r}")
    artifact_kind, storage_uri, schema_version = row
    if artifact_kind != EVIDENCE_SCHEMA_VERSION and schema_version != EVIDENCE_SCHEMA_VERSION:
        raise ValueError(f"{record_id!r} is not evidence.v1")

    root = (storage_root or DEFAULT_STORAGE_ROOT).resolve()
    path = _resolve_storage_path(str(storage_uri), storage_root=root)
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{path}: evidence payload must be an object")
    validate_evidence_v1(payload)
    return payload


def run_index_task(
    conn: psycopg.Connection,
    *,
    record_id: str,
    index_task_id: str,
    client: QdrantClientProtocol,
    gateway: Optional[LLMGateway] = None,
    embed_fn: Optional[EmbedFn] = None,
    storage_root: Optional[Path] = None,
    replay_request: Optional[dict[str, Any]] = None,
) -> IndexResult:
    """Load persisted evidence.v1 and index it into Qdrant."""
    evidence = load_evidence_v1(conn, record_id, storage_root=storage_root)
    result = index_evidence_v1(
        evidence,
        client=client,
        gateway=gateway,
        embed_fn=embed_fn,
        replay_request=replay_request,
    )
    if not evidence.get("derived_from"):
        raise ValueError("evidence.v1 must include derived_from before indexing")
    _ = index_task_id
    return result


def create_qdrant_client(*, url: Optional[str] = None) -> Any:
    from qdrant_client import QdrantClient

    return QdrantClient(url=url or qdrant_url())


def deterministic_embed(texts: list[str], _replay_request: Optional[dict[str, Any]] = None) -> list[list[float]]:
    """Offline deterministic vectors for tests (not used in production)."""
    vectors: list[list[float]] = []
    for text in texts:
        digest = uuid.uuid5(uuid.NAMESPACE_OID, text).int
        values = []
        seed = digest
        for _ in range(DEFAULT_VECTOR_SIZE):
            seed = (seed * 1664525 + 1013904223) % (2**32)
            values.append((seed / (2**31)) - 1.0)
        vectors.append(values)
    return vectors


class InMemoryQdrantClient:
    """Minimal Qdrant client for offline tests."""

    def __init__(self) -> None:
        self._vector_sizes: dict[str, int] = {}
        self._points: dict[str, dict[str, Any]] = {}

    def collection_exists(self, collection_name: str) -> bool:
        return collection_name in self._vector_sizes

    def create_collection(self, collection_name: str, vectors_config: Any) -> bool:
        size = int(getattr(vectors_config, "size", DEFAULT_VECTOR_SIZE))
        self._vector_sizes[collection_name] = size
        self._points.setdefault(collection_name, {})
        return True

    def upsert(self, collection_name: str, points: list[Any]) -> bool:
        bucket = self._points.setdefault(collection_name, {})
        for point in points:
            point_id = str(getattr(point, "id", ""))
            bucket[point_id] = {
                "id": point_id,
                "vector": list(getattr(point, "vector", [])),
                "payload": dict(getattr(point, "payload", {}) or {}),
            }
        return True

    @staticmethod
    def _cosine(a: list[float], b: list[float]) -> float:
        dot = sum(x * y for x, y in zip(a, b, strict=False))
        norm_a = math.sqrt(sum(x * x for x in a))
        norm_b = math.sqrt(sum(y * y for y in b))
        if norm_a == 0.0 or norm_b == 0.0:
            return 0.0
        return dot / (norm_a * norm_b)

    def search(
        self,
        collection_name: str,
        query_vector: list[float],
        *,
        limit: int = 10,
        with_payload: bool = True,
    ) -> list[Any]:
        bucket = self._points.get(collection_name, {})
        scored = []
        for point_id, point in bucket.items():
            score = self._cosine(query_vector, point["vector"])
            scored.append(
                type(
                    "ScoredPoint",
                    (),
                    {
                        "id": point_id,
                        "score": score,
                        "payload": point["payload"] if with_payload else {},
                    },
                )()
            )
        scored.sort(key=lambda item: item.score, reverse=True)
        return scored[:limit]