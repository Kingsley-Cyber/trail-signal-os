"""Postgres connection settings from environment (.env loaded by Makefile)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import psycopg


@dataclass(frozen=True)
class PostgresSettings:
    host: str
    port: int
    user: str
    password: str
    dbname: str

    @property
    def conninfo(self) -> str:
        return (
            f"host={self.host} port={self.port} dbname={self.dbname} "
            f"user={self.user} password={self.password}"
        )


def load_postgres_settings() -> PostgresSettings:
    env_file = Path(__file__).resolve().parents[2] / ".env"
    if env_file.is_file():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                continue
            key, value = stripped.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip())

    password = os.environ.get("POSTGRES_PASSWORD")
    if not password:
        raise RuntimeError(
            "POSTGRES_PASSWORD is required (set in .env or environment before migrate)"
        )
    return PostgresSettings(
        host=os.environ.get("POSTGRES_HOST", "localhost"),
        port=int(os.environ.get("POSTGRES_PORT", "5433")),
        user=os.environ.get("POSTGRES_USER", "trail_signal"),
        password=password,
        dbname=os.environ.get("POSTGRES_DB", "trail_signal"),
    )


def connect(settings: PostgresSettings | None = None) -> psycopg.Connection:
    cfg = settings or load_postgres_settings()
    return psycopg.connect(cfg.conninfo)
