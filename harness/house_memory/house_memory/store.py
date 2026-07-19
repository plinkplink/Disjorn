"""Instance-based memory store, generalized from claudette/memory/store.py.

Generalizations vs the reference:
- INSTANCE-based: `MemoryStore(data_dir, collection_name, embedder)` replaces
  module-level chroma client/collection globals and staticmethods. Several
  stores (one per resident) coexist in one process.
- Explicit paths: `data_dir` is required and absolute-friendly — no
  cwd-relative "./chroma_data" / "./memory_retrieval.jsonl".
- Embedder injected (Embedder protocol) instead of a hardwired module import,
  so tests run on StubEmbedder with zero network.
- Retrieval logging goes through an injected RetrievalLog (unified schema,
  explicit path, resident-tagged) instead of a hardcoded relative file.
- export_all()/import_all() added for the WP-H11 parallel-run migration:
  embeddings are exported and re-imported verbatim, so migration never
  re-embeds (no API calls, bit-identical vectors).

Semantics kept from the reference: remember returns (memory,
first_seen_subject); recall normalizes the subject filter, drops superseded
memories after the raw query, and logs raw vs returned ids; forget with
`supersede_with` inserts the replacement and links old -> new instead of
deleting.
"""

import json
import logging
from pathlib import Path
from typing import Optional, Union

import chromadb
from chromadb.config import Settings

from house_memory.embeddings import Embedder
from house_memory.retrieval_log import RetrievalLog
from house_memory.schema import Memory, normalize_subject

logger = logging.getLogger("house_memory")


