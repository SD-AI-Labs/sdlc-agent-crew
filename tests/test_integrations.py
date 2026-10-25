"""Jira/GitHub adapters, checkpointer selection, and resume-after-restart.

All HTTP is scripted through injected openers - no test here touches the network, same rule as
the rest of the suite.
"""
from __future__ import annotations

import io
import json
import urllib.error

import pytest
from fastapi.testclient import TestClient
from langgraph.checkpoint.memory import InMemorySaver

from deliveryops.agents.code import CodeAgent
from deliveryops.api.main import create_app
from deliveryops.integrations.github import GitHubError, GitHubRepoReader, get_repo_reader
from deliveryops.integrations.jira import (
    JiraCloudStore,
    JiraError,
    adf_to_text,
    get_ticket_store,
)
from deliveryops.llm.base import LLMResult
from deliveryops.mcp_server.jira import TicketStore
from deliveryops.orchestrator.checkpoint import get_checkpointer

from .conftest import make_pipeline


class FakeResponse:
    def __init__(self, payload: dict | bytes):
        self._raw = payload if isinstance(payload, bytes) else json.dumps(payload).encode()

    def read(self) -> bytes:
        return self._raw

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def http_404(req, timeout=0):
    raise urllib.error.HTTPError(req.full_url, 404, "not found", {}, io.BytesIO(b"{}"))


ISSUE = {
    "key": "PROJ-123",
    "fields": {
        "summary": "Checkout fails for all users",
        "description": {"type": "doc", "version": 1, "content": [
            {"type": "paragraph", "content": [
                {"type": "text", "text": "Payment submit returns 502."}]},
            {"type": "paragraph", "content": [
                {"type": "text", "text": "Production outage."}]},
        ]},
        "reporter": {"displayName": "Sukhdev"},
        "labels": ["payments"],
        "created": "2026-10-25T10:00:00.000+0530",
    },
}

JIRA_ENV = {"JIRA_SITE": "https://example.atlassian.net", "JIRA_EMAIL": "a@b.c",
            "JIRA_API_TOKEN": "tok", "JIRA_PROJECT": "PROJ"}


# ---------- Jira ----------


def test_adf_flattens_to_plain_text():
    text = adf_to_text(ISSUE["fields"]["description"])
    assert "Payment submit returns 502." in text and "Production outage." in text


def test_jira_get_maps_issue_to_ticket():
    seen = {}

    def opener(req, timeout=0):
        seen["url"] = req.full_url
        seen["auth"] = req.get_header("Authorization")
        return FakeResponse(ISSUE)

    store = JiraCloudStore.from_env(JIRA_ENV)  # from_env then swap opener
    store._opener = opener
    ticket = store.get("PROJ-123")
    assert ticket.id == "PROJ-123"
    assert ticket.title == "Checkout fails for all users"
    assert "Payment submit returns 502." in ticket.description
    assert ticket.reporter == "Sukhdev" and ticket.labels == ["payments"]
    assert ticket.created_at > 0
    assert "/rest/api/3/issue/PROJ-123" in seen["url"]
    assert seen["auth"].startswith("Basic ")


def test_jira_get_404_returns_none():
    store = JiraCloudStore.from_env(JIRA_ENV)
    store._opener = http_404
    assert store.get("PROJ-999") is None


def test_jira_list_uses_jql_scoped_to_the_project():
    seen = {}

    def opener(req, timeout=0):
        seen["url"] = req.full_url
        return FakeResponse({"issues": [ISSUE]})

    store = JiraCloudStore.from_env(JIRA_ENV)
    store._opener = opener
    tickets = store.list()
    assert [t.id for t in tickets] == ["PROJ-123"]
    assert "search%2Fjql" in seen["url"] or "search/jql" in seen["url"]
    assert "project+%3D+PROJ" in seen["url"] or "project%20%3D%20PROJ" in seen["url"] \
        or "project = PROJ" in seen["url"]


