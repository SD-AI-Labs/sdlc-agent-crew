# Status: what was actually run

Rules for this document: every command below was executed on the machine described, output is
pasted as it came back, and anything not executed is listed as not executed. Nothing here is a
claim about intent.

## Environment

| | |
| --- | --- |
| Host | macOS 27.0.1, arm64 |
| Repository | https://github.com/SD-AI-Labs/sdlc-agent-crew (public, `main`) |
| Python | 3.12.13 (uv-managed venv at `.venv`) |
| Package manager | uv 0.11.32, `uv.lock` committed |
| Libraries | langgraph 1.2.16, langchain-core 1.6.12, mcp 2.3.0, fastapi 0.157.0, pydantic 2.14.1, anthropic 1.17.0 |
| Database | Postgres 16 + pgvector, via Docker (Docker 29.8.2 / compose v5.6.0) |
| Model used for verification | `fake-keyword-llm` (offline keyword baseline) |

No `ANTHROPIC_API_KEY` was present in the environment, so **no real-model run was executed**. Every
number below is from the offline model or from deterministic code paths.

## Verified checklist

| # | Item | Command | Result |
| --- | --- | --- | --- |
| 1 | Install from lockfile | `uv sync --frozen --extra dev --extra anthropic` | ok, project + all console scripts installed |
| 2 | Lint | `uv run ruff check .` | `All checks passed!` |
| 3 | Test suite | `uv run pytest -q` | `112 passed in 1.28s` (132 passed after the 2026-10-25 pass, see below) |
| 4 | Eval gate blocks the baseline | `uv run deliveryops-eval` | exit 1, `passed=False` (see below) |
| 5 | Gate self-check | `uv run deliveryops-eval --baseline-check` | `OK: the gate rejects the naive baseline`, exit 0 |
| 6 | Stack boots | `docker compose up -d` | `db` healthy, `knowledge` ingested 8 docs, `api` up, `worker` up |
| 7 | Four gates over HTTP | `curl` POST `/tickets`, 4x POST `/decisions` | `intake -> design -> code -> review -> approved` |
| 8 | Run row + audit trail in Postgres | `psql` selects | 10 audit rows for one run, agents and humans interleaved |
| 9 | Telemetry rows | `psql` on `agent_calls` | one row per agent call, FK to `runs` satisfied |
| 10 | Durable outbox | `psql` on `outbox` + worker log | 9 events, all `delivered=true`, worker logged each |
| 11 | pgvector retrieval | run detail `design.citations` | `security/review-checklist.md#kb-004` |
| 12 | Dashboard renders | `GET /dashboard`, inspected in a browser | rendered with runs, per-stage cost table, escalations |
| 13 | Patch gated until approval | `GET /runs/{id}/patch` before/after gates | 409 before, diff after (tested over HTTP) |
| 14 | Escalated gate needs authority | `POST /decision` with `role=engineer` | 403 for an escalated stage; `role=senior` accepted |
| 15 | MCP server over stdio | spawned `deliveryops-mcp`, JSON-RPC `initialize -> tools/list -> tools/call` | handshake ok, 8 tools listed, `search_knowledge` returned real citations |
| 16 | Dead-code check | `uv run vulture` | exit 0, no findings (config in `pyproject.toml`) |
| 17 | Design agent reaches knowledge through MCP | `GET /runs/{id}` after the design gate | `citations: ['engineering-handbook/estimation-guide.md#kb-002']` from pgvector, via the MCP client |
| 18 | Durable telemetry totals | `GET /metrics` after rebuilding the API container | `persisted: {'calls': 6, 'tokens': 470, 'cost_usd': 0.0}` read from `agent_calls`, not from process memory |
| 19 | CI on GitHub Actions | push to `main`, run 39633444502 | conclusion `success`: `lint and tests` ok (`uv sync --frozen` + ruff + pytest on the runner), `eval gate has teeth` ok, `eval gate (real model)` ok with the model step **skipped** (no secret) |

### Eval gate output (offline baseline, 20 golden tickets)

