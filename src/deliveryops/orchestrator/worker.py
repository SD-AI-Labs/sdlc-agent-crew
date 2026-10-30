"""Outbox worker: delivers gate notifications without losing them.

Runs as its own process (`deliveryops-worker`) next to the API. It is a poller, not a subscriber
daemon: every event is a durable row, so a restart replays anything undelivered (ADR 0003).

Delivery is a log line, plus a Slack post when DELIVERYOPS_SLACK_WEBHOOK_URL is set. A gate
nobody hears about is a gate nobody answers, so the webhook is the pilot's real notification
channel - and it is treated as untrusted infrastructure: failures are logged and swallowed,
never allowed to kill the poll loop or block an event's delivery marking.
"""
from __future__ import annotations

import json
import logging
import os
import time
import urllib.request

from .events import Outbox, OutboxWorker, PostgresOutbox

log = logging.getLogger("deliveryops.worker")
TOPICS = ("gate.opened", "gate.decided", "ticket.submitted")
_MAX_BACKOFF = 30.0


def build_outbox() -> Outbox | PostgresOutbox:
    dsn = os.getenv("DATABASE_URL", "")
    return PostgresOutbox(dsn) if dsn else Outbox()


def _message(event) -> str:
    payload = event.payload or {}
    lines = [f"DeliveryOps {event.type} - run {event.run_id}, stage '{event.stage}'"]
    if payload.get("risk"):
        lines.append(f"Risk: {payload['risk']}"
                     + (" (escalated - needs an approver with authority)"
                        if payload.get("escalate") else ""))
    reasons = payload.get("reasons") or []
    if reasons:
        lines.append("Reasons: " + "; ".join(str(r) for r in reasons))
    if "approve" in payload:
        lines.append(f"Decision: {'approved' if payload['approve'] else 'rejected'}"
                     f" by {payload.get('approver', 'unknown')}")
    return "\n".join(lines)


def _slack_payload(event) -> dict:
    payload = event.payload or {}
    return {
        "text": _message(event),
        "event": event.type,
        "run_id": event.run_id,
        "stage": event.stage,
        "risk": payload.get("risk"),
        "escalate": bool(payload.get("escalate", False)),
        "reasons": [str(r) for r in (payload.get("reasons") or [])],
    }


def _post_json(url: str, payload: dict) -> None:
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=5) as resp:
        resp.read()


def notify(event) -> None:
    """Deliver one notification: always logged; posted to Slack when a webhook
    is configured. Never raises (see the module docstring)."""
    log.info("event %s run=%s stage=%s escalate=%s", event.type, event.run_id, event.stage,
             event.payload.get("escalate", False))
    url = os.getenv("DELIVERYOPS_SLACK_WEBHOOK_URL", "")
    if not url:
        return
    try:
        _post_json(url, _slack_payload(event))
    except Exception:
        log.exception("slack notification failed for run=%s stage=%s",
                      event.run_id, event.stage)


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
    backoff = interval
    try:
        while True:
            try:
                n = run_once(outbox)
            except Exception:
                # A dead database or one bad row must not kill the poller: the
                # outbox is durable, so log, back off, and poll again. Progress
                # already marked delivered stays marked.
                log.exception("worker poll failed; retrying in %.1fs", backoff)
                time.sleep(backoff)
                backoff = min(backoff * 2, _MAX_BACKOFF)
                continue
            backoff = interval
            if n:
                log.info("delivered %d event(s)", n)
            time.sleep(interval)
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
