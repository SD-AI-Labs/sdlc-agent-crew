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


def meta_of(res: LLMResult) -> dict:
    return {
        "model": res.model or "unknown",
        "input_tokens": res.input_tokens,
        "output_tokens": res.output_tokens,
        "latency_ms": res.latency_ms,
        "cost_usd": res.cost_usd,
    }
