"""Retrieval over the process library: lexical, pgvector, or a hybrid of both.

- lexical: BM25-ish scoring over the in-memory library. Offline default, no database.
- pgvector: cosine search over ingested chunks, scoped to the active embedder namespace.
- hybrid: reciprocal-rank fusion (RRF) of the lexical and vector rankings (ADR 0010).

All retrievers return the same `Hit` shape and apply an all-or-nothing confidence cutoff: when
the top score is below `min_score` the result is *no hits at all*, which the design gate then
surfaces as "no confident knowledge grounding" instead of letting weak matches masquerade as
grounding. pgvector and hybrid are wrapped in `ResilientRetriever`, so a database failure
degrades to lexical retrieval rather than a 500.
"""
from __future__ import annotations

import math
import os
import re
from dataclasses import dataclass

from .embedder import HashEmbedder, LangChainEmbedder, get_embedder, to_pgvector
from .library import DOCS, Doc

_TOKEN = re.compile(r"[a-z0-9]+")

_STOP = frozenset(
    "the a an and or of to in for on is are be with when that this it as at by from we you".split()
)

# Per-backend confidence cutoffs, measured on this library with the offline hash embedder
# (see docs/adr/0010). Override all of them with DELIVERYOPS_RETRIEVAL_MIN_SCORE.
DEFAULT_MIN_SCORE = {"lexical": 0.0, "pgvector": 0.0, "hybrid": 0.0}


def _tokens(text: str) -> list[str]:
    return [t for t in _TOKEN.findall(text.lower()) if t not in _STOP]


def _cutoff(hits: list[Hit], min_score: float) -> list[Hit]:
    """All-or-nothing grounding: below the bar, return nothing rather than weak hits."""
    if not hits:
        return hits
    if min_score > 0 and hits[0].score < min_score:
        return []
    return hits


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

    def __init__(self, docs: tuple[Doc, ...] = DOCS, min_score: float = 0.0) -> None:
        self._docs = docs
        self._min_score = min_score
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
        return _cutoff(scored[:k], self._min_score)


class PgVectorRetriever:
    """Cosine search over knowledge_chunks, scoped to the embedder that wrote the rows.

    Requires DATABASE_URL and ingested chunks. Rows embedded by a different embedder are never
    mixed in: querying across namespaces would compare incompatible vector spaces.
    """

    name = "pgvector"

    def __init__(self, dsn: str, embedder: HashEmbedder | LangChainEmbedder | None = None,
                 min_score: float = 0.0) -> None:
        self._dsn = dsn
        self._embedder = embedder or get_embedder()
        self._min_score = min_score

    def search(self, query: str, k: int = 3) -> list[Hit]:
        import psycopg

        vec = to_pgvector(self._embedder.embed(query))
        sql = (
            "SELECT source, content, 1 - (embedding <=> %s::vector) AS score "
            "FROM knowledge_chunks WHERE embedding IS NOT NULL AND embedder = %s "
            "ORDER BY embedding <=> %s::vector LIMIT %s"
        )
        with psycopg.connect(self._dsn) as conn, conn.cursor() as cur:
            cur.execute(sql, (vec, self._embedder.name, vec, k))
            rows = cur.fetchall()
        hits: list[Hit] = []
        for source, content, score in rows:
            # Stored as "path#kb-id"; split so citation() does not append the id twice.
            path, _, doc_id = source.rpartition("#")
            hits.append(Hit(doc_id=doc_id or path, source=path or source, title=path or source,
                            content=content, score=round(float(score), 4)))
        return _cutoff(hits, self._min_score)


class HybridRetriever:
    """Reciprocal-rank fusion of the lexical and vector rankings (ADR 0010).

    RRF needs no score normalisation between backends: each contributes 1/(k + rank), so a
    document that ranks well in *both* lists beats one that tops a single list.
    """

    name = "hybrid"

    def __init__(self, lexical: LexicalRetriever, vector: PgVectorRetriever,
                 rrf_k: int = 60, min_score: float = 0.0) -> None:
        self._lexical = lexical
        self._vector = vector
        self._rrf_k = rrf_k
        self._min_score = min_score

    def search(self, query: str, k: int = 3) -> list[Hit]:
        fused: dict[str, float] = {}
        best: dict[str, Hit] = {}
        for hits in (self._lexical.search(query, k=k), self._vector.search(query, k=k)):
            for rank, hit in enumerate(hits):
                fused[hit.doc_id] = fused.get(hit.doc_id, 0.0) + 1.0 / (self._rrf_k + rank + 1)
                prev = best.get(hit.doc_id)
                if prev is None or hit.score > prev.score:
                    best[hit.doc_id] = hit
        ranked = sorted(fused.items(), key=lambda kv: (-kv[1], kv[0]))[:k]
        out = [
            Hit(best[d].doc_id, best[d].source, best[d].title, best[d].content, round(s, 6))
            for d, s in ranked
        ]
        return _cutoff(out, self._min_score)


class ResilientRetriever:
    """Degrade to lexical search when the primary (database-backed) retriever fails.

    A Postgres blip or a misconfigured embedder must not 500 the design stage; grounding on the
    in-memory library is strictly better than failing the run.
    """

    def __init__(self, primary, fallback: LexicalRetriever) -> None:
        self._primary = primary
        self._fallback = fallback
        self.name = f"{primary.name}+lexical-fallback"

    def search(self, query: str, k: int = 3) -> list[Hit]:
        try:
            return self._primary.search(query, k=k)
        except Exception:
            return self._fallback.search(query, k=k)


def _min_score(backend: str) -> float:
    raw = os.getenv("DELIVERYOPS_RETRIEVAL_MIN_SCORE")
    if raw:
        return float(raw)
    return DEFAULT_MIN_SCORE[backend]


def get_retriever():
    """DELIVERYOPS_RETRIEVER=lexical|pgvector|hybrid (DB-backed modes need DATABASE_URL)."""
    choice = os.getenv("DELIVERYOPS_RETRIEVER", "lexical").lower()
    dsn = os.getenv("DATABASE_URL", "")
    if choice == "pgvector" and dsn:
        return ResilientRetriever(
            PgVectorRetriever(dsn, min_score=_min_score("pgvector")), LexicalRetriever()
        )
    if choice == "hybrid" and dsn:
        return ResilientRetriever(
            HybridRetriever(
                LexicalRetriever(), PgVectorRetriever(dsn), min_score=_min_score("hybrid")
            ),
            LexicalRetriever(),
        )
    return LexicalRetriever(min_score=_min_score("lexical"))
