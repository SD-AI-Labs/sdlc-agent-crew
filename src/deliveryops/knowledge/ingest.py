"""Ingest the process library into Postgres/pgvector.

Usage:  DATABASE_URL=postgresql://... deliveryops-knowledge

Embeddings come from the configured embedder (DELIVERYOPS_EMBEDDER: the offline hash embedder by
default, a real provider via langchain when configured). Rows are namespaced by embedder name so
switching embedders never mixes incompatible vectors. Sources that disappeared from the library
are deleted: stale rows would keep grounding answers on documents that no longer exist.
"""
from __future__ import annotations

import os
import sys

from ..llm.pricing import embedding_cost_usd
from .embedder import get_embedder, to_pgvector
from .library import DOCS


def ingest(dsn: str, docs=None, embedder=None) -> int:
    import psycopg

    docs = docs if docs is not None else DOCS
    embedder = embedder or get_embedder()
    refs = [f"{d.source}#{d.id}" for d in docs]
    count = 0
    with psycopg.connect(dsn) as conn, conn.cursor() as cur:
        for doc in docs:
            ref = f"{doc.source}#{doc.id}"
            payload = f"{doc.title}\n\n{doc.content}"
            cur.execute("DELETE FROM knowledge_chunks WHERE source = %s", (ref,))
            cur.execute(
                "INSERT INTO knowledge_chunks (source, content, embedding, embedder) "
                "VALUES (%s, %s, %s, %s)",
                (ref, payload, to_pgvector(embedder.embed(payload)), embedder.name),
            )
            count += 1
        cur.execute(
            "DELETE FROM knowledge_chunks WHERE embedder = %s AND NOT (source = ANY(%s))",
            (embedder.name, refs),
        )
        stale = cur.rowcount
        cur.execute("SELECT count(*) FROM knowledge_chunks WHERE embedder <> %s",
                    (embedder.name,))
        other = cur.fetchone()[0]
        conn.commit()
    if stale:
        print(f"removed {stale} stale chunk(s) no longer in the library", file=sys.stderr)
    if other:
        print(f"note: {other} chunk(s) belong to other embedders; they are ignored at query "
              f"time - re-ingest with the previous embedder to rebuild or delete them manually",
              file=sys.stderr)
    return count


def main(argv: list[str] | None = None) -> int:
    dsn = os.getenv("DATABASE_URL", "")
    if not dsn:
        print("DATABASE_URL is not set; nothing to ingest into.", file=sys.stderr)
        return 2
    embedder = get_embedder()
    tokens = sum(len(f"{d.title}\n\n{d.content}") for d in DOCS) // 4
    est = embedding_cost_usd(getattr(embedder, "model", embedder.name), tokens)
    n = ingest(dsn, embedder=embedder)
    print(f"ingested {n} documents into {dsn.split('@')[-1]} "
          f"(embedder={embedder.name}, ~{tokens} tokens, est. cost ${est:.6f})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
