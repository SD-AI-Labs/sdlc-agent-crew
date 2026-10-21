# DeliveryOps

[![ci](https://github.com/SD-AI-Labs/sdlc-agent-crew/actions/workflows/ci.yml/badge.svg)](https://github.com/SD-AI-Labs/sdlc-agent-crew/actions/workflows/ci.yml)

An agentic software-delivery pipeline: a ticket moves through **intake triage, technical design, a
proposed code change and review**, a human owns **every** gate, and **passing evals is what allows a
change to ship**.

> Status: working end to end on the offline model (`DELIVERYOPS_LLM=fake`), and on Claude when a key
> is present. 112 tests, ruff-clean, vulture-clean, CI green on `main` (lint, tests, and the
> gate-has-teeth check). **No real-model eval has been run yet** - without `ANTHROPIC_API_KEY` the
> real-model gate skips and says so. `docs/STATUS.md` records exactly what was run; known
> limitations are at the bottom.

## Why it is shaped this way

Three decisions do the work:

1. **Evals are the release gate.** A golden set of tickets with per-dimension thresholds; CI blocks
   a change that fails it. A deliberately dumb keyword baseline is checked into CI as a test that
   the gate *rejects* it - a gate that accepts anything is not a gate. See ADR 0004.
2. **Humans own every gate.** Each stage ends in a LangGraph `interrupt()`; the run cannot continue
   until a named human approves or rejects it. Escalated gates require an approver with authority -
   enforced, not advisory. See ADR 0002 and ADR 0005.
3. **The agent proposes; humans dispose.** The code agent emits a unified diff and never touches a
   working tree. `GET /runs/{id}/patch` only hands it over after every gate cleared. See ADR 0006.

## Architecture

```
 Jira/GitHub ticket (mock store)        MCP tool layer (stdio)
            |                          tickets | repo sandbox | knowledge | patch validation
            v                                   ^
  +--------------------------------------------------------+
  |  LangGraph pipeline                                    |
  |                                                        |
  |  intake --> [gate] --> design --> [gate] --> code      |
  |     |         ^          |          ^        |         |
  |     |      interrupt     |       interrupt   |         |
  |     v                    v                    v         |
  |  guardrails (confidence, severity inversion, injection,|
  |  patch size, forbidden paths, review findings)         |
  |                          |                             |
  |                     review --> [gate] --> finalize     |
  +--------------------------------------------------------+
            |                    |                  |
      tracer (cost, latency)  audit log      outbox -> worker
            |                    |                  |
   dashboard / /metrics     Postgres/JSONB    gate notifications
```

Every stage writes an audit entry. Every agent call writes a span with tokens, latency and dollars.
The per-run report (`/runs/{id}/summary`) is the one-page "what the agent did and how we know".

## Quick start (uv, no Docker)

```bash
uv sync --extra dev --extra anthropic     # uv owns the venv and the lockfile
uv run pytest -q                          # 112 tests, offline, no API key
uv run deliveryops-eval                   # eval gate on the offline baseline (expected: FAIL)
uv run vulture                            # dead-code check (config in pyproject.toml)
uv run deliveryops-api                    # http://localhost:8000/docs
```

Drive a ticket through the four gates:

```bash
curl -s -X POST localhost:8000/tickets -H 'content-type: application/json' \
  -d '{"title":"Login page returns 500 after password reset",
       "description":"Users who change their password get a 500 error when they log in again."}'
# -> status "awaiting_approval", awaiting.stage "intake", with reasons and a triage summary

curl -s -X POST localhost:8000/runs/<id>/decision -H 'content-type: application/json' \
  -d '{"approver":"sukhdev","approve":true,"comment":"ok","role":"senior"}'
```

Or stay in the terminal:

```bash
uv run deliveryops-approve <run_id>                       # show the gate, reasons, artifacts
uv run deliveryops-approve <run_id> --approve --role senior
uv run deliveryops-approve <run_id> --reject --comment "needs a rollback plan"
```

Real model:

```bash
export ANTHROPIC_API_KEY=... DELIVERYOPS_LLM=anthropic DELIVERYOPS_MODEL=claude-sonnet-4-6
uv run deliveryops-eval --judge --trials 3 --report eval-report.json
```

## Evals: the release gate

```bash
uv run deliveryops-eval                    # score the configured model, write eval-report.json
uv run deliveryops-eval --baseline-check   # CI self-check: fails if the dumb baseline passes
uv run deliveryops-eval --judge            # add the LLM-as-judge requirements dimension
```

Offline baseline, as measured (`deliveryops-eval`, no API key):

```
model=fake-keyword-llm cases=20 passed=False cost=$0.0000
  type               0.60  (gate 0.85)
  priority           0.70  (gate 0.80)
  estimate           0.62  (gate 0.70)
  requirements       0.07  (gate 0.60)
  safety             1.00  (gate 1.00)
  GATE FAIL  critical case failed: T-002 / T-009 / T-010 / T-015 / T-019
```

The requirements score is 0.07 *by design*: `must_mention` holds terms a triage must synthesise
(`regression`, `rollback`, `idempotency`), not words copied out of the ticket, so echoing the ticket
scores ~nothing. Golden set and rubric rules: `evals/golden/intake.json`, `docs/adr/0004`.

## MCP tool layer

`deliveryops-mcp` serves tickets, a sandboxed repository reader, knowledge search, patch validation
and run status over MCP stdio. The design agent does not import a retriever: it asks
`mcp_server/client.py` for knowledge, and that client calls the same tool functions the server
exposes, so the MCP path and the in-process path cannot drift. Claude Desktop config:

```json
{"mcpServers": {"deliveryops": {"command": "uv",
  "args": ["run", "--directory", "/absolute/path/to/deliveryops", "deliveryops-mcp"]}}}
```

Repository access refuses path traversal and gated files; `validate_patch` parses a diff and reports
secret findings without applying anything.

## Docker Compose

```bash
cp .env.example .env
docker compose up --build      # db (pgvector) + knowledge ingest + api + outbox worker
```

- `db`: Postgres 16 with pgvector, schema applied on first boot (`db/schema.sql`).
- `knowledge`: one-shot ingestion of the process library into `knowledge_chunks`.
- `api`: FastAPI on :8000, `DELIVERYOPS_RETRIEVER=pgvector` when the database is present.
- `worker`: polls the outbox and delivers gate events (durable, replays after restart).

## Observability

- `GET /dashboard` - runs, gate queue, spend and latency by stage, escalation list (server-rendered,
  no build step).
- `GET /metrics` - calls, tokens, cost, mean/p95 latency, error rate by stage, run counts, plus
  `persisted` totals read back from `agent_calls` (survives an API restart).
- `GET /runs/{id}/summary` - the per-run executive report: what the agent did, what a human decided
  and why, and how we know (gates, audit, injection screening, eval status).
- `GET /runs/{id}/patch` - the approved diff, 409 until every gate has cleared.

Cost is computed from published per-model token prices (`llm/pricing.py`); an unknown model is
reported as unpriced rather than silently costed at zero.

## Layout

```
src/deliveryops/
  agents/        intake, design, code (diff), review; output contracts and diff parsing
  orchestrator/  graph (LangGraph), pipeline facade, outbox events, worker
  guardrails/    policy signals, escalation, injection screening
  evals/         scorers, runner, gate thresholds
  knowledge/     process library, lexical + pgvector retrieval, ingestion
  mcp_server/    MCP tools (stdio) and the in-process client
  db/            repository protocol, Postgres and in-memory implementations, schema
  observability/ tracer (cost/latency) and the executive report
  console/       dashboard rendering, approval CLI
  api/           FastAPI app
evals/golden/    golden tickets
docs/            ARCHITECTURE.md, STATUS.md, adr/ (six decisions)
tests/           112 tests
```

## Deeper reading

- `docs/ARCHITECTURE.md` - components, run lifecycle, gate contract, trust boundaries, data model,
  failure semantics, extension points.
- `docs/STATUS.md` - what was actually run, with output, and what was not.

## Decisions

| ADR | Decision |
| --- | --- |
| 0001 | Evals are the release gate; humans own every gate |
| 0002 | Human gates are LangGraph interrupts, not a polling state machine |
| 0003 | Durable Postgres outbox instead of Redis Streams or LISTEN/NOTIFY |
| 0004 | How the eval gate decides whether a build may ship |
| 0005 | Guardrails take plain signal dicts; escalation changes who may approve |
| 0006 | The code agent proposes patches; humans apply them |

## Known limitations

- **In-memory checkpointer.** Run state is persisted to Postgres, but LangGraph's execution state is
  in memory: restarting the API loses in-flight runs (they remain `awaiting_approval` in the
  database and are not resumable). The `postgres` extra and the checkpointer wiring are the next
  step.
- **No authentication or multi-tenancy.** The API is a single-tenant demo; `role` is asserted, not
  verified by an identity provider.
- **The dashboard is read-only.** Approvals go through the API or the CLI.
- **20 golden tickets.** A floor, not a target; it should grow from real failures.
- **Regex injection screening.** Deterministic and auditable, not complete.
- **No patch execution.** By design (ADR 0006): the human applies the diff.
- **CI is not yet a merge blocker.** It runs on every push and pull request (lint, tests,
  `--baseline-check`), but nothing in branch protection requires it, so a red run does not legally
  block a merge. Marking `eval gate has teeth` required is a repo setting, not code.
- **The real-model gate is skipped, not passed.** Without `ANTHROPIC_API_KEY` the job reports a
  notice and skips the model run. Add the secret to turn it into a real gate.

## What was actually run

See `docs/STATUS.md` for commands and their real output, including what is verified and what is not.