class MemoryStore:
    def __init__(
        self,
        data_dir: Union[str, Path],
        collection_name: str,
        embedder: Embedder,
        retrieval_log: Optional[RetrievalLog] = None,
    ):
        self.data_dir = Path(data_dir)
        self.collection_name = collection_name
        self.embedder = embedder
        self.retrieval_log = retrieval_log
        self._client = chromadb.PersistentClient(
            path=str(self.data_dir),
            settings=Settings(anonymized_telemetry=False),
        )
        self._collection = self._client.get_or_create_collection(name=collection_name)
        self._known_subjects: set[str] = set()
        self._warmup_subject_index()

    # -- core semantics (matching the reference implementation) --------------

    def remember(self, memory: Memory) -> tuple[Memory, bool]:
        """Returns (memory, first_seen_subject)."""
        vec = self.embedder.embed_document(memory.content)
        self._collection.add(
            ids=[memory.id],
            documents=[memory.content],
            embeddings=[vec],
            metadatas=[memory.to_metadata()],
        )
        first_seen = memory.subject not in self._known_subjects
        self._known_subjects.add(memory.subject)
        logger.info(f"[Memory] remembered {memory.id}: {memory.content[:60]}")
        return memory, first_seen

    def recall(self, query: str, subject: Optional[str] = None, limit: int = 5) -> list[Memory]:
        vec = self.embedder.embed_query(query)
        norm_subject = normalize_subject(subject) if subject else None
        raw_ids, distances, out = query_collection(self._collection, vec, norm_subject, limit)
        if self.retrieval_log is not None:
            try:
                self.retrieval_log.log(query, norm_subject, raw_ids, distances, [m.id for m in out])
            except Exception as e:
                logger.warning(f"[Memory] retrieval log write failed: {e}")
        logger.info(f"[Memory] recalled {len(out)} for query: {query[:60]}")
        return out

    def forget(self, memory_id: str, supersede_with: Optional[Memory] = None) -> bool:
        """If supersede_with provided, insert new memory and link old -> new.
        Otherwise hard-delete."""
        if supersede_with:
            self.remember(supersede_with)  # discard first_seen flag
            existing = self._collection.get(ids=[memory_id])
            if existing["ids"]:
                meta = existing["metadatas"][0]
                meta["superseded_by"] = supersede_with.id
                self._collection.update(ids=[memory_id], metadatas=[meta])
                logger.info(f"[Memory] superseded {memory_id} -> {supersede_with.id}")
                return True
            return False
        self._collection.delete(ids=[memory_id])
        logger.info(f"[Memory] forgot {memory_id}")
        return True

    # -- migration surface (WP-H11) ------------------------------------------

    def export_all(self) -> list[dict]:
        """Every record — including superseded ones — with stored embeddings.

        Record shape: {"id", "content", "embedding", "metadata"}. Embeddings
        travel verbatim so import_all never re-embeds.
        """
        got = self._collection.get(include=["documents", "metadatas", "embeddings"])
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
                {
                    "id": doc_id,
                    "content": docs[i],
                    "embedding": emb,
                    "metadata": dict(metas[i]),
                }
            )
        records.sort(key=lambda r: r["id"])
        return records

    def import_all(self, records: list[dict]) -> int:
        """Load export_all()-shaped records. Stored embeddings are reused;
        records without one are embedded with this store's embedder.
        Existing ids are overwritten (upsert). Returns count imported."""
        if not records:
            return 0
        BATCH = 512
        for start in range(0, len(records), BATCH):
            batch = records[start : start + BATCH]
            self._collection.upsert(
                ids=[r["id"] for r in batch],
                documents=[r["content"] for r in batch],
                embeddings=[
                    r["embedding"]
                    if r.get("embedding") is not None
                    else self.embedder.embed_document(r["content"])
                    for r in batch
                ],
                metadatas=[r["metadata"] for r in batch],
            )
        self._known_subjects.clear()
        self._warmup_subject_index()
        return len(records)

    def count(self) -> int:
        return self._collection.count()

    # -- maintenance (from the reference) ------------------------------------

    def backfill_normalize(self) -> int:
        """One-shot: normalize subject + tags on every existing memory.
        Returns count updated."""
        from house_memory.schema import normalize_tags

        existing = self._collection.get()
        ids = existing.get("ids", []) or []
        metas = existing.get("metadatas", []) or []
        updates_ids: list[str] = []
        updates_metas: list[dict] = []
        for i, doc_id in enumerate(ids):
            meta = dict(metas[i])
            old_subject = meta.get("subject", "")
            new_subject = normalize_subject(old_subject)
            try:
                old_tags = json.loads(meta.get("tags_json", "[]"))
            except Exception:
                old_tags = []
            new_tags = normalize_tags(old_tags)
            if new_subject != old_subject or new_tags != old_tags:
                meta["subject"] = new_subject
                meta["tags_json"] = json.dumps(new_tags)
                updates_ids.append(doc_id)
                updates_metas.append(meta)
        if updates_ids:
            self._collection.update(ids=updates_ids, metadatas=updates_metas)
        logger.info(f"[Memory] backfill_normalize updated {len(updates_ids)} memories")
        self._known_subjects.clear()
        self._warmup_subject_index()
        return len(updates_ids)

    def _warmup_subject_index(self) -> None:
        """Load known subjects from existing data so first-seen flags are
        accurate across restarts."""
        try:
            existing = self._collection.get()
            for meta in existing.get("metadatas", []) or []:
                subj = meta.get("subject")
                if subj:
                    self._known_subjects.add(subj)
            logger.info(f"[Memory] warmed {len(self._known_subjects)} known subjects")
        except Exception as e:
            logger.warning(f"[Memory] subject warmup failed: {e}")


def query_collection(
    collection, query_embedding: list[float], norm_subject: Optional[str], limit: int
) -> tuple[list[str], list, list[Memory]]:
    """Raw query + post-filtering shared by MemoryStore.recall and the
    migration parallel-diff replay (so old and new stores are read with
    identical semantics). Returns (raw_ids, distances, returned_memories);
    superseded memories appear in raw_ids but never in the returned list."""
    where = {"subject": norm_subject} if norm_subject else None
    results = collection.query(
        query_embeddings=[query_embedding],
        n_results=limit,
        where=where,
    )
    ids = results.get("ids", [[]])[0]
    docs = results.get("documents", [[]])[0]
    metas = results.get("metadatas", [[]])[0]
    distances = (
        results.get("distances", [[]])[0]
        if results.get("distances") is not None
        else [None] * len(ids)
    )
    out: list[Memory] = []
    for i, doc_id in enumerate(ids):
        mem = Memory.from_chroma(doc_id, docs[i], metas[i])
        if mem.superseded_by:
            continue
        out.append(mem)
    return list(ids), list(distances), out
