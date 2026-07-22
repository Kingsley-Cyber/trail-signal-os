"""Publish unpublished outbox rows to Redis Streams (sole cp:* XADD site)."""

from __future__ import annotations

import json
from typing import Any

import psycopg


def _redis_fields(payload: dict, *, event_id: int) -> dict[str, str]:
    fields: dict[str, str] = {"outbox_event_id": str(event_id)}
    for key, value in payload.items():
        if isinstance(value, (dict, list)):
            fields[key] = json.dumps(value)
        else:
            fields[key] = str(value)
    return fields


def _xadd_to_stream(redis_client: Any, stream_name: str, fields: dict[str, str]) -> str:
    """XADD to cp:* — must remain inside dispatcher/ (guard 4 static lint)."""
    return redis_client.xadd(stream_name, fields)


def _mark_published(conn: psycopg.Connection, event_id: int) -> bool:
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE outbox_events
            SET published_at = NOW()
            WHERE event_id = %s
              AND published_at IS NULL
            RETURNING event_id
            """,
            (event_id,),
        )
        return cur.fetchone() is not None


def publish_outbox_event(
    conn: psycopg.Connection,
    redis_client: Any,
    *,
    event_id: int,
    stream_name: str,
    payload: dict,
) -> bool:
    """Idempotent publish: skip when already marked published."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT published_at
            FROM outbox_events
            WHERE event_id = %s
            FOR UPDATE
            """,
            (event_id,),
        )
        row = cur.fetchone()
    if row is None:
        return False
    if row[0] is not None:
        return False

    fields = _redis_fields(payload, event_id=event_id)
    _xadd_to_stream(redis_client, stream_name, fields)
    return _mark_published(conn, event_id)


def publish_pending_outbox(
    conn: psycopg.Connection,
    redis_client: Any,
    *,
    batch_size: int = 100,
) -> int:
    """Claim unpublished outbox rows and publish them in order."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT event_id, stream_name, payload
            FROM outbox_events
            WHERE published_at IS NULL
            ORDER BY created_at, event_id
            FOR UPDATE SKIP LOCKED
            LIMIT %s
            """,
            (batch_size,),
        )
        rows = cur.fetchall()

    published = 0
    for event_id, stream_name, payload in rows:
        if publish_outbox_event(
            conn,
            redis_client,
            event_id=event_id,
            stream_name=stream_name,
            payload=payload,
        ):
            published += 1
    return published
