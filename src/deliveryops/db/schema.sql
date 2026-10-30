-- DeliveryOps run state, audit, telemetry, outbox and knowledge store.
-- Applied automatically by docker compose (docker-entrypoint-initdb.d) or manually with psql.
CREATE EXTENSION IF NOT EXISTS vector;

-- One row per run; `state` holds the pipeline snapshot as JSONB.
CREATE TABLE IF NOT EXISTS runs (
    id            UUID PRIMARY KEY,
    title         TEXT        NOT NULL,
    description   TEXT        NOT NULL,
    status        TEXT        NOT NULL CHECK (status IN
                  ('submitted','running','awaiting_approval','approved','rejected','failed')),
    stage         TEXT        NOT NULL DEFAULT '',
    state         JSONB       NOT NULL DEFAULT '{}',
    outcome       TEXT        NOT NULL DEFAULT '',
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS runs_status_idx ON runs(status, created_at DESC);

-- Append-only audit log: every agent action and every human decision.
CREATE TABLE IF NOT EXISTS audit_events (
    id        BIGSERIAL PRIMARY KEY,
    run_id    UUID        NOT NULL REFERENCES runs(id),
    ts        TIMESTAMPTZ NOT NULL DEFAULT now(),
    actor     TEXT        NOT NULL,   -- agent:<name> | human:<id> | system
    action    TEXT        NOT NULL,
    detail    JSONB       NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS audit_events_run_idx ON audit_events(run_id, ts);
-- Natural key: re-persisting a run (e.g. a fresh process after a restart) replays its
-- audit entries, and the insert is ON CONFLICT DO NOTHING, so the log stays exactly-once.
CREATE UNIQUE INDEX IF NOT EXISTS audit_events_natural_idx
    ON audit_events(run_id, ts, actor, action);

CREATE OR REPLACE FUNCTION audit_events_immutable() RETURNS trigger AS $$
BEGIN RAISE EXCEPTION 'audit_events is append-only'; END $$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS audit_events_no_change ON audit_events;
CREATE TRIGGER audit_events_no_change BEFORE UPDATE OR DELETE ON audit_events
    FOR EACH ROW EXECUTE FUNCTION audit_events_immutable();

-- Per-call telemetry for the cost/latency/quality dashboard.
CREATE TABLE IF NOT EXISTS agent_calls (
    id            BIGSERIAL PRIMARY KEY,
    run_id        UUID REFERENCES runs(id),
    agent         TEXT        NOT NULL,
    model         TEXT        NOT NULL,
    input_tokens  INT         NOT NULL,
    output_tokens INT         NOT NULL,
    latency_ms    REAL        NOT NULL,
    cost_usd      NUMERIC(12,6) NOT NULL DEFAULT 0,
    ok            BOOLEAN     NOT NULL DEFAULT TRUE,
    ts            TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS agent_calls_run_idx ON agent_calls(run_id);

-- Escalation queue: what a senior reviewer has to look at, and why.
CREATE TABLE IF NOT EXISTS escalations (
    id        BIGSERIAL PRIMARY KEY,
    run_id    UUID        NOT NULL REFERENCES runs(id),
    stage     TEXT        NOT NULL,
    risk      TEXT        NOT NULL,
    reasons   JSONB       NOT NULL DEFAULT '[]',
    ts        TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS escalations_run_idx ON escalations(run_id, stage);

-- Outbox: durable handoff events (ADR 0003). Delivered rows get a timestamp.
CREATE TABLE IF NOT EXISTS outbox (
    id           BIGSERIAL PRIMARY KEY,
    run_id       UUID        NOT NULL,
    type         TEXT        NOT NULL,
    stage        TEXT        NOT NULL,
    payload      JSONB       NOT NULL DEFAULT '{}',
    ts           TIMESTAMPTZ NOT NULL DEFAULT now(),
    delivered_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS outbox_pending_idx ON outbox(delivered_at) WHERE delivered_at IS NULL;
CREATE UNIQUE INDEX IF NOT EXISTS outbox_unique_event ON outbox(run_id, type, stage);

-- Knowledge layer for retrieval grounding (ADR 0010). `embedding` is dimensionless so
-- embedders with different output sizes share the table; `embedder` namespaces rows so
-- switching embedders never mixes incompatible vector spaces. Existing databases need a
-- recreate (or ALTER) for this change.
CREATE TABLE IF NOT EXISTS knowledge_chunks (
    id        BIGSERIAL PRIMARY KEY,
    source    TEXT NOT NULL,
    content   TEXT NOT NULL,
    embedding vector,
    embedder  TEXT NOT NULL DEFAULT 'hash-ngram-1024'
);
CREATE UNIQUE INDEX IF NOT EXISTS knowledge_source_idx ON knowledge_chunks(source, embedder);
-- HNSW: better recall than ivfflat at this corpus size and no lists/training step (ADR 0010).
CREATE INDEX IF NOT EXISTS knowledge_embedding_idx ON knowledge_chunks
    USING hnsw (embedding vector_cosine_ops);
