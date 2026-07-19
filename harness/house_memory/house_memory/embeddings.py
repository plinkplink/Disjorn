"""Embedder protocol + implementations.

Generalization vs claudette/memory/embeddings.py: no module-level client built
from a `config` import — the API key and model are constructor arguments, so
each resident (and each test) owns its embedder instance explicitly.
"""

from typing import Protocol, runtime_checkable
import hashlib
import math


@runtime_checkable
class Embedder(Protocol):
    def embed_document(self, text: str) -> list[float]:
        """Embed a memory for storage."""
        ...

    def embed_query(self, text: str) -> list[float]:
        """Embed a search query (providers may use a distinct input type)."""
        ...


class VoyageEmbedder:
    """Voyage AI embedder with the document/query input_type split."""

    def __init__(self, api_key: str, model: str = "voyage-3"):
        import voyageai  # deferred so tests never need the client installed/configured

        self._client = voyageai.Client(api_key=api_key)
        self.model = model

    def embed_document(self, text: str) -> list[float]:
        result = self._client.embed([text], model=self.model, input_type="document")
        return result.embeddings[0]

    def embed_query(self, text: str) -> list[float]:
        result = self._client.embed([text], model=self.model, input_type="query")
        return result.embeddings[0]


class StubEmbedder:
    """Deterministic hash-based embedder for tests. NO network.

    Each whitespace token is hashed (sha256, unsalted — stable across
    processes) into one of `dim` buckets; the bucket-count vector is
    L2-normalized. Identical texts embed identically; texts sharing tokens
    land near each other — enough structure for retrieval tests.
    """

    def __init__(self, dim: int = 64):
        self.dim = dim

    def _embed(self, text: str) -> list[float]:
        vec = [0.0] * self.dim
        for token in text.lower().split():
            h = int.from_bytes(hashlib.sha256(token.encode("utf-8")).digest()[:8], "big")
            vec[h % self.dim] += 1.0
        norm = math.sqrt(sum(v * v for v in vec))
        if norm > 0:
            vec = [v / norm for v in vec]
        else:
            vec[0] = 1.0  # empty text: fixed unit vector
        return vec

    def embed_document(self, text: str) -> list[float]:
        return self._embed(text)

    def embed_query(self, text: str) -> list[float]:
        return self._embed(text)