```
$ uv run deliveryops-eval
model=fake-keyword-llm cases=20 trials=1 passed=False cost=$0.0000
  type               0.60  (gate 0.85)
  priority           0.70  (gate 0.8)
  estimate           0.62  (gate 0.7)
  requirements       0.07  (gate 0.6)
  safety             1.00  (gate 1.0)
  GATE FAIL  type: 0.60 < 0.85
  GATE FAIL  priority: 0.70 < 0.8
  GATE FAIL  estimate: 0.62 < 0.7
  GATE FAIL  requirements: 0.07 < 0.6
  GATE FAIL  critical case failed: T-009
  GATE FAIL  critical case failed: T-010
  GATE FAIL  critical case failed: T-015
  GATE FAIL  critical case failed: T-019
```

This is the intended result: the naive baseline is not shippable, and the gate says so with reasons.
(The paste above was regenerated on 2026-10-25 against the current code: T-002 passes now, so the
critical list is four cases. The earlier paste listed five; see the update section below.)
`requirements 0.07` is the design working: the `must_mention` terms are synthesis terms
(`regression`, `rollback`, `idempotency`), so echoing the ticket scores near zero.

### Postgres state after one full run (clean volume)

```
$ docker compose exec -T db psql -U deliveryops -d deliveryops -c "select ..."
 runs | audit | calls | escalations | outbox | delivered | kb
------+-------+-------+-------------+--------+-----------+----
    1 |    10 |     4 |           2 |      9 |         9 |  8
```

### Outbox delivery (worker log)

```
INFO:deliveryops.worker:event gate.opened run=... stage=intake escalate=False
INFO:deliveryops.worker:event gate.opened run=... stage=design escalate=True
INFO:deliveryops.worker:event gate.opened run=... stage=code   escalate=True
INFO:deliveryops.worker:event gate.opened run=... stage=review escalate=False
INFO:deliveryops.worker:delivered 9 event(s)
```

## Bugs found by running it (and fixed)

1. **Foreign-key ordering.** `agent_calls.run_id` references `runs.id`, but the run row was written
   after the first agent call. In-memory repositories hid it; Postgres returned 500 on the first
   ticket. Fixed by saving the run row before invoking the graph, with a regression test that
   asserts save-before-call ordering (`tests/test_persistence.py`).
2. **Duplicate escalation rows.** Escalations were recorded when the gate opened *and* again when it
   was decided. Now recorded once, at open; regression test asserts one row per stage.
3. **Doubled citations.** pgvector rows store `path#kb-id`, and the retriever appended the id a
   second time (`...#kb-004#kb-004`). Fixed by splitting the stored source.
4. **uv silently not installing the project.** Without a `[build-system]` section uv treats the
   project as non-package: `uv sync` produced no console scripts and the Docker image could not run
   `deliveryops-api`/`worker`. Fixed by declaring setuptools as the build backend.

All four were caught by execution, not by review. The first two only appear with a real database.

### MCP over stdio (real subprocess)

```
initialize -> {"jsonrpc":"2.0","id":1,"result":{"capabilities":{...}},"instructions":"Ticket, repository, ..."}
tools/list -> 8 ['get_ticket', 'list_tickets', 'create_ticket', 'read_repo_file', 'list_repo_files',
                 'search_knowledge', 'validate_patch', 'run_status']
tools/call -> ['platform/change-policy.md#kb-005', 'platform/rollout-policy.md#kb-006']
```

## Dead-code cleanup (later pass)

Run with `uv run vulture` (vulture 2.16; `[tool.vulture]` in `pyproject.toml`: paths `src`,
min-confidence 60, ignore list limited to framework-registered names such as FastAPI route handlers,
MCP tool functions, and dataclass fields read through `to_dict()`).

**Deleted (nothing read it):**

| Removed | Where |
| --- | --- |
| `DiffInfo.rejected_paths`, `DiffInfo.touched` | `agents/diff.py` |
| `RepositoryError` | `db/repository.py` |
| `RunRecord.updated_at`, `RunRecord.runtime_seconds` (written, never read) | `db/repository.py`, `orchestrator/pipeline.py` |
| `GateDecision.requires_human` (structurally always true), `GateDecision.summary` | `guardrails/policy.py` |
| `HashEmbedder.embed_many`, `DOCS_BY_ID` | `knowledge/` |
| `ToolError` (client), `embed_text` (MCP tool) | `mcp_server/` |
| `Tracer.run_summary` | `observability/tracer.py` |
| `StageSignals` (graph), `STAGES` (state) | `orchestrator/` |
| `OutboxWorker.handled` | `orchestrator/events.py` |

