"""Worker health endpoint: Cloud Run will not start a service container that
never listens on $PORT, so when PORT is set the poller serves GET /health.

The server is exercised over a real socket on an ephemeral port - the one
place a stub would prove nothing, since binding and serving are the point.
"""
from __future__ import annotations

import json
import urllib.request

import pytest

from deliveryops.orchestrator import worker


@pytest.fixture
def health_url():
    state = worker.HealthState()
    server = worker.start_health_server(state, 0)  # port 0: kernel picks one
    port = server.server_address[1]
    yield state, f"http://127.0.0.1:{port}/health"
    server.shutdown()
    server.server_close()


def _get(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=5) as resp:
        assert resp.status == 200
        return json.loads(resp.read())


def test_health_reports_a_fresh_worker(health_url):
    _state, url = health_url
    body = _get(url)
    assert body["status"] == "ok"
    assert body["polls"] == 0
    assert body["last_poll_ok_at"] is None
    assert body["last_error"] is None
    assert body["delivered_total"] == 0


def test_health_reflects_poll_outcomes(health_url):
    state, url = health_url
    state.record_poll(3)
    state.record_poll(0)
    body = _get(url)
    assert body["polls"] == 2
    assert body["delivered_total"] == 3
    assert body["last_poll_ok_at"] is not None
    assert body["last_error"] is None

    state.record_failure(RuntimeError("database is down"))
    body = _get(url)
    assert body["last_error"] == "RuntimeError: database is down"
    # A failure does not erase the last success or the delivered count.
    assert body["last_poll_ok_at"] is not None
    assert body["delivered_total"] == 3


def test_unknown_paths_are_404(health_url):
    _state, url = health_url
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        urllib.request.urlopen(url.replace("/health", "/metrics"), timeout=5)
    assert excinfo.value.code == 404
