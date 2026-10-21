# ADR 0002: Human gates are LangGraph interrupts, not a polling state machine

Status: accepted (supersedes the "plain state machine" note in ADR 0001)

## Context
A gate has to stop the run, survive until a human answers, and then continue from exactly where it
stopped. The first implementation kept a Python dict of runs and a status enum. That works for one
process and one stage, and it breaks the moment the process restarts or a stage needs retries,
branching or parallel steps.

## Decision
1. The pipeline is a LangGraph `StateGraph`: `intake -> gate -> design -> gate -> code -> gate ->
   review -> gate -> finalize`.
2. Each gate is `interrupt(payload)`. The payload is what the approver sees: stage, risk,
   escalation flag, reasons, and the stage's artifacts. The graph resumes with
   `Command(resume={...})`.
3. Gate nodes have **no side effects before `interrupt()`**. LangGraph re-executes the node body on
   resume, so any write placed above the interrupt would happen twice. Artifacts, spans and audit
   entries are produced by the agent node before the gate, never inside it.
4. Escalation is not a second graph: it is a property of the payload. An escalated gate needs an
   approver role with authority (`senior|staff|principal|security|director`); the pipeline rejects
   a junior approval with a 403 instead of silently accepting it.
5. LangGraph owns execution state; the repository owns durable state (`runs.state`,
   `audit_events`, `agent_calls`). The API can be restarted without losing the record of what
   happened.

## Consequences
- Resume-after-restart needs a durable checkpointer. Today the checkpointer is `InMemorySaver`; the
  Postgres checkpointer is available behind the `postgres` extra and is the next step for
  multi-process deployment. Until then, an API restart loses in-flight runs (they remain visible in
  the database as `awaiting_approval` but cannot be resumed). Recorded as a known limitation.
- The graph structure is fixed and legible; behaviour lives in agents and policy. Adding a stage
  means adding a node pair, not editing conditional logic in a dozen places.
- Tests drive the real graph through real interrupts, so the gate contract is tested as it runs.
