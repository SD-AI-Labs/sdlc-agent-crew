"""Retrieval over the process library: lexical by default, pgvector when a database is present.

Both implementations return the same `Hit` shape, so the design agent does not care which one is
wired in, and tests can assert grounding without a database.
"""
from __future__ import annotations

import math
import os
import re
from dataclasses import dataclass

from .embedder import HashEmbedder, to_pgvector
from .library import DOCS, Doc

_TOKEN = re.compile(r"[a-z0-9]+")

_STOP = frozenset(
    "the a an and or of to in for on is are be with when that this it as at by from we you".split()
)


def _tokens(text: str) -> list[str]:
    return [t for t in _TOKEN.findall(text.lower()) if t not in _STOP]


@dataclass(frozen=True)
class Hit:
    doc_id: str
    source: str
    title: str
    content: str
    score: float

    def citation(self) -> str:
        return f"{self.source}#{self.doc_id}"


class LexicalRetriever:
    """BM25-ish scoring over title, tags and body. No network, no database, deterministic."""

    name = "lexical"

    def __init__(self, docs: tuple[Doc, ...] = DOCS) -> None:
        self._docs = docs
        self._tokens = {d.id: _tokens(f"{d.title} {d.title} {' '.join(d.tags)} {d.content}")
                        for d in docs}
        self._df: dict[str, int] = {}
        for toks in self._tokens.values():
            for t in set(toks):
                self._df[t] = self._df.get(t, 0) + 1

    def search(self, query: str, k: int = 3) -> list[Hit]:
        q = set(_tokens(query))
        if not q:
            return []
        n = len(self._docs)
        scored: list[Hit] = []
        for doc in self._docs:
            toks = self._tokens[doc.id]
            if not toks:
                continue
            score = 0.0
            for term in q:
                tf = toks.count(term)
                if not tf:
                    continue
                df = self._df.get(term, 1)
                idf = math.log(1 + (n - df + 0.5) / (df + 0.5))
                score += idf * (tf * 2.2) / (tf + 1.2)
            if score > 0:
                scored.append(Hit(doc.id, doc.source, doc.title, doc.content, round(score, 4)))
        scored.sort(key=lambda h: (-h.score, h.doc_id))
        return scored[:k]


class PgVectorRetriever:
    """Cosine search over knowledge_chunks. Requires DATABASE_URL and ingested chunks."""

    name = "pgvector"

    def __init__(self, dsn: str, embedder: HashEmbedder | None = None) -> None:
        self._dsn = dsn
        self._embedder = embedder or HashEmbedder()

    def search(self, query: str, k: int = 3) -> list[Hit]:
        import psycopg

        vec = to_pgvector(self._embedder.embed(query))
        sql = (
            "SELECT source, content, 1 - (embedding <=> %s::vector) AS score "
            "FROM knowledge_chunks WHERE embedding IS NOT NULL "
            "ORDER BY embedding <=> %s::vector LIMIT %s"
        )
        with psycopg.connect(self._dsn) as conn, conn.cursor() as cur:
            cur.execute(sql, (vec, vec, k))
            rows = cur.fetchall()
        hits: list[Hit] = []
        for source, content, score in rows:
            # Stored as "path#kb-id"; split so citation() does not append the id twice.
            path, _, doc_id = source.rpartition("#")
            hits.append(Hit(doc_id=doc_id or path, source=path or source, title=path or source,
                            content=content, score=round(float(score), 4)))
        return hits


def get_retriever() -> LexicalRetriever | PgVectorRetriever:
    """DELIVERYOPS_RETRIEVER=lexical|pgvector (pgvector needs DATABASE_URL)."""
    choice = os.getenv("DELIVERYOPS_RETRIEVER", "lexical").lower()
    dsn = os.getenv("DATABASE_URL", "")
    if choice == "pgvector" and dsn:
        return PgVectorRetriever(dsn)
    return LexicalRetriever()
