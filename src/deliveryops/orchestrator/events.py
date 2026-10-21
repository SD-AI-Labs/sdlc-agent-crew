"""Handoff events via a Postgres outbox (or an in-memory list offline).

Why not Redis Streams or LISTEN/NOTIFY: an approval gate must not lose its notification, and
NOTIFY is fire-and-forget. An outbox row is durable, replayable, and lives in the same
transaction as the state change it describes (ADR 0003).
"""
from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field


@dataclass(frozen=True)
class Event:
    run_id: str
    type: str            # ticket.submitted | stage.completed | gate.opened | gate.decided
    stage: str
    payload: dict = field(default_factory=dict)
    ts: float = field(default_factory=time.time)
    delivered: bool = False

    def to_dict(self) -> dict:
        return asdict(self)


class Outbox:
    """In-memory outbox with the same semantics as the Postgres table."""

    def __init__(self) -> None:
        self._events: list[Event] = []

    def publish(self, run_id: str, type_: str, stage: str, payload: dict | None = None) -> Event:
        ev = Event(run_id, type_, stage, payload or {})
        self._events.append(ev)
        return ev

    def all(self) -> list[Event]:
        return list(self._events)

    def pending(self) -> list[Event]:
        return [e for e in self._events if not e.delivered]

    def has(self, run_id: str, type_: str, stage: str) -> bool:
        return any(e.run_id == run_id and e.type == type_ and e.stage == stage
                   for e in self._events)

    def mark_delivered(self, index: int) -> None:
        e = self._events[index]
        self._events[index] = Event(e.run_id, e.type, e.stage, e.payload, e.ts, True)


class PostgresOutbox:
    def __init__(self, dsn: str) -> None:
        self._dsn = dsn

    def publish(self, run_id: str, type_: str, stage: str, payload: dict | None = None) -> Event:
        import psycopg

        with psycopg.connect(self._dsn) as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO outbox (run_id, type, stage, payload) VALUES (%s,%s,%s,%s)",
                (run_id, type_, stage, json.dumps(payload or {})),
            )
            conn.commit()
        return Event(run_id, type_, stage, payload or {})

    def pending(self) -> list[Event]:
        import psycopg

        with psycopg.connect(self._dsn) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT run_id, type, stage, payload, EXTRACT(EPOCH FROM ts) FROM outbox "
                "WHERE delivered_at IS NULL ORDER BY id"
            )
            rows = cur.fetchall()
        return [Event(str(r[0]), r[1], r[2], r[3] or {}, float(r[4])) for r in rows]

    def has(self, run_id: str, type_: str, stage: str) -> bool:
        import psycopg

        with psycopg.connect(self._dsn) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM outbox WHERE run_id = %s AND type = %s AND stage = %s LIMIT 1",
                (run_id, type_, stage),
            )
            return cur.fetchone() is not None

    def mark_delivered(self, event: Event) -> None:
        import psycopg

        with psycopg.connect(self._dsn) as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE outbox SET delivered_at = now() WHERE run_id = %s AND type = %s "
                "AND stage = %s AND delivered_at IS NULL",
                (event.run_id, event.type, event.stage),
            )
            conn.commit()


class OutboxWorker:
    """Polls the outbox and hands events to subscribers. Run as its own process."""

    def __init__(self, outbox, topic: str = "gate.opened") -> None:
        self.outbox = outbox
        self.topic = topic

    def poll(self) -> list[Event]:
        """Return undelivered events for this topic. Marking delivery is the caller's job."""
        return [e for e in self.outbox.pending() if e.type == self.topic]
