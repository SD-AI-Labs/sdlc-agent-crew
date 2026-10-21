# ADR 0004: How the eval gate decides whether a build may ship

Status: accepted

## Context
The first gate compared averages against fixed thresholds on 15 tickets. That version had four
holes: one label per subjective field, no penalty for catastrophic mistakes, a requirements
dimension the naive baseline maxed out for free, and no reproducibility metadata. A gate with those
holes either blocks everything or, worse, gets lowered until it blocks nothing.

## Decision
1. **Acceptable sets for subjective fields.** `priority` and `type` accept a set of correct
   answers (`priority: ["medium","high"]`). Exact matching is reserved for fields with one right
   answer.
2. **Prescriptive requirement terms.** `must_mention` contains terms a competent triage must
   *synthesise* (`regression`, `rollback`, `idempotency`), not words already in the ticket. Echoing
   the ticket back scores ~0, which is why the naive baseline now scores 0.07 there instead of 1.00.
3. **Critical cases fail the gate outright.** A case marked `critical: true` (production outage,
   data loss, authorization bug) fails the whole run if `type` or `priority` is wrong, regardless of
   the means. Severity is not an average.
4. **Safety is measured, not assumed.** Every case flagged `injection: true` must produce an
   escalating guardrail decision; the threshold is 1.00.
5. **Trials and medians.** `--trials N` scores each case N times and takes the median, and the
   Anthropic client runs at `temperature=0`. A gate that flips on sampling noise is not a gate.
6. **Reproducibility metadata.** The report records the model, a SHA of the golden set, a SHA of the
   intake system prompt, cost and latency, so two runs can be compared without guessing what
   changed.
7. **Cost is a gate dimension.** A gate run over `MAX_COST_USD` fails: silent cost blow-ups in an
   eval harness look exactly like slow infrastructure.
8. **Never lower a threshold to make a run pass.** Fix the agent or add the failing case to the
   golden set. The `--baseline-check` job exists so this stays honest in CI: it fails the build if
   the naive keyword baseline ever passes.

## Consequences
- Golden sets must grow from real failures; 20 cases is a floor, not a target.
- LLM-as-judge is opt-in (`--judge`) and reported as a separate dimension, because a judge that
  agrees with the agent it grades is not evidence.
- The offline baseline fails by design, so CI runs the *real* gate only where a model key exists,
  and `--baseline-check` everywhere else.
