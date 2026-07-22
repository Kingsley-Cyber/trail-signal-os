"""Reconciler first-pass readiness gate for /readyz (doc 09 §4)."""

from __future__ import annotations

import asyncio
import threading
from typing import Any, Callable

import psycopg

from control.reconciliation import run_reconciler_pass
from db.repositories.connection import connect


class ReconcilerReadiness:
    """Tracks whether the reconciler has completed its first pass."""

    def __init__(
        self,
        *,
        connect_fn: Callable[[], psycopg.Connection] = connect,
        redis_connect_fn: Callable[[], Any] | None = None,
        run_pass_fn: Callable[..., Any] = run_reconciler_pass,
    ) -> None:
        self._connect_fn = connect_fn
        self._redis_connect_fn = redis_connect_fn
        self._run_pass_fn = run_pass_fn
        self._lock = threading.Lock()
        self._first_pass_done = False
        self._first_pass_error: str | None = None

    @property
    def is_ready(self) -> bool:
        with self._lock:
            return self._first_pass_done

    @property
    def first_pass_error(self) -> str | None:
        with self._lock:
            return self._first_pass_error

    def run_first_pass(self) -> None:
        """Run one reconciler pass synchronously; mark ready only on success."""
        try:
            conn = self._connect_fn()
            try:
                redis_client = None
                if self._redis_connect_fn is not None:
                    try:
                        redis_client = self._redis_connect_fn()
                    except Exception:
                        redis_client = None
                self._run_pass_fn(conn, redis_client)
            finally:
                conn.close()
        except Exception as exc:
            with self._lock:
                self._first_pass_done = False
                self._first_pass_error = str(exc)
            return

        with self._lock:
            self._first_pass_done = True
            self._first_pass_error = None

    def mark_ready(self) -> None:
        """Test hook: mark readiness without running the reconciler."""
        with self._lock:
            self._first_pass_done = True
            self._first_pass_error = None

    async def start_first_pass(self) -> None:
        await asyncio.to_thread(self.run_first_pass)
