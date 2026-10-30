from __future__ import annotations

import os

from .base import LLM
from .fake import FakeLLM


def get_llm() -> LLM:
    """DELIVERYOPS_LLM=fake (default, offline) or a real provider via init_chat_model.

    `chat` (or a provider name for back-compat: anthropic|openai|google|google_genai) builds a
    ChatModelLLM; DELIVERYOPS_PROVIDER and DELIVERYOPS_MODEL select the backend and model, so
    the pipeline is model-independent - swapping providers is configuration, not code.

    An unrecognised value is a loud error, never a silent FakeLLM: a typo in production
    config must not quietly swap a keyword toy in for the real model.
    """
    mode = os.getenv("DELIVERYOPS_LLM", "fake").lower()
    if mode == "fake":
        return FakeLLM()
    providers = {"anthropic", "openai", "google", "google_genai"}
    if mode == "chat" or mode in providers:
        from .chat_model import ChatModelLLM

        return ChatModelLLM(provider=None if mode == "chat" else mode)
    raise ValueError(
        f"unknown DELIVERYOPS_LLM {mode!r}: expected 'fake', 'chat', or one of "
        f"{sorted(providers)}"
    )
