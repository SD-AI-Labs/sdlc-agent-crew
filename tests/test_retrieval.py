"""RAG credibility pack: embedder config, hybrid retrieval, cutoff, fallback, recall gate."""
from __future__ import annotations

import json
import sys
import types
from pathlib import Path

from deliveryops.evals.gates import RETRIEVAL_THRESHOLDS, retrieval_check
from deliveryops.evals.runner import run_retrieval_eval
from deliveryops.guardrails.policy import evaluate
from deliveryops.knowledge.embedder import HashEmbedder, LangChainEmbedder, get_embedder
from deliveryops.knowledge.ingest import ingest
from deliveryops.knowledge.library import DOCS
from deliveryops.knowledge.retriever import (
    Hit,
    HybridRetriever,
    LexicalRetriever,
    ResilientRetriever,
    get_retriever,
)
from deliveryops.llm.pricing import embedding_cost_usd

GOLDEN = Path(__file__).resolve().parent.parent / "evals" / "golden" / "retrieval.json"


def _pairs() -> list[dict]:
    return json.loads(GOLDEN.read_text())


# ---------- golden set ----------

def test_golden_pairs_reference_real_documents():
    pairs = _pairs()
    assert 15 <= len(pairs) <= 20
    known = {d.id for d in DOCS}
    for pair in pairs:
        assert pair["expected"] in known, pair
        assert pair["query"].strip()


# ---------- embedder configuration ----------

def test_embedder_factory_defaults_to_hash(monkeypatch):
    monkeypatch.delenv("DELIVERYOPS_EMBEDDER", raising=False)
    assert isinstance(get_embedder(), HashEmbedder)


def test_langchain_embedder_uses_env_config(monkeypatch):
    calls = {}

    class FakeClient:
        def embed_query(self, text):
            return [1.0, 2.0]

    def fake_init_embeddings(model, provider=None):
        calls["model"] = model
        calls["provider"] = provider
        return FakeClient()

    embeddings_mod = types.ModuleType("langchain.embeddings")
    embeddings_mod.init_embeddings = fake_init_embeddings
    langchain_mod = types.ModuleType("langchain")
    monkeypatch.setitem(sys.modules, "langchain", langchain_mod)
    monkeypatch.setitem(sys.modules, "langchain.embeddings", embeddings_mod)

    monkeypatch.setenv("DELIVERYOPS_EMBEDDER", "langchain")
    monkeypatch.setenv("DELIVERYOPS_EMBED_PROVIDER", "acme")
    monkeypatch.setenv("DELIVERYOPS_EMBED_MODEL", "m1")
    embedder = get_embedder()
    assert isinstance(embedder, LangChainEmbedder)
    assert embedder.name == "langchain-acme-m1"
    assert embedder.embed("hello") == [1.0, 2.0]
    assert calls == {"model": "m1", "provider": "acme"}


def test_embedding_pricing_entry():
    assert embedding_cost_usd("text-embedding-3-small", 1_000_000) == 0.02
    assert embedding_cost_usd("hash-ngram-1024", 1_000_000) == 0.0
    assert embedding_cost_usd("some-unknown-model", 1_000_000) == 0.0


# ---------- hybrid retrieval ----------

def _stub(name, hits):
    return type(name, (), {"name": name, "search": lambda self, q, k=3: hits[:k]})()


def test_hybrid_rrf_rewards_agreement():
    a = Hit("kb-001", "a.md", "A", "x", 5.0)
    b = Hit("kb-002", "b.md", "B", "x", 4.0)
    c = Hit("kb-003", "c.md", "C", "x", 3.0)
    hybrid = HybridRetriever(_stub("lex", [a, b]), _stub("vec", [b, c]))
    ranked = [h.doc_id for h in hybrid.search("q", k=3)]
    # kb-002 is on both lists and beats kb-001, which tops only one.
    assert ranked == ["kb-002", "kb-001", "kb-003"]


def test_cutoff_is_all_or_nothing():
    weak = Hit("kb-001", "a.md", "A", "x", 0.01)
    hybrid = HybridRetriever(_stub("lex", [weak]), _stub("vec", []), min_score=0.5)
    assert hybrid.search("q") == []
    # And below the bar the answer is empty, not the weak hit.
    lex = LexicalRetriever(min_score=1000.0)
    assert lex.search("definition of done") == []


