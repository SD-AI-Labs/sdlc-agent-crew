# ADR 0001: Evals are the release gate; humans own every approval gate

Status: accepted

## Context
Agents are non-deterministic. "It worked when I tried it" is not a release criterion,
and an agent acting on delivery work without a human checkpoint is not acceptable.

## Decision
1. Every agent has a golden set (`evals/golden/`) and per-dimension thresholds
   (`evals/gates.py`). CI runs the gate on every push and pull request. Making it *block* a merge
   also needs the check marked required in branch protection - that is configuration, not code, and
   it is listed as outstanding in `docs/STATUS.md`.
2. Malformed model output counts as a failure, never a skipped case.
3. Every stage ends in a human approval gate. Guardrails (`guardrails/policy.py`) decide
   what is escalated and record why. Autonomy can be earned later by relaxing policy
   with eval evidence, not by default.
4. Every transition is written to an append-only audit log.
5. LLM access sits behind a small interface so providers are swappable and tests run
   offline.

## Consequences
- The gate is only as good as the golden set. It must grow from real failures.
- A deterministic keyword baseline (`FakeLLM`) is deliberately weak: a test asserts the
  gate blocks it, which proves the gate has teeth.
- Orchestration was a plain state machine at first; ADR 0002 replaces it with a LangGraph graph
  whose gates are interrupts. The "evals are the release gate, humans own the gates" decision here
  still stands.
