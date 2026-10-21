# ADR 0005: Guardrails take plain signal dicts, and escalation changes who may approve

Status: accepted

## Context
Guardrails that live inside agents are invisible in the audit log and impossible to eval. They also
tend to be written as long `if` chains that drift from the policy they claim to implement.

## Decision
1. `evaluate(stage, signals)` takes a **plain dict** and returns a frozen `GateDecision(stage,
   requires_human, escalate, risk, reasons)`. Adding a signal never changes a signature.
2. Signals understood today: `confidence`, `priority`, `estimate_points`, `severity_inversion`,
   `injection_hits`, `open_questions`, `files_touched`, `patch_lines`, `critical_findings`,
   `verdict`.
3. **Everything goes to a human.** There is no auto-approve path. `reasons` explains what the human
   is being asked to look at; escalation marks the subset that needs authority.
4. **Escalation is enforced, not advisory.** An escalated gate rejects an approver whose role is not
   senior (`senior|staff|principal|security|director`). This is asserted in tests and returned as
   HTTP 403.
5. **Injection screening is deterministic** (`guardrails/injection.py`): regex families for
   instruction override, persona hijack, fake system prompt, output steering and gate bypass.
   A hit escalates at `critical` risk and records the matched phrases.
6. **Severity inversion** is flagged when ticket text looks urgent (outage, data loss, sev1) but the
   triage says otherwise. That is a disagree-with-the-model signal, which is exactly what a human
   gate is for.
7. **Changing the gate is itself a gated change**: patches touching `evals/golden/`, `.github/`,
   secrets or infrastructure escalate at `critical`.

## Consequences
- The policy is unit-tested without a model, so guardrail behaviour is deterministic in CI.
- The escalation queue is queryable (`escalations` table, dashboard panel, `/metrics`), and the
  worker emits delivery events for it.
- Regexes are a first line of defence, not a complete defence. A model-based classifier belongs in
  the same signal slot when there is a golden set for it.
