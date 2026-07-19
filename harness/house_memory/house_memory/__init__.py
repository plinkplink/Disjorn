"""house_memory — shared resident memory library for the Disjorn harness.

WP-H6: claudette/memory generalized into per-resident instance-based stores
(explicit paths, injected embedder, unified retrieval log), plus the spine
loader (WP-H7 read side) and the WP-H11 parallel-run migration tooling.
"""

from house_memory.embeddings import Embedder, StubEmbedder, VoyageEmbedder
from house_memory.migration import DiffReport, MigrationReport, QueryDiff, migrate, parallel_diff
from house_memory.retrieval_log import RetrievalLog, RetrievalRecord, read_records
from house_memory.schema import (
    Memory,
    normalize_subject,
    normalize_tag,
    normalize_tags,
)
from house_memory.spine import Spine, SpineEntry
from house_memory.store import MemoryStore

__all__ = [
    "Embedder",
    "StubEmbedder",
    "VoyageEmbedder",
    "Memory",
    "MemoryStore",
    "RetrievalLog",
    "RetrievalRecord",
    "read_records",
    "Spine",
    "SpineEntry",
    "migrate",
    "parallel_diff",
    "MigrationReport",
    "DiffReport",
    "QueryDiff",
    "normalize_subject",
    "normalize_tag",
    "normalize_tags",
]
