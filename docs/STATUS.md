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
| 3 | Test suite | `uv run pytest -q` | `112 passed in 1.28s` |
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
  GATE FAIL  critical case failed: T-002
  GATE FAIL  critical case failed: T-009
  GATE FAIL  critical case failed: T-010
  GATE FAIL  critical case failed: T-015
  GATE FAIL  critical case failed: T-019
```

This is the intended result: the naive baseline is not shippable, and the gate says so with reasons.
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
112 passed. Surface changes worth knowing: MCP exposes 8 tools (was 9), `/metrics` gained
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
| Resume after API restart | Runs persist in Postgres, but the LangGraph checkpointer is `InMemorySaver`, so an in-flight run cannot be resumed after a restart. The `postgres` extra is wired in `pyproject.toml`; the checkpointer swap is not implemented. |
| Authentication / identity | None. The `role` used for escalation authority is supplied by the caller and not verified. |
| Per-stage metrics after restart | `/metrics` now reports `persisted` totals (calls, tokens, cost) read from `agent_calls`, but the per-stage breakdown and p95 latency are still aggregated from in-process spans. A restart keeps the totals, not the stage detail. |
| pgvector index quality | `ivfflat (lists=10)` on 8 rows; recall is not measured and the index adds nothing at this size. |
| Dashboard | Verified at desktop width only; no responsive/a11y pass. Read-only by design. |
| Docker image size / hardening | Based on `python:3.12-slim`, runs as root. Not a deployment artifact. |

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
