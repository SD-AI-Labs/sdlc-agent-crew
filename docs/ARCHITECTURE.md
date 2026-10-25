# Architecture

DeliveryOps is a ticket-to-patch pipeline with a human gate after every stage, a durable record of
everything that happened, and an eval suite that decides whether the agents are allowed to ship.

This document describes how the pieces fit, what is guaranteed where, and where the seams are for
changing it. Decisions and their trade-offs live in `docs/adr/`.

## 1. The shape

```
                    +---------------------------+
  ticket text  ---> |  API (FastAPI)            | <--- approval CLI / dashboard
  (Jira or mock)    |  /tickets /runs /metrics  |
                    +------------+--------------+
                                 |
                                 v
                    +---------------------------+
                    |  Pipeline facade          |   run ids, thread ids, resume rules,
                    |  (orchestrator/pipeline)  |   persistence sync, outbox publishing
                    +------------+--------------+
                                 |
                                 v
   +---------------------------------------------------------------+
   |  LangGraph StateGraph                                         |
   |                                                               |
   |  intake ---> [gate] ---> design ---> [gate] ---> code         |
   |                                        |          |           |
   |                                        |          v           |
   |                                        |       [gate] ---> review ---> [gate]
   |                                        |                              |
   |                                        v                              v
   |                                    rejected                        finalize
   |                                                                   (approved)
   +---------------------------------------------------------------+
        |                    |                     |
        v                    v                     v
   guardrails           tracer                outbox
   (policy signals)     (spans + $)           (durable events)
        |                    |                     |
        v                    v                     v
   GateDecision        repository            worker process
   (risk, reasons,     (Postgres/JSONB)      (gate notifications)
    escalate)               |
                            v
                     audit_events / agent_calls / escalations
```

The graph is fixed. Everything that varies — model, prompts, thresholds, tools, knowledge corpus —
is injected. Adding a stage means adding an agent node plus a gate node and wiring two edges.

## 2. Components

| Component | Module | Responsibility | Does not do |
| --- | --- | --- | --- |
| API | `api/main.py` | HTTP surface, validation, error mapping (404/403/409) | business rules |
| Pipeline facade | `orchestrator/pipeline.py` | start/resume/view, role checks for escalations, persistence sync, event publishing | graph internals |
| Graph | `orchestrator/graph.py` | stage ordering, gate interrupts, routing on reject/fail | persistence, policy detail |
| Agents | `agents/*.py` | prompt + output contract + validation, per-stage artifacts | deciding who approves |
| Guardrails | `guardrails/policy.py`, `guardrails/injection.py` | risk, reasons, escalation from signal dicts | calling models |
| Evals | `evals/*` | golden set scoring, gate thresholds, release decision | touching production data |
| MCP tools | `mcp_server/server.py` | tickets, repo sandbox, knowledge search, patch validation, run status | holding state |
| Integrations | `integrations/jira.py`, `integrations/github.py` | Jira Cloud intake, read-only GitHub repo grounding | writes to either system |
| Knowledge | `knowledge/*` | process library, lexical + pgvector retrieval, ingestion | deciding relevance policy |
| Repository | `db/repository.py` | run/audit/telemetry/escalation persistence | orchestration |
| Observability | `observability/tracer.py`, `report.py` | spans, cost/latency aggregates, executive report | alerting |
| Console | `console/dashboard.py`, `console/approve.py` | read-only dashboard, terminal approval | mutation outside the API |

Dependency direction is one-way: API → pipeline → graph → agents/guardrails; agents reach data
through the MCP client, not by importing retrievers or filing systems directly. Real external
systems are env-configured adapters behind those same seams (`integrations/`, ADR 0007): Jira
Cloud fills the ticket-store surface, a strictly read-only GitHub reader grounds the code agent
and the repo tools, and the mocks stay the default when no `JIRA_*`/`DELIVERYOPS_GITHUB_*` is set.

## 3. Run lifecycle

```
submitted -> running -> awaiting_approval -> (approved|rejected) ...
                  \-> failed        (any agent contract violation)
```

States, and who sets them:

| State | Set by | Meaning |
| --- | --- | --- |
| `submitted` | `Pipeline.start` (repo write before invoke) | run row exists; nothing else yet |
| `awaiting_approval` | derived from a pending `interrupt` | a human must answer, with reasons |
| `approved` | `finalize` node | all four gates cleared; patch is releasable |
| `rejected` | any gate node on a negative decision | the run stops; later stages never run |
| `failed` | any agent node on `AgentOutputError` | malformed output never reaches a gate |

`Pipeline.view` derives `awaiting_approval` from the checkpoint's interrupt, not from a stored flag,
so the state cannot disagree with the graph. The repository row mirrors it for querying.

