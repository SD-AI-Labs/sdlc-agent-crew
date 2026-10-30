"""Provider-agnostic chat-model LLM on LangChain v1's init_chat_model.

One code path for every provider: DELIVERYOPS_PROVIDER + DELIVERYOPS_MODEL pick the backend
("anthropic" + "claude-sonnet-4-6", "openai" + "gpt-...", "google_genai" + "gemini-...", ...).
The provider's langchain package must be installed (extras: anthropic / openai / google).

Structured output asks for the provider's native JSON-schema mode first, falls back to
tool-calling structured output, and last to plain text that the agents' own validators still
check - the contract is enforced by the validators either way; structured output only makes
the transport reliable. Token usage is preserved on every path so cost tracing keeps working.
"""
from __future__ import annotations

import os
import time

from .base import LLMResult


def _meta(resp, model: str, latency_ms: float) -> dict:
    usage = getattr(resp, "usage_metadata", None) or {}
    return {
        "model": model,
        "input_tokens": int(usage.get("input_tokens") or 0),
        "output_tokens": int(usage.get("output_tokens") or 0),
        "latency_ms": latency_ms,
        "cost_usd": LLMResult(
            text="", input_tokens=int(usage.get("input_tokens") or 0),
            output_tokens=int(usage.get("output_tokens") or 0), model=model,
        ).cost_usd,
    }


class ChatModelLLM:
    def __init__(self, provider: str | None = None, model: str | None = None,
                 max_retries: int = 2) -> None:
        from langchain.chat_models import init_chat_model

        self.provider = provider or os.getenv("DELIVERYOPS_PROVIDER", "anthropic")
        self.name = model or os.getenv("DELIVERYOPS_MODEL", "claude-sonnet-4-6")
        self._model = init_chat_model(model=self.name, model_provider=self.provider,
                                      max_retries=max_retries)

    def _messages(self, system: str, user: str) -> list:
        from langchain_core.messages import HumanMessage, SystemMessage

        return [SystemMessage(content=system), HumanMessage(content=user)]

    def complete(self, system: str, user: str, *, max_tokens: int = 1000,
                 temperature: float = 0.0) -> LLMResult:
        start = time.perf_counter()
        resp = self._model.bind(max_tokens=max_tokens, temperature=temperature).invoke(
            self._messages(system, user))
        latency = (time.perf_counter() - start) * 1000
        usage = getattr(resp, "usage_metadata", None) or {}
        text = getattr(resp, "text", None)
        if not isinstance(text, str):
            text = str(resp.content)
        return LLMResult(
            text=text,
            input_tokens=int(usage.get("input_tokens") or 0),
            output_tokens=int(usage.get("output_tokens") or 0),
            latency_ms=latency,
            model=self.name,
        )

    def structured(self, system: str, user: str, schema, *,
                   max_tokens: int = 1000) -> tuple[dict, dict]:
        """Return (raw_dict, meta). Native json_schema -> tool-calling -> plain text."""
        messages = self._messages(system, user)
        runners = []
        try:
            runners.append(self._model.with_structured_output(
                schema, method="json_schema", include_raw=True))
        except (TypeError, ValueError, NotImplementedError):
            pass
        runners.append(self._model.with_structured_output(schema, include_raw=True))
        for runner in runners:
            start = time.perf_counter()
            try:
                out = runner.bind(max_tokens=max_tokens).invoke(messages)
            except Exception:
                continue
            parsed = out.get("parsed")
            if parsed is None:
                continue
            raw = parsed.model_dump() if hasattr(parsed, "model_dump") else dict(parsed)
            meta = _meta(out.get("raw"), self.name, (time.perf_counter() - start) * 1000)
            return raw, meta
        # No structured path worked: plain text, and the agent validators decide.
        res = self.complete(system, user, max_tokens=max_tokens)
        return res.json(), {
            "model": res.model, "input_tokens": res.input_tokens,
            "output_tokens": res.output_tokens, "latency_ms": res.latency_ms,
            "cost_usd": res.cost_usd,
        }
