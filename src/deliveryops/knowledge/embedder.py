"""Embedding backends for the knowledge layer.

`HashEmbedder` is the offline default: deterministic, key-free, and weak - it exists so tests
and CI never need an API key. `LangChainEmbedder` wires any provider LangChain supports through
`init_embeddings`, configured by env exactly like the LLM layer (no provider is hard-coded):

  DELIVERYOPS_EMBEDDER=hash|langchain            (default: hash)
  DELIVERYOPS_EMBED_PROVIDER=openai|...          (default: openai)
  DELIVERYOPS_EMBED_MODEL=text-embedding-3-small

The provider package is an optional extra (e.g. `pip install deliveryops[openai]`), so a missing
one fails loudly at first embed call, never at import time.
"""
from __future__ import annotations

import hashlib
import math
import os
import re

_TOKEN = re.compile(r"[a-z0-9]+")

DIM = 1024


class HashEmbedder:
    name = "hash-ngram-1024"
    dim = DIM

    def embed(self, text: str) -> list[float]:
        vec = [0.0] * self.dim
        tokens = _TOKEN.findall(text.lower())
        for tok in tokens:
            for feature, weight in ((tok, 1.0), (tok[:4], 0.3)):
                h = int.from_bytes(hashlib.blake2b(feature.encode(), digest_size=8).digest(), "big")
                vec[h % self.dim] += weight
        norm = math.sqrt(sum(v * v for v in vec))
        if norm:
            vec = [v / norm for v in vec]
        return vec


class LangChainEmbedder:
    """Real embeddings via langchain's `init_embeddings`; provider and model come from env."""

    def __init__(self, provider: str, model: str) -> None:
        self.provider = provider
        self.model = model
        self.name = f"langchain-{provider}-{model}"
        self._client = None

    def _load(self):
        if self._client is None:
            try:
                from langchain.embeddings import init_embeddings
            except ImportError as e:
                raise RuntimeError(
                    "DELIVERYOPS_EMBEDDER=langchain needs the provider package, e.g. "
                    f"`pip install deliveryops[{self.provider}]`"
                ) from e
            self._client = init_embeddings(self.model, provider=self.provider)
        return self._client

    def embed(self, text: str) -> list[float]:
        return [float(v) for v in self._load().embed_query(text)]


def get_embedder() -> HashEmbedder | LangChainEmbedder:
    """DELIVERYOPS_EMBEDDER=hash (default, offline) or langchain (real provider via env)."""
    mode = os.getenv("DELIVERYOPS_EMBEDDER", "hash").lower()
    if mode == "langchain":
        return LangChainEmbedder(
            provider=os.getenv("DELIVERYOPS_EMBED_PROVIDER", "openai"),
            model=os.getenv("DELIVERYOPS_EMBED_MODEL", "text-embedding-3-small"),
        )
    return HashEmbedder()


def to_pgvector(vec: list[float]) -> str:
    """Render a vector for the pgvector text literal format: '[0.1,0.2,...]'."""
    return "[" + ",".join(f"{v:.6f}" for v in vec) + "]"