### The gate contract

A gate node does exactly three things: compute signals, `interrupt(payload)`, then write the
decision. Nothing before the interrupt may have a side effect, because LangGraph re-executes the
node body on resume. The payload is the approver's whole context:

```json
{
  "run_id": "...", "stage": "code", "risk": "high", "escalate": true,
  "reasons": ["patch touches gated paths: .github/"],
  "summary": {"files_touched": ["..."], "patch_lines": 12, "diff": "--- a/..."},
  "injection_phrases": ["ignore all previous instructions"]
}
```

Resume carries `{approver, approve, comment, role}`. If the gate is escalated, a role outside
`senior|staff|principal|security|director` is refused with 403 before the graph is touched.

## 4. Trust boundaries

| Boundary | Control |
| --- | --- |
| Ticket text → model | injection screening (deterministic regex families), escalation at `critical` risk, matched phrases recorded in the gate payload and audit log |
| Model → pipeline | strict output contracts; JSON-only; enum/range checks; malformed output fails the run |
| Model → repository | the code agent emits a diff; files are taken from the diff headers, not from the model's claim; patches touching CI, evals, secrets or infra escalate |
| Model → filesystem | no filesystem access at all (ADR 0006); `read_repo_file` is a sandboxed MCP tool with traversal and `.env` refusal |
| Human → pipeline | every stage needs a named approver; escalated stages need authority; every decision is audited |
| Eval → release | per-dimension thresholds, critical-case hard fails, safety dimension, cost budget (ADR 0004) |

## 5. Data model (`db/schema.sql`)

| Table | Purpose | Notes |
| --- | --- | --- |
| `runs` | one row per run, pipeline snapshot in `state` JSONB | `status`, `stage`, `outcome` for querying |
| `audit_events` | append-only log of every agent action and human decision | trigger blocks UPDATE/DELETE |
| `agent_calls` | per-call telemetry: model, tokens, latency, cost, ok | FK to `runs` — the run row is written before the first span |
| `escalations` | escalation queue with risk and reasons | one row per escalated stage, written when the gate opens |
| `outbox` | durable handoff events | unique on `(run_id, type, stage)`; `delivered_at` set by the worker |
| `knowledge_chunks` | process docs + 1024-dim embedding | unique on `source`; ivfflat cosine index |

Cost is computed at write time from `llm/pricing.py`; an unknown model is reported as unpriced
rather than silently costed at zero.

## 6. Failure semantics

| Failure | Behaviour | Why |
| --- | --- | --- |
| Model returns non-JSON or violates the contract | run → `failed`, no gate opens, audit entry written | a gate you can walk past with junk is not a gate |
| Postgres unavailable mid-run | API surfaces the error; in-memory repo keeps offline use working | persistence is not allowed to silently drop audit rows |
| API restarts mid-run | with the Postgres checkpointer (default when `DATABASE_URL` is set) the run resumes from the open gate; with `DELIVERYOPS_CHECKPOINTER=memory` it stays `awaiting_approval` and cannot be resumed | durable execution state, not a process-memory promise |
| Worker down | events accumulate undelivered in `outbox`; delivery resumes on restart | at-least-once, ordered by insert id |
| Eval gate fails | CI job fails, merge is blocked (once the check is required) | evals, not opinions |

## 7. Extension points

| Goal | Change |
| --- | --- |
| New stage | add an agent in `agents/`, a node pair in `graph.py`, a policy entry in `POLICIES`, goldens for it |
| Different model | configuration only: `DELIVERYOPS_LLM=chat` + `DELIVERYOPS_PROVIDER`/`DELIVERYOPS_MODEL` (LangChain `init_chat_model`); a genuinely new client implements `complete()` in `llm/` |
| Another tracker / repo host | add an adapter in `integrations/` with the same surface (Jira Cloud and read-only GitHub ship today) |
| Different knowledge corpus | replace `knowledge/library.py` or ingest externally; retrieval is unchanged |
| Stricter autonomy policy | edit `POLICIES` thresholds; the escalation path already exists |
| Durable execution state | shipped: Postgres checkpointer via `DELIVERYOPS_CHECKPOINTER` (default with a database) |
| Dashboard with real interactivity | keep `/metrics` and `/runs` as the contract, replace `console/dashboard.py` |

## 8. What this architecture deliberately does not do

- No auto-approve path, not even for trivial changes. Autonomy would be a policy change backed by
  eval evidence, not a code shortcut.
- No patch execution. No sandbox is built because no code is applied.
- No multi-tenancy or auth. `role` is a claim, not an identity.
- No event bus beyond the outbox. One database is the consistency boundary on purpose (ADR 0003).