**Wired instead of deleted** - each of these was dead *and* contradicted a documented claim, so the
fix was to make the claim true rather than drop the code:

| Previously dead | Now used by | Effect |
| --- | --- | --- |
| `injection.matched_phrases` | gate payload `injection_phrases` | ADR 0005 said matched phrases are recorded; now they are |
| `mcp_server/client.py` | design agent's knowledge search | the MCP layer is on the hot path, not decorative |
| `ReviewResult.critical_count` | review payload `critical_findings`, read by the code/review gate | one computation instead of two |
| `Repository.calls` | `/metrics` `persisted` block | token/cost totals survive an API restart |

Result after the pass: `uv run vulture` exit 0, `uv run ruff check .` clean, `uv run pytest -q`
112 passed (132 after the 2026-10-25 pass). Surface changes worth knowing: MCP exposes 8 tools (was 9), `/metrics` gained
`persisted`, the gate payload can carry `injection_phrases`, and `DesignAgent` takes a `search`
callable instead of a `retriever` object.

## Not done / not verified

| Area | Status |
| --- | --- |
| Real-model eval (`ANTHROPIC_API_KEY`) | **Not executed.** Gate thresholds are unvalidated against Claude; the numbers for a real model are unknown. |
| LLM-as-judge dimension | Implemented and unit-tested with a scripted judge; **never run against a real judge model**. |
| Real-model gate in CI | The `eval gate (real model)` job completes `success` but the model step is `skipped` because no `ANTHROPIC_API_KEY` secret exists. It reports a notice instead of pretending to have run. |
| Branch protection | Not configured. CI is green but nothing requires it, so a failing check does not block a merge today. |
| MCP with a third-party client | Verified over real stdio with a JSON-RPC probe (initialize, tools/list, tools/call) and in a pytest smoke test. Not tried with Claude Desktop or the MCP Inspector UI. |
| Resume after API restart | **Implemented 2026-10-25** (`orchestrator/checkpoint.py`, default postgres when `DATABASE_URL` is set). Unit-tested: selection rules, plus a restart-resume test across two pipeline objects sharing a durable checkpointer. **Not yet verified against a real Postgres in this pass** (no Docker on the machine used); the compose stack and `deploy/README.md` step 5 are the way to prove it. |
| Authentication / identity | None. The `role` used for escalation authority is supplied by the caller and not verified. |
| Per-stage metrics after restart | `/metrics` now reports `persisted` totals (calls, tokens, cost) read from `agent_calls`, but the per-stage breakdown and p95 latency are still aggregated from in-process spans. A restart keeps the totals, not the stage detail. |
| pgvector index quality | `ivfflat (lists=10)` on 8 rows; recall is not measured and the index adds nothing at this size. |
| Dashboard | Verified at desktop width only; no responsive/a11y pass. Read-only by design. |
| Docker image size / hardening | Based on `python:3.12-slim`, runs as root. Not a deployment artifact. |

## Update: production-hardening pass, 2026-10-25

Environment for everything below: Linux x86_64 sandbox, Python 3.12.15 (uv-managed, now pinned by
`.python-version`), uv 0.13.3. No Docker and no network credentials were present, which bounds
what could be executed.

What changed:

1. **Jira Cloud intake** (`integrations/jira.py`): real REST v3 get/list/create behind the ticket
   store seam, env-configured (`JIRA_SITE`/`JIRA_EMAIL`/`JIRA_API_TOKEN`/`JIRA_PROJECT`), partial
   config is a loud error. Mock store stays the default.
2. **Read-only GitHub grounding** (`integrations/github.py`): repo listing + file contents for the
   code agent and the MCP repo tools, same gating rules as the local sandbox. No write path to
   GitHub exists (ADR 0006/0007).
3. **Postgres checkpointer** (`orchestrator/checkpoint.py`): `InMemorySaver` replaced by
   `PostgresSaver` whenever `DATABASE_URL` is set (override with `DELIVERYOPS_CHECKPOINTER`).
4. **Housekeeping**: `LICENSE` (MIT - a choice worth revisiting if this should be
   non-commercial), `.python-version` pin, Docker image runs as non-root, stale T-002 eval paste
   regenerated in README and this file.
5. **Deploy config**: `deploy/terraform/` (Cloud Run api + worker, Cloud SQL pgvector, Secret
   Manager) and `deploy/README.md` runbook.

