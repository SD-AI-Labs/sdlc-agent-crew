"""Database migrations: apply schema.sql once, version-tracked, then set up the
LangGraph checkpointer tables.

Runs as the `deliveryops-migrate` console script - a one-shot Cloud Run job in
deployment, a one-shot compose service locally - so the API never boots against a
half-created database. It replaces the manual psql step deploy/README.md used to
prescribe (which still works as a fallback; the schema is re-runnable).

schema.sql is idempotent end to end (IF NOT EXISTS / OR REPLACE throughout; the
one CREATE TRIGGER is preceded by DROP TRIGGER IF EXISTS), so applying it as a
single version-1 blob is safe. The limit of that approach is *changing* a table's
shape: CREATE TABLE IF NOT EXISTS will not alter an existing table, so a future
schema change must ship as version 2 SQL here, not by editing version 1 (the
knowledge_chunks recreate note in schema.sql is that lesson, already learned).
"""
from __future__ import annotations

import os
from pathlib import Path

SCHEMA_VERSION = 1

_MIGRATIONS_DDL = (
    "CREATE TABLE IF NOT EXISTS schema_migrations ("
    "version INT PRIMARY KEY, applied_at TIMESTAMPTZ NOT NULL DEFAULT now())"
)


def _schema_sql() -> str:
    path = Path(__file__).with_name("schema.sql")
    if not path.is_file():
        raise RuntimeError(f"schema.sql not found next to migrate.py (looked at {path})")
    return path.read_text()


def applied_versions(conn) -> list[int]:
    with conn.cursor() as cur:
        cur.execute("SELECT version FROM schema_migrations ORDER BY version")
        return [int(r[0]) for r in cur.fetchall()]


def migrate(conn) -> list[int]:
    """Apply any pending versions over an open psycopg connection.

    Returns the versions applied by this call ([] when already up to date).
    Connection-based so tests can drive it with a fake; main() owns the DSN.
    """
    with conn.cursor() as cur:
        cur.execute(_MIGRATIONS_DDL)
    if SCHEMA_VERSION in applied_versions(conn):
        conn.commit()
        return []
    with conn.cursor() as cur:
        cur.execute(_schema_sql())
        cur.execute(
            "INSERT INTO schema_migrations (version) VALUES (%s)", (SCHEMA_VERSION,))
    conn.commit()
    return [SCHEMA_VERSION]


def setup_checkpointer(dsn: str) -> None:
    """Create the LangGraph PostgresSaver tables.

    The API also runs setup() at boot via get_checkpointer(); doing it here means
    a fresh database is fully formed before any service starts, and a boot-time
    setup failure surfaces in the migrate job's logs rather than the API's.
    """
    try:
        from langgraph.checkpoint.postgres import PostgresSaver
    except ImportError as e:
        raise RuntimeError(
            "checkpointer setup needs the 'postgres' extra (uv sync --extra postgres)"
        ) from e
    with PostgresSaver.from_conn_string(dsn) as saver:
        saver.setup()


def main() -> int:
    dsn = os.getenv("DATABASE_URL", "")
    if not dsn:
        raise SystemExit("deliveryops-migrate requires DATABASE_URL")
    import psycopg

    with psycopg.connect(dsn) as conn:
        applied = migrate(conn)
    setup_checkpointer(dsn)
    if applied:
        print(f"applied migrations: {applied}; checkpointer tables ready")
    else:
        print("schema already up to date; checkpointer tables ready")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
