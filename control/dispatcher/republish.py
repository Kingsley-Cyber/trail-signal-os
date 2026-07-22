"""Republish outbox payloads after Redis loss (reconciler path until N9)."""

from __future__ import annotations

from typing import Any

import psycopg

from control.dispatcher.publish import _redis_fields, _xadd_to_stream


def stream_contains_event(redis_client: Any, stream_name: str, event_id: int) -> bool:
    """Return True when the stream already carries this outbox event id."""
    target = str(event_id)
    messages = redis_client.xrange(stream_name, min="-", max="+")
    for _message_id, fields in messages:
        if fields.get("outbox_event_id") == target:
            return True
    return False


def republish_missing_stream_messages(
    conn: psycopg.Connection,
    redis_client: Any,
    *,
    batch_size: int = 100,
) -> int:
    """Re-XADD published outbox rows whose stream entry is missing (restart-Redis)."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT oe.event_id, oe.stream_name, oe.payload
            FROM outbox_events oe
            JOIN tasks t ON t.task_id = oe.task_id
            WHERE oe.published_at IS NOT NULL
              AND t.state = 'READY'
            ORDER BY oe.created_at, oe.event_id
            LIMIT %s
            """,
            (batch_size,),
        )
        rows = cur.fetchall()

    republished = 0
    for event_id, stream_name, payload in rows:
        if stream_contains_event(redis_client, stream_name, event_id):
            continue
        fields = _redis_fields(payload, event_id=event_id)
        _xadd_to_stream(redis_client, stream_name, fields)
        republished += 1
    return republished