def test_jira_create_posts_adf_and_returns_a_ticket():
    calls = []

    def opener(req, timeout=0):
        calls.append(req)
        if req.get_method() == "POST":
            return FakeResponse({"key": "PROJ-124"})
        return FakeResponse({**ISSUE, "key": "PROJ-124"})

    store = JiraCloudStore.from_env(JIRA_ENV)
    store._opener = opener
    ticket = store.create("Add CSV export", "Teams want CSV.", "me", ["reporting"])
    assert ticket.id == "PROJ-124"
    posted = json.loads(calls[0].data.decode())
    assert posted["fields"]["project"] == {"key": "PROJ"}
    assert posted["fields"]["description"]["type"] == "doc"


def test_jira_create_requires_a_project():
    store = JiraCloudStore("https://x.atlassian.net", "a@b.c", "tok")
    with pytest.raises(JiraError, match="JIRA_PROJECT"):
        store.create("t", "d")


def test_partial_jira_config_is_a_loud_error():
    with pytest.raises(JiraError, match="JIRA_API_TOKEN"):
        JiraCloudStore.from_env({"JIRA_SITE": "https://x", "JIRA_EMAIL": "a@b.c"})


def test_ticket_store_factory_defaults_to_the_mock():
    assert isinstance(get_ticket_store({}), TicketStore)
    assert isinstance(get_ticket_store(JIRA_ENV), JiraCloudStore)
    with pytest.raises(JiraError):
        get_ticket_store({"JIRA_SITE": "https://x"})


# ---------- GitHub reader ----------


TREE = {"tree": [
    {"path": "src/auth/session.py", "type": "blob"},
    {"path": "README.md", "type": "blob"},
    {"path": "src", "type": "tree"},
]}

GH_ENV = {"DELIVERYOPS_GITHUB_REPO": "sukhad3/demo-service", "GITHUB_TOKEN": "ghp_x"}


def github_opener(req, timeout=0):
    if "/git/trees/" in req.full_url:
        return FakeResponse(TREE)
    if "/contents/" in req.full_url:
        return FakeResponse(b"def login(user):\n    return user\n")
    raise AssertionError(f"unexpected url {req.full_url}")


def test_github_lists_and_reads_files():
    reader = GitHubRepoReader.from_env(GH_ENV)
    reader._opener = github_opener
    assert reader.list_files() == ["README.md", "src/auth/session.py"]
    assert reader.list_files("src") == ["src/auth/session.py"]
    got = reader.read_file("src/auth/session.py")
    assert got["text"].startswith("def login")
    assert got["bytes"] > 0


def test_github_reader_refuses_gated_and_escaping_paths():
    reader = GitHubRepoReader.from_env(GH_ENV)
    reader._opener = github_opener
    assert "refusing" in reader.read_file(".env")["error"]
    assert "refusing" in reader.read_file("certs/key.pem")["error"]
    assert "escapes" in reader.read_file("../secret")["error"]
    assert "escapes" in reader.read_file("/etc/passwd")["error"]


def test_github_reader_requires_owner_repo():
    with pytest.raises(GitHubError):
        GitHubRepoReader("no-slash")
    with pytest.raises(GitHubError):
        GitHubRepoReader.from_env({})


def test_repo_reader_factory_is_none_until_configured():
    assert get_repo_reader({}) is None
    assert isinstance(get_repo_reader(GH_ENV), GitHubRepoReader)


# ---------- code agent grounding ----------


class StubRepo:
    def list_files(self):
        return ["src/auth/session.py", "README.md"]

    def read_file(self, path):
        return {"path": path, "bytes": 30, "text": "def login(user):\n    return user\n"}


