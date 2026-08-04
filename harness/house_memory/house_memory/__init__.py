"""house_memory — shared resident memory library for the Disjorn harness.

WP-H6: claudette/memory generalized into per-resident instance-based stores
(explicit paths, injected embedder, unified retrieval log), plus the spine
loader (WP-H7 read side) and the WP-H11 parallel-run migration tooling.
"""

from house_memory.embeddings import Embedder, StubEmbedder, VoyageEmbedder
from house_memory.migration import DiffReport, MigrationReport, QueryDiff, migrate, parallel_diff
from house_memory.retrieval_log import (
    CALLER_CONSOLIDATION,
    CALLER_DAYDREAM,
    CALLER_INCUBATION,
    CALLER_SELF_QUERY,
    CALLER_SERVICE,
    CALLER_WRITE_DEDUP,
    HEAT_CALLERS,
    KNOWN_CALLERS,
    RetrievalLog,
    RetrievalRecord,
    UnknownCaller,
    read_records,
)
from house_memory.schema import (
    Memory,
    normalize_subject,
    normalize_tag,
    normalize_tags,
)
from house_memory.spine import (
    BUILD_SEAT,
    RESIDENT_SEAT,
    SEATS,
    Spine,
    SpineEntry,
)
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
    "CALLER_SERVICE",
    "CALLER_SELF_QUERY",
    "CALLER_CONSOLIDATION",
    "CALLER_DAYDREAM",
    "CALLER_INCUBATION",
    "CALLER_WRITE_DEDUP",
    "KNOWN_CALLERS",
    "HEAT_CALLERS",
    "UnknownCaller",
    "Spine",
    "SpineEntry",
    "SEATS",
    "RESIDENT_SEAT",
    "BUILD_SEAT",
    "migrate",
    "parallel_diff",
    "MigrationReport",
    "DiffReport",
    "QueryDiff",
    "normalize_subject",
    "normalize_tag",
    "normalize_tags",
]
