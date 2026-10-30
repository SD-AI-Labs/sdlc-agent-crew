"""Worker notifications: the Slack webhook payload, and failure behaviour.

The webhook is stubbed at urllib.request.urlopen (the same injection point the
Jira/GitHub adapters use), so no test touches the network.
"""
from __future__ import annotations

import json
import urllib.request

import pytest

from deliveryops.orchestrator import worker
from deliveryops.orchestrator.events import Event, Outbox

WEBHOOK = "https://hooks.slack.com/services/T000/B000/xxx"


def _event() -> Event:
    return Event("run-1", "gate.opened", "design",
                 {"risk": "high", "escalate": True,
                  "reasons": ["patch touches gated paths: .github/"]})


class _FakeResponse:
    def read(self):
        return b"ok"

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


@pytest.fixture
def captured(monkeypatch):
    posts: list = []

    def fake_urlopen(req, timeout=None):
        posts.append((req, timeout))
        return _FakeResponse()

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    return posts


def test_notify_posts_the_gate_event_as_json(monkeypatch, captured):
    monkeypatch.setenv("DELIVERYOPS_SLACK_WEBHOOK_URL", WEBHOOK)
    worker.notify(_event())
    assert len(captured) == 1
    req, _timeout = captured[0]
    assert req.full_url == WEBHOOK
    body = json.loads(req.data)
    assert body["event"] == "gate.opened"
    assert body["run_id"] == "run-1"
    assert body["stage"] == "design"
    assert body["risk"] == "high"
    assert body["escalate"] is True
    assert body["reasons"] == ["patch touches gated paths: .github/"]
    assert "run-1" in body["text"] and "design" in body["text"]


def test_notify_without_a_webhook_stays_log_only(monkeypatch, captured):
    monkeypatch.delenv("DELIVERYOPS_SLACK_WEBHOOK_URL", raising=False)
    worker.notify(_event())
    assert captured == []


def test_a_webhook_failure_is_swallowed_and_logged(monkeypatch, caplog):
    monkeypatch.setenv("DELIVERYOPS_SLACK_WEBHOOK_URL", WEBHOOK)

    def boom(req, timeout=None):
        raise OSError("connection refused")

    monkeypatch.setattr(urllib.request, "urlopen", boom)
    with caplog.at_level("ERROR", logger="deliveryops.worker"):
        worker.notify(_event())  # must not raise
    assert "slack notification failed" in caplog.text


def test_run_once_delivers_pending_events_to_slack(monkeypatch, captured):
    monkeypatch.setenv("DELIVERYOPS_SLACK_WEBHOOK_URL", WEBHOOK)
    outbox = Outbox()
    outbox.publish("run-1", "gate.opened", "design",
                   {"risk": "high", "escalate": True, "reasons": ["r"]})
    assert worker.run_once(outbox) == 1
    assert len(captured) == 1


def test_a_failing_poll_is_retried_with_backoff_not_a_crash(monkeypatch):
    """The main loop's contract: run_once raising means log + sleep + retry."""
    sleeps: list[float] = []
    calls = {"n": 0}

    def flaky(outbox):
        calls["n"] += 1
        if calls["n"] < 3:
            raise RuntimeError("database is down")
        raise KeyboardInterrupt  # the only way out of the loop in this test

    monkeypatch.setattr(worker, "run_once", flaky)
    monkeypatch.setattr(worker.time, "sleep", sleeps.append)
    monkeypatch.setenv("DELIVERYOPS_WORKER_INTERVAL", "0.5")
    assert worker.main() == 0
    assert calls["n"] == 3
    assert sleeps == [0.5, 1.0]  # backoff doubled, then the interrupt landed
