from __future__ import annotations

import os

from .base import LLM
from .fake import FakeLLM


def get_llm() -> LLM:
    """DELIVERYOPS_LLM=anthropic|fake (default: fake, so nothing breaks without a key)."""
    provider = os.getenv("DELIVERYOPS_LLM", "fake").lower()
    if provider == "anthropic":
        from .anthropic_client import AnthropicLLM

        return AnthropicLLM()
    return FakeLLM()
