# ADR 0010: Retrieval credibility - real embeddings, hybrid fusion, measured recall

## Context

The knowledge layer had production-grade plumbing (MCP tool path, pgvector, ingest, citations in
the audit trail) over POC-grade retrieval: a hash embedder that cannot rank paraphrases, no
retrieval eval, no confidence cutoff, an ivfflat index on 8 rows, stale rows kept forever, no
embedder versioning, and a database blip failing the whole design stage with a 500.

## Decisions

**Real embeddings are configuration, not code.** `knowledge/embedder.py` gains
`LangChainEmbedder` (LangChain `init_embeddings`, any provider) selected by
`DELIVERYOPS_EMBEDDER=hash|langchain` plus `DELIVERYOPS_EMBED_PROVIDER` / `DELIVERYOPS_EMBED_MODEL`
- the same model-independence rule as the LLM layer (ADR 0008). The hash embedder stays the
default so tests and CI never need an API key. Embedding prices live in `llm/pricing.py`; the
ingest CLI prints the estimated cost of a reingest.

**Embedder namespacing.** Every row in `knowledge_chunks` carries its embedder's name; queries
filter on the active embedder. Comparing vectors across spaces is meaningless, so mixing is
prevented structurally rather than by convention. The embedding column is dimensionless so
embedders with different output sizes share the table. Existing databases need a recreate.

**Hybrid retrieval by reciprocal rank fusion.** `HybridRetriever` fuses the lexical and vector
rankings with RRF (k=60): no score normalisation, and a document both backends like beats one a
single backend loves. Deployments with a database default to `DELIVERYOPS_RETRIEVER=hybrid`.

**All-or-nothing confidence cutoff.** Each retriever takes a `min_score`; when the top hit is
below it the answer is *no hits*, and the design gate then says "no confident knowledge
grounding" (medium risk, not an escalation) instead of letting weak matches pose as grounding.
`DELIVERYOPS_RETRIEVAL_MIN_SCORE` overrides the per-backend default. Measured honestly on the
offline stack (hash embedder + BM25-ish lexical): **no cutoff value separates** out-of-domain
queries from the weakest legitimate golden query (weakest golden top score 1.79, strongest
sampled out-of-domain top 1.79; hash-vector score distributions overlap completely), so the
defaults are 0.0 (off). The cutoff pays off with real embeddings, whose relevant/irrelevant
score distributions separate; calibrate then, with the golden set, and record the value here.

**Degrade, don't 500.** `ResilientRetriever` wraps the database-backed retrievers: a Postgres
failure or a misconfigured embedder falls back to the in-memory lexical index. Grounding on the
small library is strictly better than failing the run.

**Ingest hygiene.** Reingest deletes rows whose source left the library and reports rows owned
by other embedders. HNSW replaces ivfflat: at this corpus size the lists parameter was theater,
and HNSW has no training step and better recall.

## Measured retrieval quality (offline stack, 20 golden query -> kb-id pairs)

| retriever              | recall@1 | recall@3 | gate (0.70 / 0.90) |
| ---------------------- | -------- | -------- | ------------------ |
| lexical                | 0.75     | 0.90     | passes             |
| hash-vector (pgvector) | 0.60     | 0.80     | fails              |
| hybrid (lexical+hash)  | 0.60     | 0.90     | fails recall@1     |

The gate thresholds are set from what the offline stack actually achieves, not from aspiration.
The table is also the honest case for real embeddings: the vector side drags the hybrid down
today, and the eval (`deliveryops-eval --retrieval`) exists to prove the improvement when a real
embedder is configured - re-measure and update this table and the thresholds then.

## Consequences

- Switching embedders is: set env, re-run `deliveryops-knowledge`, re-measure the retrieval gate.
- The design gate can now say "no confident knowledge grounding" - visible in the gate payload
  and audit, never silent.
- Chunking is still deliberately absent: at ~1KB per document it would only add moving parts.
  It becomes necessary when documents exceed the embedding model's useful context or when a
  single document's topics diverge enough to compete in ranking.
