"""Token pricing so every call can be reported in dollars, not just tokens.

Prices are USD per 1M tokens. Unknown models cost 0.0 and are reported as unpriced, which is
visible in the dashboard rather than silently wrong.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Price:
    input_per_mtok: float
    output_per_mtok: float


PRICES: dict[str, Price] = {
    # Anthropic (list prices, USD per 1M tokens)
    "claude-sonnet-4-6": Price(3.00, 15.00),
    "claude-opus-4-6": Price(15.00, 75.00),
    "claude-haiku-4-5": Price(0.80, 4.00),
    # Offline / test doubles are free by definition.
    "fake-keyword-llm": Price(0.0, 0.0),
    "scripted": Price(0.0, 0.0),
    "oracle": Price(0.0, 0.0),
}

# Prefix match so dated snapshots (e.g. claude-sonnet-4-6-20260101) resolve.
_FALLBACK_PREFIXES: tuple[tuple[str, Price], ...] = (
    ("claude-opus", PRICES["claude-opus-4-6"]),
    ("claude-sonnet", PRICES["claude-sonnet-4-6"]),
    ("claude-haiku", PRICES["claude-haiku-4-5"]),
)


def price_for(model: str) -> Price | None:
    if model in PRICES:
        return PRICES[model]
    for prefix, price in _FALLBACK_PREFIXES:
        if model.startswith(prefix):
            return price
    return None


def cost_usd(model: str, input_tokens: int, output_tokens: int) -> float:
    p = price_for(model)
    if p is None:
        return 0.0
    return (input_tokens / 1_000_000) * p.input_per_mtok + (
        output_tokens / 1_000_000
    ) * p.output_per_mtok


def is_priced(model: str) -> bool:
    return price_for(model) is not None

# Embedding prices, USD per 1M tokens (list prices). The offline hash embedder is free.
EMBEDDING_PRICES: dict[str, float] = {
    "text-embedding-3-small": 0.02,
    "text-embedding-3-large": 0.13,
    "text-embedding-ada-002": 0.10,
    "hash-ngram-1024": 0.0,
}


def embedding_cost_usd(model: str, tokens: int) -> float:
    """Cost of an embedding call set. Unknown models cost 0.0 and should be added above."""
    price = EMBEDDING_PRICES.get(model)
    return 0.0 if price is None else (tokens / 1_000_000) * price
