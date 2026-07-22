"""Apply ordered SQL migrations from db/migrations/."""

from __future__ import annotations

from pathlib import Path

from db.repositories.connection import connect, load_postgres_settings

REPO_ROOT = Path(__file__).resolve().parents[2]
MIGRATIONS_DIR = REPO_ROOT / "db" / "migrations"


def migrations_dir() -> Path:
    return MIGRATIONS_DIR


def _migration_files() -> list[Path]:
    return sorted(MIGRATIONS_DIR.glob("*.sql"))


def _applied_versions(conn) -> set[str]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT EXISTS (
                SELECT 1
                FROM information_schema.tables
                WHERE table_schema = 'public'
                  AND table_name = 'schema_migrations'
            )
            """
        )
        if not cur.fetchone()[0]:
            return set()
        cur.execute("SELECT version FROM schema_migrations")
        return {row[0] for row in cur.fetchall()}


def apply_migrations(conn=None) -> list[str]:
    """Apply pending migrations. Returns newly applied version names."""
    own_conn = conn is None
    if own_conn:
        conn = connect(load_postgres_settings())

    applied_now: list[str] = []
    try:
        applied = _applied_versions(conn)
        for path in _migration_files():
            version = path.name
            if version in applied:
                continue
            sql = path.read_text(encoding="utf-8")
            with conn.transaction():
                conn.execute(sql)
                conn.execute(
                    """
                    INSERT INTO schema_migrations (version)
                    VALUES (%s)
                    ON CONFLICT (version) DO NOTHING
                    """,
                    (version,),
                )
            applied_now.append(version)
        if own_conn:
            conn.commit()
    finally:
        if own_conn:
            conn.close()

    return applied_now


def main() -> int:
    settings = load_postgres_settings()
    applied = apply_migrations()
    if applied:
        print(
            f"Applied {len(applied)} migration(s) to "
            f"{settings.dbname}@{settings.host}:{settings.port}: "
            + ", ".join(applied)
        )
    else:
        print(
            f"No pending migrations for "
            f"{settings.dbname}@{settings.host}:{settings.port}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
