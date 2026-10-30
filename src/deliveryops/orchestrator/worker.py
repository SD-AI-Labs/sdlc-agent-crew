"""Outbox worker: delivers gate notifications without losing them.

Runs as its own process (`deliveryops-worker`) next to the API. It is a poller, not a subscriber
daemon: every event is a durable row, so a restart replays anything undelivered (ADR 0003).

Delivery is a log line, plus a Slack post when DELIVERYOPS_SLACK_WEBHOOK_URL is set. A gate
nobody hears about is a gate nobody answers, so the webhook is the pilot's real notification
channel - and it is treated as untrusted infrastructure: failures are logged and swallowed,
never allowed to kill the poll loop or block an event's delivery marking.

When PORT is set (Cloud Run injects it), a minimal HTTP health server runs alongside the
poller: Cloud Run will not start a service container that never listens on $PORT. It serves
exactly one route, GET /health, reporting poll liveness from shared state. With no PORT
(local runs, docker compose) nothing listens and the worker is a pure poller, as before.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

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


class HealthState:
    """Poll liveness, written by the poll loop and read by the health server.

    Plain attributes only: under CPython a reader may see a slightly stale
    snapshot mid-update, which is fine for a liveness signal - it is never
    used to make a delivery decision.
    """

    def __init__(self) -> None:
        self.started_at = time.time()
        self.polls = 0
        self.last_poll_ok_at: float | None = None
        self.last_error: str | None = None
        self.delivered_total = 0

    def record_poll(self, delivered: int) -> None:
        self.polls += 1
        self.last_poll_ok_at = time.time()
        self.last_error = None
        self.delivered_total += delivered

    def record_failure(self, error: Exception) -> None:
        self.polls += 1
        self.last_error = f"{type(error).__name__}: {error}"[:200]

    def snapshot(self) -> dict:
        return {
            "status": "ok",
            "uptime_seconds": round(time.time() - self.started_at, 1),
            "polls": self.polls,
            "last_poll_ok_at": self.last_poll_ok_at,
            "last_error": self.last_error,
            "delivered_total": self.delivered_total,
        }


def start_health_server(state: HealthState, port: int) -> ThreadingHTTPServer:
    """Serve GET /health on `port` in a daemon thread; returns the server."""

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler's name
            if self.path != "/health":
                self.send_error(404)
                return
            body = json.dumps(state.snapshot()).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args) -> None:  # keep health checks out of the log
            pass

    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True,
                     name="worker-health").start()
    return server


def main() -> int:
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
    from ..config import validate_config

    validate_config()
    outbox = build_outbox()
    interval = float(os.getenv("DELIVERYOPS_WORKER_INTERVAL", "1.0"))
    log.info("outbox worker started (backend=%s)", type(outbox).__name__)
    state = HealthState()
    port = os.getenv("PORT", "")
    if port:
        start_health_server(state, int(port))
        log.info("health server listening on port %s (GET /health)", port)
    backoff = interval
    try:
        while True:
            try:
                n = run_once(outbox)
            except Exception as e:
                # A dead database or one bad row must not kill the poller: the
                # outbox is durable, so log, back off, and poll again. Progress
                # already marked delivered stays marked.
                state.record_failure(e)
                log.exception("worker poll failed; retrying in %.1fs", backoff)
                time.sleep(backoff)
                backoff = min(backoff * 2, _MAX_BACKOFF)
                continue
            state.record_poll(n)
            backoff = interval
            if n:
                log.info("delivered %d event(s)", n)
            time.sleep(interval)
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
