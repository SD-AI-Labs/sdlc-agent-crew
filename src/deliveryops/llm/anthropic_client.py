"""Anthropic-backed LLM. Requires `pip install -e ".[anthropic]"` and ANTHROPIC_API_KEY."""
from __future__ import annotations

import os
import time

from .base import LLMResult


class AnthropicLLM:
    def __init__(self, model: str | None = None) -> None:
        import anthropic  # imported lazily so the core runs without the SDK

        self._client = anthropic.Anthropic()
        self.name = model or os.getenv("DELIVERYOPS_MODEL", "claude-sonnet-4-6")

    def complete(
        self, system: str, user: str, *, max_tokens: int = 1000, temperature: float = 0.0
    ) -> LLMResult:
        start = time.perf_counter()
        resp = self._client.messages.create(
            model=self.name,
            max_tokens=max_tokens,
            temperature=temperature,
            system=system,
            messages=[{"role": "user", "content": user}],
        )
        text = "".join(b.text for b in resp.content if b.type == "text")
        return LLMResult(
            text=text,
            input_tokens=resp.usage.input_tokens,
            output_tokens=resp.usage.output_tokens,
            latency_ms=(time.perf_counter() - start) * 1000,
            model=self.name,
        )
