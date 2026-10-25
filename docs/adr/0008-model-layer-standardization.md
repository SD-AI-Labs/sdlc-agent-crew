# ADR 0008: Model-independent LLM layer; full agent loops evaluated and deferred

## Context

The LLM layer was a raw Anthropic SDK behind a hand-rolled protocol, and agents got JSON out of
models by asking nicely in the prompt and stripping markdown fences. Against LangChain/LangGraph
v1 (current as of 2026-10) this was the one layer that read as hand-rolled rather than
deliberate. The requirement set here is model independence: swapping providers must be
configuration, not code.

## Decision

- **One code path for every provider.** `llm/chat_model.py` wraps LangChain v1's
  `init_chat_model` (`DELIVERYOPS_PROVIDER` + `DELIVERYOPS_MODEL`; `DELIVERYOPS_LLM=chat`, with
  provider names accepted for back-compat). Provider packages are optional extras
  (`anthropic` / `openai` / `google`). The internal `LLM` protocol is unchanged, so the
  deterministic `FakeLLM` test strategy and every agent contract are untouched.
- **Structured output is the transport; validators stay the contract.** Each agent declares a
  Pydantic schema. `ChatModelLLM.structured()` tries the provider's native JSON-schema mode
  first, falls back to tool-calling structured output, then to plain text - and in every case
  the existing hand-written validators enforce the output contract and fail the run closed.
  Fence-stripping is now the last resort, not the mechanism. Token usage (and therefore cost
  tracing) is preserved on every path.
- **`create_agent` tool-calling loops and middleware were evaluated and deliberately deferred.**
  The four agents are single-shot by design: a gated delivery pipeline wants deterministic,
  inspectable contracts, and a tool loop buys autonomy this system intentionally withholds
  (ADR 0001, ADR 0006). Revisit if a stage ever needs genuine multi-step tool use; the
  middleware ecosystem (HITL, PII, summarization) only applies on that path.
- **LangGraph Studio support** via `langgraph.json` and `orchestrator/factory.py`: the same
  compiled graph the API serves opens in Studio (`langgraph dev`) for visual inspection and
  checkpoint time-travel.

## Consequences

The model layer follows current framework standards without changing the architecture story.
Cost: a langchain dependency in the provider extras (the core stays langchain-free for tests
and CI). Structured-output fallbacks are silent-by-design at runtime but always end at the same
validators, so a provider silently degrading changes reliability, not correctness.
