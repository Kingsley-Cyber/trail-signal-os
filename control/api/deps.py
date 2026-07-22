"""Database connection dependency for control API routes."""

from __future__ import annotations

from collections.abc import Iterator

import psycopg
from fastapi import Request

from db.repositories.connection import connect


def get_db(request: Request) -> Iterator[psycopg.Connection]:
    conn = connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
