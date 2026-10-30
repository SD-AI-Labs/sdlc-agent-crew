"""Run, audit, escalation and telemetry persistence.

In-memory by default so the whole system runs and tests without a database; Postgres when
DATABASE_URL is set. Both satisfy the same protocol, which is what the pipeline and API use.
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from psycopg_pool import ConnectionPool

from ..observability.tracer import Span


@dataclass
class RunRecord:
    id: str
    title: str
    description: str
    status: str = "submitted"
    stage: str = ""
    state: dict = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)
    outcome: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class AuditEntry:
    run_id: str
    ts: float
    actor: str
    action: str
    detail: dict

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class Escalation:
    run_id: str
    stage: str
    risk: str
    reasons: tuple[str, ...]
    ts: float = 0.0


class Repository(Protocol):
    def save(self, record: RunRecord) -> None: ...
    def get(self, run_id: str) -> RunRecord | None: ...
    def list(self, limit: int = 50) -> list[RunRecord]: ...
    def append_audit(self, entry: AuditEntry) -> None: ...
    def audit(self, run_id: str) -> list[AuditEntry]: ...
    def record_call(self, span: Span) -> None: ...
    def calls(self, run_id: str | None = None) -> list[Span]: ...
    def record_escalation(self, esc: Escalation) -> None: ...
    def escalations(self, run_id: str | None = None) -> list[Escalation]: ...


class InMemoryRepository:
    def __init__(self) -> None:
        self._runs: dict[str, RunRecord] = {}
        self._audit: dict[str, list[AuditEntry]] = {}
        self._calls: list[Span] = []
        self._escalations: list[Escalation] = []

    def save(self, record: RunRecord) -> None:
        self._runs[record.id] = record

    def get(self, run_id: str) -> RunRecord | None:
        return self._runs.get(run_id)

    def list(self, limit: int = 50) -> list[RunRecord]:
        return sorted(self._runs.values(), key=lambda r: r.created_at, reverse=True)[:limit]

    def append_audit(self, entry: AuditEntry) -> None:
        # Idempotent, mirroring the Postgres ON CONFLICT DO NOTHING: replaying a run's
        # audit (e.g. a fresh Pipeline persisting after a restart) must not duplicate.
        entries = self._audit.setdefault(entry.run_id, [])
        if entry not in entries:
            entries.append(entry)

    def audit(self, run_id: str) -> list[AuditEntry]:
        return list(self._audit.get(run_id, []))

    def record_call(self, span: Span) -> None:
        self._calls.append(span)

    def calls(self, run_id: str | None = None) -> list[Span]:
        return [c for c in self._calls if run_id is None or c.run_id == run_id]

    def record_escalation(self, esc: Escalation) -> None:
        self._escalations.append(esc)

    def escalations(self, run_id: str | None = None) -> list[Escalation]:
        return [e for e in self._escalations if run_id is None or e.run_id == run_id]


# One connection pool per DSN, shared by every Postgres-backed component in the
# process (repository, outbox). Created lazily on first use: constructing a
# repository must stay side-effect free, exactly as it was when each call opened
# its own connection.
_POOLS: dict[str, ConnectionPool] = {}


def _pool_size(env_name: str, default: int) -> int:
    raw = os.getenv(env_name, "")
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        raise ValueError(f"{env_name} must be an integer, got {raw!r}") from None


def get_pool(dsn: str) -> ConnectionPool:
    """The shared pool for a DSN. Sizes come from DELIVERYOPS_DB_POOL_MIN /
    DELIVERYOPS_DB_POOL_MAX (defaults 1/8); the deployment budget is
    API instances x pool max, which must stay under the database's
    max_connections (see deploy/terraform/<target>)."""
    pool = _POOLS.get(dsn)
    if pool is None:
        from psycopg_pool import ConnectionPool

        pool = ConnectionPool(
            dsn,
            min_size=_pool_size("DELIVERYOPS_DB_POOL_MIN", 1),
            max_size=_pool_size("DELIVERYOPS_DB_POOL_MAX", 8),
        )
        _POOLS[dsn] = pool
    return pool


class PostgresRepository:
    """Stores runs as JSONB state plus normalised audit, telemetry and escalation rows."""

    def __init__(self, dsn: str) -> None:
        self._dsn = dsn

    def _connect(self):
        # A context manager over a pooled connection: the same `with` shape the
        # per-call psycopg.connect() had, without a fresh handshake per statement.
        return get_pool(self._dsn).connection()

    def save(self, record: RunRecord) -> None:
        sql = (
            "INSERT INTO runs (id, title, description, status, stage, state, outcome, "
            "created_at, updated_at) VALUES (%s,%s,%s,%s,%s,%s,%s,to_timestamp(%s),now()) "
            "ON CONFLICT (id) DO UPDATE SET status=EXCLUDED.status, stage=EXCLUDED.stage, "
            "state=EXCLUDED.state, outcome=EXCLUDED.outcome, updated_at=now()"
        )
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                sql,
                (
                    record.id,
                    record.title,
                    record.description,
                    record.status,
                    record.stage,
                    json.dumps(record.state),
                    record.outcome,
                    record.created_at,
                ),
            )
            conn.commit()

    def get(self, run_id: str) -> RunRecord | None:
        sql = (
            "SELECT id, title, description, status, stage, state, outcome, "
            "EXTRACT(EPOCH FROM created_at) FROM runs WHERE id = %s"
        )
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(sql, (run_id,))
            row = cur.fetchone()
        if not row:
            return None
        rid, title, desc, status, stage, state, outcome, created = row
        return RunRecord(str(rid), title, desc, status, stage or "", state or {},
                         float(created or 0), outcome=outcome or "")

    def list(self, limit: int = 50) -> list[RunRecord]:
        sql = (
            "SELECT id, title, description, status, stage, state, outcome, "
            "EXTRACT(EPOCH FROM created_at) FROM runs ORDER BY created_at DESC LIMIT %s"
        )
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(sql, (limit,))
            rows = cur.fetchall()
        return [
            RunRecord(str(r[0]), r[1], r[2], r[3], r[4] or "", r[5] or {}, float(r[7] or 0),
                      outcome=r[6] or "")
            for r in rows
        ]

    def append_audit(self, entry: AuditEntry) -> None:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO audit_events (run_id, ts, actor, action, detail) "
                "VALUES (%s, to_timestamp(%s), %s, %s, %s) "
                "ON CONFLICT (run_id, ts, actor, action) DO NOTHING",
                (entry.run_id, entry.ts, entry.actor, entry.action, json.dumps(entry.detail)),
            )
            conn.commit()

    def audit(self, run_id: str) -> list[AuditEntry]:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT run_id, EXTRACT(EPOCH FROM ts), actor, action, detail "
                "FROM audit_events WHERE run_id = %s ORDER BY id",
                (run_id,),
            )
            rows = cur.fetchall()
        return [AuditEntry(str(r[0]), float(r[1]), r[2], r[3], r[4] or {}) for r in rows]

    def record_call(self, span: Span) -> None:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO agent_calls (run_id, agent, model, input_tokens, output_tokens, "
                "latency_ms, cost_usd, ok) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
                (span.run_id, span.agent, span.model, span.input_tokens, span.output_tokens,
                 span.latency_ms, span.cost_usd, span.ok),
            )
            conn.commit()

    def calls(self, run_id: str | None = None) -> list[Span]:
        sql = (
            "SELECT run_id, agent, model, input_tokens, output_tokens, latency_ms, cost_usd, "
            "EXTRACT(EPOCH FROM ts), ok FROM agent_calls"
        )
        args: tuple = ()
        if run_id:
            sql += " WHERE run_id = %s"
            args = (run_id,)
        sql += " ORDER BY id"
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(sql, args)
            rows = cur.fetchall()
        return [
            Span(str(r[0]), "", r[1], r[2], r[3], r[4], float(r[5]), float(r[6]), float(r[7]),
                 bool(r[8]))
            for r in rows
        ]

    def record_escalation(self, esc: Escalation) -> None:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO escalations (run_id, stage, risk, reasons) VALUES (%s,%s,%s,%s)",
                (esc.run_id, esc.stage, esc.risk, json.dumps(list(esc.reasons))),
            )
            conn.commit()

    def escalations(self, run_id: str | None = None) -> list[Escalation]:
        sql = "SELECT run_id, stage, risk, reasons FROM escalations"
        args: tuple = ()
        if run_id:
            sql += " WHERE run_id = %s"
            args = (run_id,)
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(sql, args)
            rows = cur.fetchall()
        return [Escalation(str(r[0]), r[1], r[2], tuple(r[3] or [])) for r in rows]


def get_repository() -> Repository:
    dsn = os.getenv("DATABASE_URL", "")
    if dsn:
        return PostgresRepository(dsn)
    return InMemoryRepository()