# ---------- resilience ----------

def test_db_failure_degrades_to_lexical():
    class Boom:
        name = "boom"

        def search(self, query, k=3):
            raise ConnectionError("database is down")

    resilient = ResilientRetriever(Boom(), LexicalRetriever())
    hits = resilient.search("rollback policy", k=3)
    assert hits and all(isinstance(h, Hit) for h in hits)


def test_retriever_factory_modes(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("DELIVERYOPS_RETRIEVER", raising=False)
    assert isinstance(get_retriever(), LexicalRetriever)

    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@localhost:5432/db")
    monkeypatch.setenv("DELIVERYOPS_RETRIEVER", "hybrid")
    assert isinstance(get_retriever(), ResilientRetriever)
    monkeypatch.setenv("DELIVERYOPS_RETRIEVER", "pgvector")
    assert isinstance(get_retriever(), ResilientRetriever)


# ---------- ingest hygiene ----------

class _FakeCursor:
    def __init__(self):
        self.executed: list[tuple[str, tuple]] = []
        self.rowcount = 0

    def execute(self, sql, params=()):
        self.executed.append((sql, params))
        self.rowcount = 0

    def fetchone(self):
        return (0,)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class _FakeConn:
    def __init__(self, cur):
        self._cur = cur

    def cursor(self):
        return self._cur

    def commit(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def test_ingest_namespaces_rows_and_deletes_stale_sources(monkeypatch):
    cur = _FakeCursor()
    fake = types.ModuleType("psycopg")
    fake.connect = lambda dsn: _FakeConn(cur)
    monkeypatch.setitem(sys.modules, "psycopg", fake)

    n = ingest("postgresql://fake", embedder=HashEmbedder())
    assert n == len(DOCS)

    inserts = [p for sql, p in cur.executed if sql.startswith("INSERT")]
    assert len(inserts) == len(DOCS)
    assert all(p[3] == "hash-ngram-1024" for p in inserts)

    stale_deletes = [p for sql, p in cur.executed if "NOT (source = ANY" in sql]
    assert stale_deletes == [("hash-ngram-1024", [f"{d.source}#{d.id}" for d in DOCS])]


# ---------- retrieval eval gate ----------

def test_lexical_recall_meets_the_gate():
    report = run_retrieval_eval(GOLDEN, LexicalRetriever())
    assert report["recalls"]["recall@3"] >= RETRIEVAL_THRESHOLDS["recall@3"]
    assert report["recalls"]["recall@1"] >= RETRIEVAL_THRESHOLDS["recall@1"]
    assert report["passed"]


def test_retrieval_gate_check():
    assert retrieval_check({"recall@1": 1.0, "recall@3": 1.0}) == []
    failures = retrieval_check({"recall@1": 0.0, "recall@3": 0.5})
    assert len(failures) == 2


def test_retrieval_cli(monkeypatch, capsys):
    from deliveryops.evals.runner import main

    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("DELIVERYOPS_RETRIEVER", raising=False)
    rc = main(["--retrieval", "--retrieval-golden", str(GOLDEN)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "retriever=lexical" in out
    assert "recall@1" in out


# ---------- no-grounding signal to the design gate ----------

def test_design_gate_flags_missing_grounding():
    decision = evaluate("design", {"confidence": 0.9, "open_questions": 0, "grounding_hits": 0})
    assert not decision.escalate
    assert str(decision.risk) == "medium"
    assert any("no confident knowledge grounding" in r for r in decision.reasons)


def test_design_gate_silent_when_grounded():
    decision = evaluate("design", {"confidence": 0.9, "open_questions": 0, "grounding_hits": 2})
    assert decision.reasons == ()


def test_pipeline_design_gate_sees_empty_search():
    from .conftest import make_pipeline

    pipeline = make_pipeline(search=lambda q, k: [])
    view = pipeline.start("Login page returns 500 after password reset", "details")
    view = pipeline.resume(view.run_id, "sukhdev", True, "ok", role="senior")
    assert view.awaiting["stage"] == "design"
    assert any("no confident knowledge grounding" in r for r in view.awaiting["reasons"])