Commands actually executed in this pass (real output):

```
$ uv run pytest -q
132 passed in 2.19s
$ uv run ruff check .
All checks passed!
$ uv run vulture            # exit 0, no findings
$ uv run deliveryops-eval   # exit 1 - the gate still rejects the baseline (see block above)
$ uv run deliveryops-eval --baseline-check
OK: the gate rejects the naive baseline
```

Not executed in this pass (and therefore not claimed):

| Area | Status |
| --- | --- |
| Live Jira Cloud calls | Client is unit-tested with scripted HTTP (20 new tests). No real Jira site was contacted. |
| Live GitHub API calls | Same: scripted HTTP only. No real repository was read. |
| Resume after restart on real Postgres | Semantics tested with a shared durable checkpointer; the Postgres-specific path (`PostgresSaver.setup()` and resume) has not run against a live database in this pass. |
| Docker build / compose up | No Docker on this machine. The non-root Dockerfile change is unverified. |
| Terraform | `terraform plan`/`apply` never run; `deploy/terraform` is unverified against GCP. |
| Real-model eval | Still not executed (no `ANTHROPIC_API_KEY`), unchanged policy. |

## Update: model-layer standardization + guardrails, 2026-10-25 (same machine)

What changed:

1. **Model-independent LLM layer** (`llm/chat_model.py`): LangChain v1 `init_chat_model` behind
   the unchanged internal `LLM` protocol; provider and model are env configuration
   (`DELIVERYOPS_LLM=chat`, `DELIVERYOPS_PROVIDER`, `DELIVERYOPS_MODEL`; extras per provider).
   The raw-SDK Anthropic client was deleted. langgraph bumped 1.2.16 -> 1.2.17.
2. **Structured output as transport** (ADR 0008): each agent declares a Pydantic schema;
   `structured()` tries native JSON-schema mode, then tool-calling, then plain text - the
   existing validators enforce the contract on every path. Usage/cost metadata preserved.
3. **LangGraph Studio**: `langgraph.json` + `orchestrator/factory.py` entry point; the compiled
   graph imports and compiles (verified in-process). `langgraph dev` itself was not launched.
4. **Guardrails** (ADR 0009): pre-prompt injection screening of GitHub repo content (file names
   with separator-evasion normalized + file contents; exclusions are visible in the prompt) and
   a per-run cost cap (`DELIVERYOPS_MAX_RUN_COST_USD`, default 1.00, 0 disables) that fails the
   run closed. Deferred with reasons recorded: PII redaction, model-based moderation.

Commands actually executed:

```
$ uv run pytest -q
144 passed in 2.44s
$ uv run ruff check .
All checks passed!
$ uv run vulture                # exit 0, no findings
$ uv run deliveryops-eval --baseline-check
OK: the gate rejects the naive baseline
$ uv run python -c "from deliveryops.orchestrator.factory import graph"   # compiles
```

Still not executed: any real-model call (no key here), `langgraph dev` end-to-end, and
everything in the earlier "not executed" table remains true.

## 2026-10-25 (later): retrieval credibility pack (ADR 0010)

5. **Real embeddings behind the embed protocol**: `LangChainEmbedder` (any provider via
   `init_embeddings`), selected by `DELIVERYOPS_EMBEDDER`/`DELIVERYOPS_EMBED_PROVIDER`/
   `DELIVERYOPS_EMBED_MODEL`; hash stays the offline default. Embedding prices in
   `llm/pricing.py`; ingest prints the estimated reingest cost. Not executed: a real embedding
   call (no provider key in this environment) - the langchain path is covered by structural
   tests with a fake client only.
6. **Retrieval eval**: 20 golden query -> kb-id pairs (`evals/golden/retrieval.json`),
   `deliveryops-eval --retrieval` reports recall@1/@3 against thresholds in `evals/gates.py`.
   Measured on the offline stack (this machine, these commands):
   lexical 0.75/0.90 (passes the 0.70/0.90 gate), hash-vector 0.60/0.80 (fails),
   hybrid-with-hash 0.60/0.90 (fails recall@1). The vector numbers were measured with an
   in-memory cosine index identical to the pgvector query, **not** against a live database.
   Known misses: r01 (ship-to-production phrasing ranks rollout policy first) and r15
   ("roll back" as two tokens never matches "rollback" lexically).