class CaptureLLM:
    name = "capture"

    def __init__(self):
        self.user = ""

    def complete(self, system, user, *, max_tokens=1000, temperature=0.0):
        self.user = user
        payload = {"diff": "--- a/src/auth/session.py\n+++ b/src/auth/session.py\n"
                           "@@ -1,1 +1,1 @@\n-def login(user):\n+def login(user, reset=False):\n",
                   "files_touched": ["src/auth/session.py"], "rationale": "r", "confidence": 0.9}
        return LLMResult(text=json.dumps(payload), model=self.name)


def test_code_agent_prompt_is_grounded_in_the_real_repo():
    llm = CaptureLLM()
    agent = CodeAgent(llm, repo=StubRepo())
    agent.run("Fix login", "Touch src/auth/session.py", ["keep sessions valid"])
    assert "Files in the target repository" in llm.user
    assert "def login(user):" in llm.user  # the named file's real contents reached the prompt


def test_code_agent_survives_an_unreachable_repo():
    class DownRepo:
        def list_files(self):
            raise GitHubError("github unreachable: boom")

        def read_file(self, path):
            return {"error": "down"}

    llm = CaptureLLM()
    result = CodeAgent(llm, repo=DownRepo()).run("Fix login", "design", ["req"])
    assert result.confidence == 0.9
    assert "unreachable" in llm.user


# ---------- checkpointer ----------


def test_checkpointer_defaults_to_memory_without_a_database():
    assert isinstance(get_checkpointer({}), InMemorySaver)
    assert isinstance(get_checkpointer({"DELIVERYOPS_CHECKPOINTER": "memory"}), InMemorySaver)


def test_postgres_checkpointer_requires_a_database_url():
    with pytest.raises(RuntimeError, match="DATABASE_URL"):
        get_checkpointer({"DELIVERYOPS_CHECKPOINTER": "postgres"})


def test_unknown_checkpointer_mode_is_rejected():
    with pytest.raises(RuntimeError, match="DELIVERYOPS_CHECKPOINTER"):
        get_checkpointer({"DELIVERYOPS_CHECKPOINTER": "redis"})


def test_run_resumes_after_restart_with_a_durable_checkpointer():
    """A new pipeline object (a 'restarted process') continues an in-flight run."""
    saver = InMemorySaver()  # stands in for the Postgres saver; same resume semantics
    first = make_pipeline(checkpointer=saver)
    view = first.start("Login page returns 500", "Users get a 500 after a password reset.")
    assert view.status == "awaiting_approval" and view.stage == "intake"

    second = make_pipeline(checkpointer=saver)  # different object = different process
    resumed = second.resume(view.run_id, "sukhdev", True, role="senior")
    assert resumed.stage == "design"
    assert resumed.decisions[0]["approver"] == "sukhdev"


# ---------- API intake through the tracker ----------


def test_api_pulls_a_ticket_from_the_configured_tracker(monkeypatch):
    import deliveryops.api.main as api_main

    class StubStore:
        def get(self, ticket_id):
            if ticket_id == "PROJ-123":
                return type("T", (), {"title": "Checkout fails for all users",
                                      "description": "Payment submit returns 502 for everyone."})
            return None

    monkeypatch.setattr(api_main, "_tickets", StubStore())
    client = TestClient(create_app(make_pipeline()))
    r = client.post("/tickets", json={"ticket_id": "PROJ-123"})
    assert r.status_code == 201, r.text
    assert r.json()["ticket"]["title"] == "Checkout fails for all users"
    assert client.post("/tickets", json={"ticket_id": "NOPE-1"}).status_code == 404


def test_api_reports_502_when_the_tracker_is_unreachable(monkeypatch):
    import deliveryops.api.main as api_main

    class DownStore:
        def get(self, ticket_id):
            raise JiraError("jira unreachable: boom")

    monkeypatch.setattr(api_main, "_tickets", DownStore())
    client = TestClient(create_app(make_pipeline()))
    r = client.post("/tickets", json={"ticket_id": "PROJ-123"})
    assert r.status_code == 502
