"""WP-H11 parallel-run migration tooling (library + CLI).

Claudette's reversibility requirement, verbatim (HARNESS-PLAN WP-H11):
"her existing store is never converted in place — extract to the new shape,
run OLD and NEW in parallel, diff retrievals, and cut over only when the new
store returns at least what the old one did. Old store retained after cutover
(rotates, never dies)."

- `migrate()` copies every record (including superseded ones) out of an old
  chroma dir into a new MemoryStore. Stored embeddings travel verbatim — no
  re-embedding, no API calls, and the old store is only read.
- `parallel_diff()` replays queries from a retrieval log against BOTH stores
  with identical read semantics and reports per-query result-set containment
  (new ⊇ old). Replays never write to the new store's retrieval log.

Cut over only when parallel_diff reports ok=True over a representative log.

CLI:
    python -m house_memory.migration migrate --old-chroma-dir D --new-data-dir D2 \
        --new-collection NAME [--old-collection NAME]
    python -m house_memory.migration diff --old-chroma-dir D --new-data-dir D2 \
        --new-collection NAME --log memory_retrieval.jsonl \
        [--old-collection NAME] [--limit 5] [--model voyage-3]
    (diff embeds queries with Voyage; set VOYAGE_API_KEY.)
"""

import argparse
import json
import os
import sys
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional, Union

import chromadb
from chromadb.config import Settings

from house_memory.retrieval_log import RetrievalRecord, read_records
from house_memory.store import MemoryStore, query_collection


@dataclass
class MigrationReport:
    old_chroma_dir: str
    old_collection: str
    total_old_records: int
    imported: int
    new_store_count: int

    @property
    def complete(self) -> bool:
        return self.imported == self.total_old_records


@dataclass
class QueryDiff:
    query: str
    subject_filter: Optional[str]
    old_returned_ids: list[str] = field(default_factory=list)
    new_returned_ids: list[str] = field(default_factory=list)
    missing_from_new: list[str] = field(default_factory=list)
    contained: bool = True


@dataclass
class DiffReport:
    total_queries: int
    contained_queries: int
    failed_queries: int
    ok: bool  # every replayed query satisfied new ⊇ old
    details: list[QueryDiff] = field(default_factory=list)


def _open_old_collection(old_chroma_dir: Union[str, Path], old_collection: Optional[str]):
    """Open an existing chroma dir for reading. Never creates a collection —
    the old store is never converted (or extended) in place."""
    client = chromadb.PersistentClient(
        path=str(old_chroma_dir),
        settings=Settings(anonymized_telemetry=False),
    )
    if old_collection:
        return client.get_collection(name=old_collection)
    collections = client.list_collections()
    if len(collections) != 1:
        names = [c.name for c in collections]
        raise ValueError(
            f"old store at {old_chroma_dir} has {len(collections)} collections "
            f"({names}); pass old_collection explicitly"
        )
    return client.get_collection(name=collections[0].name)


def migrate(
    old_chroma_dir: Union[str, Path],
    new_store: MemoryStore,
    old_collection: Optional[str] = None,
) -> MigrationReport:
    """Extract every record from the old store into new_store. Old store is
    read-only throughout; embeddings are copied verbatim."""
    col = _open_old_collection(old_chroma_dir, old_collection)
    got = col.get(include=["documents", "metadatas", "embeddings"])
    ids = got.get("ids", []) or []
    docs = got.get("documents", []) or []
    metas = got.get("metadatas", []) or []
    embs = got.get("embeddings", None)
    records = []
    for i, doc_id in enumerate(ids):
        emb = None
        if embs is not None and len(embs) > i:
            emb = [float(x) for x in embs[i]]
        records.append(
            {"id": doc_id, "content": docs[i], "embedding": emb, "metadata": dict(metas[i])}
        )
    imported = new_store.import_all(records)
    return MigrationReport(
        old_chroma_dir=str(old_chroma_dir),
        old_collection=col.name,
        total_old_records=len(ids),
        imported=imported,
        new_store_count=new_store.count(),
    )


