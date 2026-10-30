# Changelog

## v1.0.0 — 2026-10-30 (first frozen release)

The pipeline as designed in the ADRs, hardened to the point where every claim in the
README has either been executed or is listed as not executed in `docs/STATUS.md`.

**The pipeline**
- Four LangGraph stages (intake → design → code → review), each ending in a human gate
  implemented as an `interrupt()`; escalated gates require an approver with authority.
- Deterministic guardrails over signal dicts: confidence, severity inversion, prompt
  injection, patch size, forbidden paths, review findings; per-run cost cap that fails
  the run closed.
- Evals as the release gate: 20 golden tickets with per-dimension thresholds and
  critical-case hard fails, a retrieval gate (recall@1/@3 on 20 golden pairs), and a CI
  self-check proving the gate rejects the naive baseline.
- MCP tool layer (tickets, sandboxed repo reader, knowledge search, patch validation,
  run status); the design agent reaches knowledge through the MCP client, not a
  retriever import.
- Adapters behind env-configured seams: Jira Cloud intake, strictly read-only GitHub
  grounding, model-independent LLM layer (any LangChain provider is configuration),
  lexical/pgvector/hybrid retrieval with embedder namespacing.
- Durable state in Postgres: checkpointer (runs survive an API restart), append-only
  audit, per-call telemetry with dollar costs, transactional-style outbox + worker.

**Hardening in this release**
- Gate decisions are atomic and idempotent (per-run lock, expected-stage check,
  `Idempotency-Key` support); audit persistence is idempotent across restarts.
- LLM transport failures fail the run with an audit entry and a failure span instead of
  stranding it; unknown or partial configuration fails loudly at startup, never
  silently degrades; one startup validation pass with a redacted config summary.
- Pooled Postgres connections; `deliveryops-migrate` (version-tracked schema +
  checkpointer setup); worker serves `GET /health` on `$PORT` and posts gate events to
  Slack when configured; optional bearer-token API identity (approver and role from the
  token, not the request body).

**Deployment**
- One entrypoint, `deploy/run.sh [local|gcp|aws]`, prompting when no target is given;
  cloud applies only with `--apply`. Terraform for Cloud Run + Cloud SQL (GCP) and ECS
  Fargate + RDS (AWS), remote state on both, least-privilege runtime identities split
  api vs worker. The app knows its target via `DELIVERYOPS_DEPLOY_TARGET` and holds
  cloud targets to stricter startup rules.

**Known at freeze time** (details in `docs/STATUS.md`)
- No real-model eval has been run yet — every published number is from the offline
  stack, and the CI real-model job skips loudly without `ANTHROPIC_API_KEY`.
- Neither cloud target has been applied against a real account; both Terraform modules
  validate, and the GCP runbook's restart-resume proof is the first post-deploy test.
- The eval gate covers the intake agent; design/code/review golden sets are future work.
