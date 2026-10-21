"""MCP tool layer and the knowledge retriever."""
from __future__ import annotations

import asyncio
import json

from deliveryops.knowledge.embedder import HashEmbedder, to_pgvector
from deliveryops.knowledge.library import DOCS
from deliveryops.knowledge.retriever import LexicalRetriever, get_retriever
from deliveryops.mcp_server import server as mcp


def _call(name: str, args: dict):
    """Call an MCP tool the way a client does, and return parsed JSON payloads.

    The SDK emits one text block per returned value, so a list-returning tool arrives as several
    blocks and a single-object tool as one.
    """
    res = asyncio.run(mcp.server.call_tool(name, args))
    blocks = list(getattr(res, "content", []) or [])
    parsed = []
    for block in blocks:
        text = getattr(block, "text", None)
        if text is None:
            continue
        try:
            parsed.append(json.loads(text))
        except ValueError:
            parsed.append(text)
    if not parsed:
        return None
    return parsed[0] if len(parsed) == 1 else parsed


def test_server_lists_the_tools_the_crew_needs():
    tools = asyncio.run(mcp.server.list_tools())
    names = {t.name for t in tools}
    assert {"get_ticket", "search_knowledge", "read_repo_file", "validate_patch",
            "list_repo_files", "run_status"} <= names


def test_get_ticket_returns_seeded_ticket():
    ticket = _call("get_ticket", {"ticket_id": "OPS-101"})
    assert ticket["id"] == "OPS-101" and "500" in ticket["title"]


def test_missing_ticket_returns_an_error_not_an_exception():
    assert "not found" in _call("get_ticket", {"ticket_id": "OPS-999"})["error"]


def test_create_and_list_tickets():
    created = _call("create_ticket", {"title": "Test ticket", "description": "Body text here"})
    listing = _call("list_tickets", {})
    assert any(t["id"] == created["id"] for t in listing)


def test_read_repo_file_blocks_traversal_and_secrets():
    assert "error" in _call("read_repo_file", {"path": "../../etc/passwd"})
    assert "error" in _call("read_repo_file", {"path": ".env"})


def test_read_repo_file_reads_within_root(tmp_path, monkeypatch):
    (tmp_path / "hello.txt").write_text("hi there")
    monkeypatch.setattr(mcp, "REPO_ROOT", tmp_path)
    assert _call("read_repo_file", {"path": "hello.txt"})["text"] == "hi there"


def test_validate_patch_accepts_and_rejects_diffs():
    good = _call("validate_patch", {"diff": "--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n-a\n+b\n"})
    assert good["valid"] and good["files"] == ["x.py"]
    bad = _call("validate_patch", {"diff": "nonsense"})
    assert bad["valid"] is False


def test_validate_patch_reports_secret_findings():
    diff = ("--- a/x.py\n+++ b/x.py\n@@ -1 +1,2 @@\n a = 1\n"
            "+api_key = \"sk-live-abcdef123456\"\n")
    assert _call("validate_patch", {"diff": diff})["findings"]


def test_search_knowledge_returns_citations():
    query = "rollback release policy and security review"
    raw = _call("search_knowledge", {"query": query, "k": 3})
    hits = raw if isinstance(raw, list) else [raw]
    assert len(hits) >= 2, "a multi-term query should retrieve more than one document"
    assert all("#kb-" in h["citation"] for h in hits)


def test_retrieval_ranks_the_relevant_document_first():
    hits = LexicalRetriever().search("rollback plan for a release", k=1)
    assert hits[0].doc_id in {"kb-006", "kb-005"}


def test_every_library_document_is_retrievable():
    r = LexicalRetriever()
    for doc in DOCS:
        assert r.search(f"{doc.title} {doc.tags[0]}", k=3), doc.id


def test_embedder_is_deterministic_and_normalised():
    a, b = HashEmbedder(), HashEmbedder()
    v = a.embed("rollback policy")
    assert v == b.embed("rollback policy")
    assert len(v) == 1024
    assert 0.99 < sum(x * x for x in v) ** 0.5 < 1.01


def test_pgvector_hits_do_not_duplicate_the_doc_id():
    from deliveryops.knowledge.retriever import Hit

    hit = Hit(doc_id="kb-004", source="security/review-checklist.md",
              title="Security review checklist", content="x", score=0.9)
    assert hit.citation() == "security/review-checklist.md#kb-004"


def test_pgvector_literal_format():
    assert to_pgvector([0.5, -0.25]) == "[0.500000,-0.250000]"


def test_outbox_backend_follows_database_url(monkeypatch):
    from deliveryops.orchestrator.events import Outbox, PostgresOutbox
    from deliveryops.orchestrator.worker import build_outbox

    monkeypatch.delenv("DATABASE_URL", raising=False)
    assert isinstance(build_outbox(), Outbox)
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@localhost:5432/db")
    assert isinstance(build_outbox(), PostgresOutbox)


def test_retriever_factory_defaults_to_lexical(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("DELIVERYOPS_RETRIEVER", raising=False)
    assert isinstance(get_retriever(), LexicalRetriever)
