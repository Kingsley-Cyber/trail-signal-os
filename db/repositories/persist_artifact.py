"""Guard 7 artifact persistence — content-addressed storage + Postgres row."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import psycopg

from guards.runtime_guards import guard7_require_provenance

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_STORAGE_ROOT = REPO_ROOT / "storage"


def _storage_path(content_hash: str, *, storage_root: Path) -> Path:
    if not content_hash.startswith("sha256:"):
        raise ValueError("content_hash must start with sha256:")
    digest = content_hash.removeprefix("sha256:")
    shard = digest[:2]
    return storage_root / "artifacts" / "json" / shard / f"{digest}.json"


def persist_artifact(
    conn: psycopg.Connection,
    *,
    artifact_id: str,
    content_hash: str,
    artifact_kind: str,
    payload: dict[str, Any],
    derived_from: list[str],
    provenance: dict[str, Any],
    created_by_task: str | None = None,
    schema_version: str | None = None,
    storage_root: Path | None = None,
) -> bool:
    """Write artifact bytes + metadata; return True when inserted, False on dedup no-op."""
    guard7_require_provenance(provenance)
    if not derived_from:
        raise ValueError("derived_from must be non-empty (LAW 2)")

    root = (storage_root or DEFAULT_STORAGE_ROOT).resolve()
    path = _storage_path(content_hash, storage_root=root)
    path.parent.mkdir(parents=True, exist_ok=True)
    serialized = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    path.write_text(serialized, encoding="utf-8")

    try:
        storage_uri = f"file://{path.relative_to(REPO_ROOT)}"
    except ValueError:
        storage_uri = path.as_uri()
    schema = schema_version or artifact_kind
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO artifacts (
                artifact_id,
                content_hash,
                storage_uri,
                media_type,
                artifact_kind,
                uncompressed_bytes,
                created_by_task,
                derived_from,
                provenance,
                schema_version
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s::jsonb, %s)
            ON CONFLICT (content_hash, COALESCE(schema_version, ''), artifact_kind)
            DO NOTHING
            RETURNING artifact_id
            """,
            (
                artifact_id,
                content_hash,
                storage_uri,
                "application/json",
                artifact_kind,
                len(serialized.encode("utf-8")),
                created_by_task,
                json.dumps(derived_from),
                json.dumps(provenance),
                schema,
            ),
        )
        return cur.fetchone() is not None