7. **Confidence cutoff + no-grounding signal**: every retriever takes `min_score`
   (`DELIVERYOPS_RETRIEVAL_MIN_SCORE`); below it the result is zero hits and the design gate
   adds "no confident knowledge grounding" (medium risk, not an escalation). Defaults are 0.0
   (off): measured on this library, no cutoff separates out-of-domain queries from the weakest
   legitimate golden query (weakest golden top 1.79, strongest sampled out-of-domain top 1.79).
   Claiming a calibrated default would be fiction; calibrate with a real embedder.
8. **Hybrid retrieval** (`DELIVERYOPS_RETRIEVER=hybrid`): RRF (k=60) of lexical + vector.
   Compose now defaults to hybrid.
9. **Hygiene**: HNSW replaces ivfflat; `knowledge_chunks` gains an `embedder` namespace column
   (queries filter on it) and a dimensionless vector column - **existing databases need a
   recreate**; ingest deletes stale sources and reports rows owned by other embedders;
   database failures degrade to lexical instead of a 500 (`ResilientRetriever`).

Commands actually executed:

```
$ uv run pytest -q
159 passed in 2.24s
$ uv run ruff check src tests
All checks passed!
$ uv run vulture                # exit 0, no findings
$ uv run deliveryops-eval --baseline-check
OK: the gate rejects the naive baseline
$ uv run deliveryops-eval --retrieval
retriever=lexical cases=20 passed=True
  recall@1   0.75  (gate 0.7)
  recall@3   0.90  (gate 0.9)
  MISS  r01: expected kb-001, got ['kb-006', 'kb-002', 'kb-005']
  MISS  r15: expected kb-006, got []
```

Still not executed: any real embedding/LLM API call, `--retrieval` against a live database,
`docker compose up` with the new schema, and everything in the earlier "not executed" lists.

## Update: production Phase 1, 2026-10-30

Branch `fix/production-phase1` (from `fix/p0-batch`; not pushed). Implements Phase 1 of the
production plan: make the deployment real - terraform correctness, connection pooling,
migrations, startup config validation, pilot API identity, and real gate notifications.

Environment for this pass: Linux x86_64 sandbox, Python 3.12 (uv), terraform 1.9.8 (downloaded
locally for fmt/validate only). **No Docker and no GCP access in this sandbox** - the "not
executed" list below is correspondingly long, and nothing here claims a deploy happened.

What changed:

1. **Terraform** (`deploy/terraform`, commit `2b67c16`): GCS remote backend (partial config -
   bucket/prefix per environment at init); model env drift fixed (`DELIVERYOPS_LLM=chat` +
   `DELIVERYOPS_PROVIDER`/`DELIVERYOPS_MODEL` variables, replacing `fake`/"flip to anthropic");
   Cloud SQL REGIONAL availability, backups with PITR + 7 retained, Sunday 03:00 UTC
   maintenance window, disk autoresize; worker pinned to 1 always-on instance with
   `cpu_idle = false`; API scaling explicit with the connection budget
   (instances x `DELIVERYOPS_DB_POOL_MAX`) in a comment; runtime identities split (API SA reads
   all secrets, worker SA only `database-url`); new `deliveryops-migrate` Cloud Run v2 job;
   provider lock file committed; `deploy/README.md` updated (backend init, migrate step,
   worker note).
2. **Connection pooling** (`204d637`): `PostgresRepository` and `PostgresOutbox` check
   connections out of one lazily-created `psycopg_pool` pool per DSN instead of
   `psycopg.connect()` per call; sizes from `DELIVERYOPS_DB_POOL_MIN/MAX` (defaults 1/8).
   Dependency is now `psycopg[binary,pool]`. In-memory paths untouched.
3. **Migrations runner** (`2ccd45b`): `deliveryops-migrate` console script (`db/migrate.py`) -
   `schema_migrations` table, schema.sql applied as version 1 when unrecorded (every statement
   verified idempotent), then `PostgresSaver.setup()`. Compose gains a one-shot `migrate`
   service that api/worker wait on; the initdb mount stays.
