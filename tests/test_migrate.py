"""Migrations runner: schema.sql applies once as version 1, a second run is a
no-op, and main() wires the DSN through to the checkpointer setup.

Driven by a stateful fake connection (same sys.modules pattern as
test_retrieval.py): the fake remembers which versions the INSERT recorded, so
the version check on the second run sees real state.
"""
from __future__ import annotations

import sys
import types

import pytest

from deliveryops.db import migrate as migrate_mod


class _FakeCursor:
    def __init__(self, conn: _FakeConn) -> None:
        self._conn = conn

    def execute(self, sql, params=None):
        self._conn.executed.append((sql, params))
        if sql.startswith("INSERT INTO schema_migrations"):
            self._conn.applied.append(params[0])

    def fetchall(self):
        return [(v,) for v in self._conn.applied]

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class _FakeConn:
    def __init__(self) -> None:
        self.executed: list = []
        self.applied: list[int] = []

    def cursor(self):
        return _FakeCursor(self)

    def commit(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def test_first_run_applies_the_schema_and_records_version_1():
    conn = _FakeConn()
    assert migrate_mod.migrate(conn) == [1]
    blobs = [sql for sql, _ in conn.executed if "CREATE TABLE IF NOT EXISTS runs" in sql]
    assert len(blobs) == 1
    assert conn.applied == [1]


def test_second_run_is_a_noop():
    conn = _FakeConn()
    migrate_mod.migrate(conn)
    executed_before = len(conn.executed)
    assert migrate_mod.migrate(conn) == []
    later = [sql for sql, _ in conn.executed[executed_before:]]
    assert not any("CREATE TABLE IF NOT EXISTS runs" in sql for sql in later)
    assert conn.applied == [1]


def test_main_migrates_and_sets_up_the_checkpointer(monkeypatch):
    conn = _FakeConn()
    fake = types.ModuleType("psycopg")
    fake.connect = lambda dsn: conn
    monkeypatch.setitem(sys.modules, "psycopg", fake)
    monkeypatch.setenv("DATABASE_URL", "postgresql://fake")
    seen: list[str] = []
    monkeypatch.setattr(migrate_mod, "setup_checkpointer", seen.append)
    assert migrate_mod.main() == 0
    assert seen == ["postgresql://fake"]
    assert conn.applied == [1]


def test_main_without_database_url_fails_loudly(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    with pytest.raises(SystemExit):
        migrate_mod.main()
