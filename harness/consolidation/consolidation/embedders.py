"""NullEmbedder — the proof that consolidation never embeds.

`house_memory.MemoryStore` requires an `Embedder`, but consolidation only ever
*reads* a store (`export_all`, `count`) — it never calls `remember`, `recall`,
or `import_all`, the only paths that embed. Injecting an embedder that raises
on use turns "consolidation makes no network calls" from a claim into a
tripwire: any code path that tried to embed would blow up in tests.
"""

from __future__ import annotations


class EmbedderUsedError(RuntimeError):
    """Raised if consolidation ever attempts to embed. It must not."""


class NullEmbedder:
    """Satisfies the house_memory `Embedder` protocol; refuses to embed.

    Construction of a `MemoryStore` touches the collection (`get`) but never
    embeds, so a NullEmbedder-backed store opens fine for read-only work.
    """

    def embed_document(self, text: str):  # pragma: no cover - guard
        raise EmbedderUsedError(
            "consolidation is read-only and must never embed a document "
            "(proposes-never-acts / no network calls)"
        )

    def embed_query(self, text: str):  # pragma: no cover - guard
        raise EmbedderUsedError(
            "consolidation is read-only and must never embed a query "
            "(proposes-never-acts / no network calls)"
        )
