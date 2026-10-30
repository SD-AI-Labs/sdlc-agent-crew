"""Outbox worker: delivers gate notifications without losing them.

Runs as its own process (`deliveryops-worker`) next to the API. It is a poller, not a subscriber
daemon: every event is a durable row, so a restart replays anything undelivered (ADR 0003).
"""
from __future__ import annotations

import logging
import os
import time

from .events import Outbox, OutboxWorker, PostgresOutbox

log = logging.getLogger("deliveryops.worker")
TOPICS = ("gate.opened", "gate.decided", "ticket.submitted")


def build_outbox() -> Outbox | PostgresOutbox:
    dsn = os.getenv("DATABASE_URL", "")
    return PostgresOutbox(dsn) if dsn else Outbox()


def notify(event) -> None:
    """Where a real system would post to Slack/Jira. Logged so a demo shows the handoff."""
    log.info("event %s run=%s stage=%s escalate=%s", event.type, event.run_id, event.stage,
             event.payload.get("escalate", False))


def run_once(outbox) -> int:
    delivered = 0
    for topic in TOPICS:
        worker = OutboxWorker(outbox, topic=topic)
        for event in worker.poll():
            notify(event)
            if isinstance(outbox, PostgresOutbox):
                outbox.mark_delivered(event)
            delivered += 1
    return delivered


def main() -> int:
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
    from ..config import validate_config

    validate_config()
    outbox = build_outbox()
    interval = float(os.getenv("DELIVERYOPS_WORKER_INTERVAL", "1.0"))
    log.info("outbox worker started (backend=%s)", type(outbox).__name__)
    try:
        while True:
            n = run_once(outbox)
            if n:
                log.info("delivered %d event(s)", n)
            time.sleep(interval)
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
