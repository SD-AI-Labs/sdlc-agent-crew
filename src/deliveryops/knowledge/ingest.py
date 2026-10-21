"""Ingest the process library into Postgres/pgvector.

Usage:  DATABASE_URL=postgresql://... deliveryops-knowledge
Idempotent: current chunks are cleared and rewritten for each source document.
"""
from __future__ import annotations

import os
import sys

from .embedder import HashEmbedder, to_pgvector
from .library import DOCS


def ingest(dsn: str, docs=None, embedder: HashEmbedder | None = None) -> int:
    import psycopg

    docs = docs if docs is not None else DOCS
    embedder = embedder or HashEmbedder()
    count = 0
    with psycopg.connect(dsn) as conn, conn.cursor() as cur:
        for doc in docs:
            ref = f"{doc.source}#{doc.id}"
            payload = f"{doc.title}\n\n{doc.content}"
            cur.execute("DELETE FROM knowledge_chunks WHERE source = %s", (ref,))
            cur.execute(
                "INSERT INTO knowledge_chunks (source, content, embedding) VALUES (%s, %s, %s)",
                (ref, payload, to_pgvector(embedder.embed(payload))),
            )
            count += 1
        conn.commit()
    return count


def main(argv: list[str] | None = None) -> int:
    dsn = os.getenv("DATABASE_URL", "")
    if not dsn:
        print("DATABASE_URL is not set; nothing to ingest into.", file=sys.stderr)
        return 2
    n = ingest(dsn)
    print(f"ingested {n} documents into {dsn.split('@')[-1]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
