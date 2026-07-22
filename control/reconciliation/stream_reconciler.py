"""Repair Redis stream gaps for published outbox rows (restart-Redis path)."""

from __future__ import annotations

from typing import Any

import psycopg

from control.dispatcher.republish import republish_missing_stream_messages


def republish_missing_streams(
    conn: psycopg.Connection,
    redis_client: Any,
    *,
    batch_size: int = 100,
) -> int:
    """Re-XADD published outbox rows whose stream entry is missing."""
    return republish_missing_stream_messages(
        conn,
        redis_client,
        batch_size=batch_size,
    )
