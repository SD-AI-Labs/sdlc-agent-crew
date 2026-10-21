"""Provider-agnostic LLM interface. Agents depend on this, never on a vendor SDK."""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Protocol

from .pricing import cost_usd


@dataclass(frozen=True)
class LLMResult:
    text: str
    input_tokens: int = 0
    output_tokens: int = 0
    latency_ms: float = 0.0
    model: str = ""

    @property
    def cost_usd(self) -> float:
        return cost_usd(self.model, self.input_tokens, self.output_tokens)

    def json(self) -> dict:
        """Parse model output as JSON, tolerating markdown code fences."""
        t = self.text.strip()
        if t.startswith("```"):
            t = t.strip("`")
            if t.lower().startswith("json"):
                t = t[4:]
        return json.loads(t.strip())


class LLM(Protocol):
    name: str

    def complete(
        self, system: str, user: str, *, max_tokens: int = 1000, temperature: float = 0.0
    ) -> LLMResult: ...
