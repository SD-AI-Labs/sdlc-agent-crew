"""A deterministic offline embedder.

Hashing embeddings are weak but real: they are stable across processes, need no network, and let
the pgvector code path be exercised (and tested) without an API key. Swap in a hosted embedding
model by implementing the same `embed` method.
"""
from __future__ import annotations

import hashlib
import math
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


def to_pgvector(vec: list[float]) -> str:
    """Render a vector for the pgvector text literal format: '[0.1,0.2,...]'."""
    return "[" + ",".join(f"{v:.6f}" for v in vec) + "]"
