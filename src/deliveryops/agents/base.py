"""Shared agent plumbing: output contracts, meta capture, and the error type gates understand."""
from __future__ import annotations

from ..llm.base import LLMResult


class AgentOutputError(ValueError):
    """Raised when model output does not satisfy the agent's output contract."""


def parse_json(res: LLMResult, agent: str) -> dict:
    try:
        raw = res.json()
    except ValueError as e:
        raise AgentOutputError(f"{agent}: output is not JSON ({e})") from e
    if not isinstance(raw, dict):
        raise AgentOutputError(f"{agent}: output must be a JSON object")
    return raw


def require(raw: dict, agent: str, *keys: str) -> None:
    missing = [k for k in keys if k not in raw]
    if missing:
        raise AgentOutputError(f"{agent}: missing keys {missing}")


def confidence(value: object, agent: str) -> float:
    try:
        c = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError) as e:
        raise AgentOutputError(f"{agent}: confidence is not a number") from e
    if not 0.0 <= c <= 1.0:
        raise AgentOutputError(f"{agent}: confidence {c} out of range")
    return c


def str_list(value: object, agent: str, field: str) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise AgentOutputError(f"{agent}: {field} must be a list of strings")
    return value


def complete_structured(llm, system: str, user: str, schema, agent: str,
                        *, max_tokens: int = 1000) -> tuple[dict, dict]:
    """One completion path for every agent: provider structured output when the LLM offers it
    (ChatModelLLM.structured), plain text + JSON parsing otherwise (FakeLLM, tests). Either
    way the agent's validator below enforces the contract - structured output is transport
    reliability, not the guardrail.
    """
    structured = getattr(llm, "structured", None)
    if callable(structured):
        try:
            raw, meta = structured(system, user, schema, max_tokens=max_tokens)
        except Exception:
            raw = None
        if isinstance(raw, dict):
            return raw, meta
    res = llm.complete(system, user, max_tokens=max_tokens)
    return parse_json(res, agent), meta_of(res)


def meta_of(res: LLMResult) -> dict:
    return {
        "model": res.model or "unknown",
        "input_tokens": res.input_tokens,
        "output_tokens": res.output_tokens,
        "latency_ms": res.latency_ms,
        "cost_usd": res.cost_usd,
    }
