"""Connection pooling: Postgres components check connections out of one shared
psycopg_pool pool per DSN instead of paying a fresh psycopg.connect() handshake
per statement.

The fakes follow the pattern of test_retrieval.py: psycopg and psycopg_pool are
replaced in sys.modules. psycopg.connect is wired to explode, so any code path
that bypasses the pool fails the test instead of silently working.
"""
from __future__ import annotations

import sys
import types

import pytest

import deliveryops.db.repository as repository
from deliveryops.db.repository import PostgresRepository, RunRecord
from deliveryops.orchestrator.events import PostgresOutbox

DSN = "postgresql://fake"


class _FakeCursor:
    def __init__(self) -> None:
        self.executed: list = []
        self.rowcount = 0

    def execute(self, sql, params=None):
        self.executed.append((sql, params))

    def fetchone(self):
        return None

    def fetchall(self):
        return []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class _FakeConnection:
    def __init__(self, pool: _FakePool) -> None:
        self._pool = pool
        self._cur = _FakeCursor()

    def cursor(self):
        return self._cur

    def commit(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self._pool.returned += 1
        return False


class _FakePool:
    instances: list[_FakePool] = []

    def __init__(self, conninfo, **kwargs) -> None:
        self.conninfo = conninfo
        self.kwargs = kwargs
        self.checkouts = 0
        self.returned = 0
        _FakePool.instances.append(self)

    def connection(self):
        self.checkouts += 1
        return _FakeConnection(self)


@pytest.fixture
def fake_pg(monkeypatch):
    _FakePool.instances = []
    repository._POOLS.clear()
    pool_mod = types.ModuleType("psycopg_pool")
    pool_mod.ConnectionPool = _FakePool
    monkeypatch.setitem(sys.modules, "psycopg_pool", pool_mod)
    pg = types.ModuleType("psycopg")

    def _no_direct_connect(dsn):
        raise AssertionError("psycopg.connect called directly - the pool was bypassed")

    pg.connect = _no_direct_connect
    monkeypatch.setitem(sys.modules, "psycopg", pg)
    yield _FakePool
    repository._POOLS.clear()


def test_repository_checks_connections_out_of_the_pool(fake_pg):
    repo = PostgresRepository(DSN)
    repo.save(RunRecord(id="r1", title="t", description="d"))
    repo.append_audit(repository.AuditEntry("r1", 1.0, "system", "submitted", {}))
    assert len(fake_pg.instances) == 1
    pool = fake_pg.instances[0]
    assert pool.conninfo == DSN
    assert pool.checkouts == 2
    assert pool.returned == 2  # every checkout was handed back


def test_repository_and_outbox_share_one_pool_per_dsn(fake_pg):
    PostgresRepository(DSN).save(RunRecord(id="r1", title="t", description="d"))
    outbox = PostgresOutbox(DSN)
    outbox.publish("r1", "gate.opened", "intake")
    outbox.pending()
    assert len(fake_pg.instances) == 1
    assert fake_pg.instances[0].checkouts == 3


def test_a_second_dsn_gets_its_own_pool(fake_pg):
    PostgresRepository(DSN).save(RunRecord(id="r1", title="t", description="d"))
    PostgresRepository("postgresql://other").save(
        RunRecord(id="r2", title="t", description="d"))
    assert len(fake_pg.instances) == 2


def test_pool_sizes_come_from_env(fake_pg, monkeypatch):
    monkeypatch.setenv("DELIVERYOPS_DB_POOL_MIN", "2")
    monkeypatch.setenv("DELIVERYOPS_DB_POOL_MAX", "5")
    PostgresRepository(DSN).save(RunRecord(id="r1", title="t", description="d"))
    assert fake_pg.instances[0].kwargs == {"min_size": 2, "max_size": 5}


def test_pool_sizes_default_to_1_and_8(fake_pg):
    PostgresRepository(DSN).save(RunRecord(id="r1", title="t", description="d"))
    assert fake_pg.instances[0].kwargs == {"min_size": 1, "max_size": 8}


def test_a_non_integer_pool_size_is_a_loud_error(fake_pg, monkeypatch):
    monkeypatch.setenv("DELIVERYOPS_DB_POOL_MAX", "lots")
    with pytest.raises(ValueError, match="DELIVERYOPS_DB_POOL_MAX"):
        PostgresRepository(DSN).save(RunRecord(id="r1", title="t", description="d"))
