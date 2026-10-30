from __future__ import annotations

import os

from .base import LLM
from .fake import FakeLLM


def get_llm() -> LLM:
    """DELIVERYOPS_LLM=fake (default, offline) or a real provider via init_chat_model.

    `chat` (or a provider name for back-compat: anthropic|openai|google|google_genai) builds a
    ChatModelLLM; DELIVERYOPS_PROVIDER and DELIVERYOPS_MODEL select the backend and model, so
    the pipeline is model-independent - swapping providers is configuration, not code.
    """
    mode = os.getenv("DELIVERYOPS_LLM", "fake").lower()
    providers = {"anthropic", "openai", "google", "google_genai"}
    if mode == "chat" or mode in providers:
        from .chat_model import ChatModelLLM

        return ChatModelLLM(provider=None if mode == "chat" else mode)
    return FakeLLM()
