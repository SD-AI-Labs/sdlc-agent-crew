# ADR 0003: Durable outbox (Postgres) for handoffs, not Redis Streams or LISTEN/NOTIFY

Status: accepted

## Context
The pipeline must tell the world that a gate is open. Options considered:

- Postgres `LISTEN/NOTIFY`: simplest, but notifications are not persisted. If no listener is
  connected when the gate opens, the notification is gone, and an approval gate that loses its
  notification is a stuck run.
- Redis Streams: durable and well understood, but adds a second stateful service, a second
  consistency domain (run state in Postgres, events in Redis) and operational surface that this
  project does not need at its scale.
- Postgres outbox table: one row per event, written in the same database as the state change.
  Delivery is a poll, so restarts replay instead of losing.

## Decision
Events are rows in `outbox(run_id, type, stage, payload, delivered_at)`, with a unique index on
`(run_id, type, stage)` so a retried state write cannot duplicate an event. `OutboxWorker` polls
undelivered rows per topic and marks them delivered. The local/offline path uses an in-memory
`Outbox` with the same interface.

## Consequences
- Delivery is at-least-once and ordered by insert id; subscribers must be idempotent. The unique
  index makes the producer side naturally idempotent.
- If skip-locked consumers are needed later, `SELECT ... FOR UPDATE SKIP LOCKED` is a local change,
  not a new service.
- Trade-off accepted: polling adds latency (default 1s) and a tiny bit of load. For human approval
  gates, one second is invisible.