def parallel_diff(
    old_store_dir: Union[str, Path],
    new_store: MemoryStore,
    queries_from_log: Union[str, Path, list[RetrievalRecord]],
    old_collection: Optional[str] = None,
    limit: int = 5,
) -> DiffReport:
    """Replay logged queries against both stores; report per-query containment
    (new returned ⊇ old returned).

    Both stores are read with identical semantics (subject filter as logged,
    superseded entries dropped post-query). Query embeddings come from
    new_store.embedder — for the real WP-H11 run both stores hold Voyage
    vectors, so one VoyageEmbedder serves both. Replays bypass recall() and
    are NEVER written to the new store's retrieval log.
    """
    if isinstance(queries_from_log, (str, Path)):
        records = read_records(queries_from_log)
    else:
        records = list(queries_from_log)
    old_col = _open_old_collection(old_store_dir, old_collection)

    details: list[QueryDiff] = []
    failed = 0
    for rec in records:
        n = max(limit, len(rec.raw_ids), 1)
        vec = new_store.embedder.embed_query(rec.query)
        _, _, old_out = query_collection(old_col, vec, rec.subject_filter or None, n)
        _, _, new_out = query_collection(new_store._collection, vec, rec.subject_filter or None, n)
        old_ids = [m.id for m in old_out]
        new_ids = [m.id for m in new_out]
        missing = [i for i in old_ids if i not in set(new_ids)]
        contained = not missing
        if not contained:
            failed += 1
        details.append(
            QueryDiff(
                query=rec.query,
                subject_filter=rec.subject_filter,
                old_returned_ids=old_ids,
                new_returned_ids=new_ids,
                missing_from_new=missing,
                contained=contained,
            )
        )
    return DiffReport(
        total_queries=len(details),
        contained_queries=len(details) - failed,
        failed_queries=failed,
        ok=failed == 0,
        details=details,
    )


# -- CLI ----------------------------------------------------------------------


def _build_new_store(args, embedder) -> MemoryStore:
    return MemoryStore(
        data_dir=args.new_data_dir,
        collection_name=args.new_collection,
        embedder=embedder,
        retrieval_log=None,  # migration tooling never writes retrieval logs
    )


class _NoEmbed:
    """migrate copies stored vectors; embedding must never be needed."""

    def embed_document(self, text: str) -> list[float]:
        raise RuntimeError("migrate should never re-embed; old record missing its embedding")

    def embed_query(self, text: str) -> list[float]:
        raise RuntimeError("migrate does not embed queries")


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="house-memory-migrate", description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_mig = sub.add_parser("migrate", help="copy old chroma store into a new MemoryStore")
    p_diff = sub.add_parser("diff", help="replay a retrieval log against old + new stores")
    for p in (p_mig, p_diff):
        p.add_argument("--old-chroma-dir", required=True)
        p.add_argument("--old-collection", default=None)
        p.add_argument("--new-data-dir", required=True)
        p.add_argument("--new-collection", required=True)
    p_diff.add_argument("--log", required=True, help="retrieval log (jsonl) to replay")
    p_diff.add_argument("--limit", type=int, default=5)
    p_diff.add_argument("--model", default="voyage-3")

    args = parser.parse_args(argv)

    if args.cmd == "migrate":
        report = migrate(args.old_chroma_dir, _build_new_store(args, _NoEmbed()), args.old_collection)
        out = asdict(report)
        out["complete"] = report.complete
        print(json.dumps(out, indent=2))
        return 0 if report.complete else 1

    # diff
    api_key = os.environ.get("VOYAGE_API_KEY")
    if not api_key:
        print("diff needs VOYAGE_API_KEY set (query embedding)", file=sys.stderr)
        return 2
    from house_memory.embeddings import VoyageEmbedder

    store = _build_new_store(args, VoyageEmbedder(api_key=api_key, model=args.model))
    report = parallel_diff(
        args.old_chroma_dir, store, args.log, args.old_collection, limit=args.limit
    )
    print(json.dumps(asdict(report), indent=2))
    return 0 if report.ok else 1


if __name__ == "__main__":
    sys.exit(main())