4. **Startup config validation** (`75f5181`): `deliveryops/config.py`, called by the API and
   worker entry points. Aggregates every env problem (unknown mode/provider/embedder/
   retriever values, retriever/checkpointer vs `DATABASE_URL`, numeric knobs, Jira partial
   config via the adapter's own constructor) and logs a redacted summary - secrets appear only
   as set/unset.
5. **Pilot API identity** (`c66347c`): `DELIVERYOPS_API_TOKENS` (JSON token -> {name, role}),
   off by default. When set: mutating endpoints require `Authorization: Bearer <token>`
   (401 otherwise), decision approver/role come from the token and body values are ignored,
   escalated gates still 403 on the token's role, reads open unless
   `DELIVERYOPS_AUTH_PROTECT_READS=1`, `/health` always open. Malformed token config is a
   loud startup error. README + `.env.example` document it.
6. **Slack gate notifications + worker hardening** (`d5de15c`): `notify()` posts the gate
   event as JSON to `DELIVERYOPS_SLACK_WEBHOOK_URL` via stdlib urllib when set (log-only
   otherwise); failures are logged and swallowed. The poll loop wraps `run_once` in
   try/except with exponential backoff (cap 30s) instead of dying on a database hiccup.
7. **CI** (`26933b2`): new `docker` job (build only, no push) and `terraform` job
   (terraform 1.9.8 pinned: `fmt -check`, `init -backend=false`, `validate`). Existing jobs
   untouched.

Commands actually executed in this pass:

```
$ uv run pytest -q                     # 206 passed (was 173 on fix/p0-batch; +33 new)
$ uv run ruff check .                  # All checks passed!
$ uv run vulture                       # exit 0, no findings
$ uv run deliveryops-eval --baseline-check
OK: the gate rejects the naive baseline
$ terraform fmt -check -recursive deploy/terraform   # clean (after fmt)
$ terraform init -backend=false && terraform validate
Success! The configuration is valid.
$ uv run deliveryops-migrate           # no DATABASE_URL: refuses loudly, as designed
```

`terraform validate` earned its keep: it caught **two pre-existing bugs** in the original
configuration - the worker's `INGRESS_TRAFFIC_NONE` is not a legal value for a Cloud Run v2
service (now `INGRESS_TRAFFIC_INTERNAL_ONLY`), and `replication { auto {} }` on one line was
not fmt-parseable. Both are fixed in the terraform commit.

Not executed in this pass (and therefore not claimed):

| Area | Status |
| --- | --- |
| `terraform apply` / any GCP call | **Not run.** No GCP access in this sandbox; validate + fmt only. The deploy remains unproven end to end. |
| Docker build / `docker compose up` | **Not run** (no Docker). The new compose `migrate` service and the CI `docker` job are unexercised; the CI job's first run on GitHub is their first real test. |
| Real Postgres for the pool / migrate | **Not run.** Pooling and the migrate runner are unit-tested against faked `psycopg`/`psycopg_pool`; `PostgresSaver.setup()` inside `deliveryops-migrate` has not run against a live database. |
| Slack webhook | **Not posted.** `urllib.request.urlopen` is stubbed in tests; no real Slack workspace received a message. |
| Worker as a Cloud Run *service* | **Known unresolved gap.** Validate passes, but a v2 service's container must listen on `$PORT` and `deliveryops-worker` serves no HTTP - a real deploy would still need a health endpoint on the worker or the event-driven job conversion the production plan prefers. Recorded here rather than papered over. |
| Secret *versions* pinning | Not done (Phase 1 plan item 1.6): services still reference `latest`. Rotation safety is unchanged from before this pass. |
| `DELIVERYOPS_EMBEDDER` factory default | `get_embedder()` itself still maps unknown values to the hash embedder silently; startup validation now rejects them for the API and worker entry points, but the eval runner and knowledge ingest do not call `validate_config()`. |

### Addendum (same day): the worker `$PORT` gap above is closed

`deliveryops-worker` now serves HTTP when `PORT` is set: a stdlib
`ThreadingHTTPServer` in a daemon thread answers `GET /health` with the poll loop's
liveness (uptime, poll count, last successful poll, last error, delivered total) from a
shared `HealthState` the loop updates on every poll and failure. One route only -
anything else is 404 - and with no `PORT` set nothing listens, so local and compose
behaviour is unchanged. The terraform worker service now declares
`ports { container_port = 8080 }` to match. The event-driven job conversion from the
production plan remains the longer-term option; this removes the deploy blocker without
changing the worker's shape.

Executed for this addendum: `uv run pytest -q` (209 passed, +3 health tests over a real
socket), `ruff` clean, `vulture` clean (the two `BaseHTTPRequestHandler` hooks added to
the framework-names ignore list), `terraform fmt -check` + `validate` clean, and a live
smoke run - the real worker process started with `PORT` set, `/health` returned live poll
counters, other paths returned 404, and the process stopped cleanly. Still not executed:
a real Cloud Run deployment (no GCP access in this sandbox).

## Update: one branch, three targets (local | gcp | aws), 2026-10-30 (later still)

The two fix branches are gone: `fix/p0-batch` and `fix/production-phase1` were
fast-forwarded into `main` (16 commits, no merge commit - main had not diverged)
and deleted. Everything below landed on `main` directly, one commit per step,
and **nothing was pushed**.

What changed:

1. **Terraform layout** (`3645e38`): the GCP module moved to
   `deploy/terraform/gcp/`; `deploy/terraform/aws/` is its sibling (below).
   Historical entries in this file keep the old path - they record what was
   true when written.
2. **AWS module** (`621594c`): ECS Fargate + RDS mirroring the GCP module -
   RDS Postgres 16 (Multi-AZ by default, 7-day PITR backups, deletion
   protection, not publicly accessible, 5432 only from the task security
   groups), ECR, an ALB-fronted API service health-checking `/health`, a
   worker service with no load balancer, a migrate task definition,
   per-service execution roles split like the GCP service accounts (worker
   injects only `database-url`; task roles have no policies), secret
   containers with values added afterwards, and an S3 + DynamoDB remote
   backend (partial config, per-env key). Two simplifications are stated in
   the module, not hidden: the default VPC stands in for a dedicated one
   (tasks take public IPs as their egress path), and the RDS master user is
   the app user - which is what lets `deliveryops-migrate` run schema.sql's
   `CREATE EXTENSION` for pgvector, master holding `rds_superuser` (the GCP
   app user holds the Cloud SQL equivalent). `.terraform/` joined
   `.gitignore` in the same commit: the AWS provider set is ~700MB of
   binaries that `init` fetches; they are never committed. (The GCP
   provider binaries committed by an earlier pass remain in history -
   untracking them is a separate cleanup, not attempted here.)
3. **`DELIVERYOPS_DEPLOY_TARGET`** (`f1978b5`): `local` (default) | `gcp` |
   `aws`, validated in `config.py`. Unknown value = loud error. Cloud
   targets additionally require `DATABASE_URL`, reject
   `DELIVERYOPS_LLM=fake` unless `DELIVERYOPS_ALLOW_FAKE_LLM=1`, and log a
   prominent warning when `DELIVERYOPS_API_TOKENS` is unset (a warning, not
   an error: a pilot may sit behind IAP / an ALB with OIDC). Local gains no
   new constraints. Both terraform modules and docker-compose set the
   variable for their environment. 15 new tests (`tests/test_deploy_target.py`)
   cover every rule for both cloud targets.
4. **`deploy/run.sh`** (`347c5af`): one entrypoint. The target is an argument
   or an interactive prompt (`Run target? [local/gcp/aws] (default local):`;
   one re-prompt on invalid input, then a non-zero exit). `local` runs the
   compose stack when docker exists, else starts the API (:8000) and worker
   (PORT=18099) as uv processes under a trap that stops both with the script.
   `gcp`/`aws` run `terraform init` (backend config from a per-module
   `backend.hcl` or `TF_BACKEND_CONFIG_ARGS`) and `terraform plan`; apply
   happens only under `--apply`.
5. **Runbook + CI** (`d199549`): `deploy/README.md` restructured around the
   three targets (shared contract first, then per-target sections; the AWS
   section covers ECR push, secret values via the aws CLI, and the migrate
   task via `aws ecs run-task`). The CI terraform job is a matrix validating
   both modules.

Commands actually executed in this pass:

```
$ uv run pytest -q                     # 224 passed (was 209; +15 deploy-target tests)
$ uv run ruff check .                  # All checks passed!
$ uv run vulture                       # exit 0, no findings
$ uv run deliveryops-eval --baseline-check
OK: the gate rejects the naive baseline
$ terraform fmt -check -recursive deploy/terraform   # clean
$ terraform -chdir=deploy/terraform/gcp init -backend=false && terraform -chdir=deploy/terraform/gcp validate
Success! The configuration is valid.
$ terraform -chdir=deploy/terraform/aws init -backend=false && terraform -chdir=deploy/terraform/aws validate
Success! The configuration is valid.   # hashicorp/aws ~> 5.0 downloaded for real in this sandbox
$ printf 'bogus\nnope\n' | deploy/run.sh        # re-prompts once, exits 2
$ printf '\n' | deploy/run.sh                    # (inside a throwaway network namespace - see below)
local target, via uv ... Ctrl-C stops the API and the worker.
$ printf 'aws\n' | deploy/run.sh                 # dispatches to terraform init; stops at the
                                                 # partial-backend prompt (no backend config given)
$ TF_BACKEND_CONFIG_ARGS='-backend-config=bucket=no-such-bucket-xyz -backend-config=prefix=deliveryops/test' deploy/run.sh gcp
                                                 # init reaches the real GCS backend and fails on
                                                 # missing Google credentials - the sandbox limit
```

Two testing notes, stated plainly. The `run.sh` local uv branch was executed
inside a throwaway network namespace (`unshare -rn`) because this sandbox's
ports 8000/18099 were already held by the earlier smoke-run API/worker, which
were left running untouched; inside the namespace the branch cold-started
for real - health check passed, URLs printed - and on SIGINT the trap
stopped both children (verified: the ports were dead afterwards). And the
backend-config test caught a real bug in the making: quotes inside
`TF_BACKEND_CONFIG_ARGS` survive word splitting literally and terraform
rejects them, so the script's header now documents unquoted values.

Not executed in this pass (and therefore not claimed):

| Area | Status |
| --- | --- |
| `terraform apply` / plan against real accounts | **Not run.** No GCP or AWS credentials exist in this sandbox; both cloud paths through `run.sh` stop at backend initialisation, exactly as recorded above. A real `plan` needs credentials plus the required variables (`project_id`/`image`, `image`). |
| AWS anything beyond validate | **Not run.** No AWS account was touched: no ECR push, no `run-task`, no RDS. The module is validate-clean only - the same status the GCP module had before its first apply, which caught two real bugs only *at* validate time; AWS may have plan/apply-time surprises of its own. |
| `run.sh` local via docker compose | **Not run** (no Docker in this sandbox). The compose branch is three commands and a print; its first real run is on a machine with Docker. |
| CI matrix job | **Not run.** The workflow change is committed; GitHub Actions will exercise it on the next push - which has not happened (nothing is pushed). |

## Addendum (2026-10-30, late): provider binaries purged from history

The GCP provider binaries under `deploy/terraform/gcp/.terraform/` (a 117 MB
`terraform-provider-google` binary - over GitHub's 100 MB per-file limit, so a push
would have been rejected - plus an 18 MB random provider) were tracked in the local
commits. They entered in the worker `$PORT` commit via a `git add -A` after a local
`terraform validate` run, before the `.terraform/` ignore rule existed; the later
`git mv` carried them along. Because those commits had never been pushed, the history
was rewritten with `git filter-repo` (paths `*/.terraform/*` stripped) instead of only
untracking them going forward. Result: pack size 136 MB -> 0.4 MB, all 26 commits and
their content preserved, `.gitignore` now uses `**/.terraform/`. Two consequences worth
knowing: commit hashes from the production-readiness commit onward changed (its tree is
byte-identical to the pushed original - verified with an empty diff - so the divergence
is hash-only and reconciles trivially at push time), and a pre-rewrite bundle backup was
kept outside the repo until the rewritten history is pushed. `terraform init` re-downloads
providers on demand; nothing functional changed.

## Reproduce

```bash
uv sync --frozen --extra dev --extra anthropic
uv run ruff check . && uv run pytest -q
uv run deliveryops-eval --baseline-check

cp .env.example .env
docker compose up --build -d
curl -s localhost:8000/health

curl -s -X POST localhost:8000/tickets -H 'content-type: application/json' \
  -d '{"title":"Checkout fails for all users","description":"Payment submit returns 502 for every customer. Production outage."}' | tee /tmp/run.json
curl -s -X POST localhost:8000/runs/<id>/decision -H 'content-type: application/json' \
  -d '{"approver":"you","approve":true,"role":"senior"}'
open http://localhost:8000/dashboard
```
