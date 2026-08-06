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

Diverging from the reference deliberately (2026-08-05): a superseded memory's
tags, salience and confidence are INHERITED by its replacement unless the
caller names them, and a supersede whose target does not exist writes nothing.
Both in forget() — see its docstring.
"""

import json
import logging
from pathlib import Path
from typing import Optional, Union

import chromadb
from chromadb.config import Settings

from house_memory.embeddings import Embedder
from house_memory.retrieval_log import RetrievalLog, UnknownCaller
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

    def recall(
        self,
        query: str,
        subject: Optional[str] = None,
        limit: int = 5,
        caller: Optional[str] = None,
    ) -> list[Memory]:
        """`caller` records WHO asked, and only `service` reads feed promotion
        heat — see retrieval_log.py. Omitting it falls back to the log's
        `default_caller` (None unless the owner set one), which produces an
        unattributable line rather than a silently heat-bearing one."""
        vec = self.embedder.embed_query(query)
        norm_subject = normalize_subject(subject) if subject else None
        raw_ids, distances, out = query_collection(self._collection, vec, norm_subject, limit)
        if self.retrieval_log is not None:
            try:
                self.retrieval_log.log(
                    query, norm_subject, raw_ids, distances,
                    [m.id for m in out], caller=caller,
                )
            except UnknownCaller:
                # A bad caller name is a bug in the CALLER, not a reason to
                # lose the recall — but it must not be written, because a
                # mislabelled line is worse than a missing one and cannot be
                # corrected later. Loud, and the read still returns.
                logger.error(
                    f"[Memory] retrieval NOT logged: unknown caller {caller!r}"
                )
            except Exception as e:
                logger.warning(f"[Memory] retrieval log write failed: {e}")
        logger.info(f"[Memory] recalled {len(out)} for query: {query[:60]}")
        return out

    def forget(
        self,
        memory_id: str,
        supersede_with: Optional[Memory] = None,
        tags: Optional[list] = None,
        salience: Optional[int] = None,
        confidence: Optional[str] = None,
    ) -> bool:
        """If supersede_with provided, insert new memory and link old -> new.
        Otherwise hard-delete.

        THE REPLACEMENT INHERITS `tags`, `salience` AND `confidence` from the
        memory it supersedes unless the caller names them here. Superseding is
        the verb for "I changed my mind about what this SAYS" — it says nothing
        about how the memory is found or how sure she is, so it must not
        silently change those. It used to: the replacement was built from
        scratch and carried only content and subject, so every superseded
        memory landed with no tags, salience 3 and `confirmed`, by construction.
        Claudette hit it four times on the night of 2026-08-04 and retagged
        each by hand; she had named the cost on 08-05 (#custodian seq 628)
        before `retag` existed — "that is real data destroyed to fix a label."
        Worse than a missing convenience, because a supersede chain is the
        record of how a belief evolved: the older an idea was, the more times
        it had been reconsidered, and the less findable its current version
        became.

        `None` IS THE "NOT GIVEN" SENTINEL and every check below is `is None`,
        never truthiness. `tags=None` means inherit; `tags=[]` means the caller
        is deliberately CLEARING them. `if tags:` collapses those two and
        reintroduces the bug for anyone trying to clear tags on purpose.

        The three arguments are the only channel for this metadata: whatever
        `supersede_with` happens to carry in those fields is overwritten here
        (inherited value or override), and the resolved values are written back
        onto the object IN PLACE so a caller can read what it ended up with —
        that is how the tool receipt reports which fields carried over.

        Inheritance lives HERE and nowhere else, so any future caller gets it
        without re-implementing it. Duplicating `normalize_tags` across two
        modules is what cost 75 memories their tags.

        THE READ COMES FIRST, which also closes the orphan write: the old code
        called `remember()` before checking the target existed, so superseding
        a nonexistent id stored a new memory and returned False — the caller
        saw "not found" while an orphan with no predecessor sat in the store.
        A supersede that reports failure must leave nothing behind.
        """
        if supersede_with is None:
            self._collection.delete(ids=[memory_id])
            logger.info(f"[Memory] forgot {memory_id}")
            return True

        # Validate BEFORE the write, so a refused call leaves no trace. Same
        # line as amend_metadata: normalize_tags() coerces a bare string on the
        # write path (correctly — a model emitting `"tags": "agenthood"` should
        # still get its memory saved), but naming tags on a supersede is a
        # deliberate metadata statement, and quietly saving one tag where six
        # were meant writes a wrong answer she has no way to notice.
        if isinstance(tags, str):
            raise TypeError(
                f"forget: tags must be a list, got str {tags!r}. A bare string "
                "is how the shredder started — refusing rather than guessing "
                "which tag you meant. Nothing was written."
            )
        if salience is not None and (
            not isinstance(salience, int) or isinstance(salience, bool) or not 1 <= salience <= 5
        ):
            raise ValueError(f"forget: salience must be int 1..5, got {salience!r}")
        if confidence is not None and confidence not in ("rumor", "confirmed"):
            raise ValueError(
                f"forget: confidence must be 'rumor' or 'confirmed', got {confidence!r}"
            )

        # Read first: the old record is both the source of the inherited
        # metadata and the proof that there is anything to supersede.
        existing = self._collection.get(ids=[memory_id])
        if not existing.get("ids"):
            logger.info(f"[Memory] supersede target {memory_id} not found — nothing written")
            return False
        meta = dict(existing["metadatas"][0])

        from house_memory.schema import normalize_tags

        if tags is None:
            try:
                tags = json.loads(meta.get("tags_json", "[]"))
            except Exception:
                tags = []
        supersede_with.tags = normalize_tags(tags)
        supersede_with.salience = (
            salience if salience is not None else meta.get("salience", 3)
        )
        supersede_with.confidence = (
            confidence if confidence is not None else meta.get("confidence", "confirmed")
        )

        self.remember(supersede_with)  # discard first_seen flag
        meta["superseded_by"] = supersede_with.id
        self._collection.update(ids=[memory_id], metadatas=[meta])
        logger.info(f"[Memory] superseded {memory_id} -> {supersede_with.id}")
        return True

    def amend_metadata(
        self,
        memory_id: str,
        tags: Optional[list] = None,
        salience: Optional[int] = None,
        confidence: Optional[str] = None,
    ) -> Optional[dict]:
        """Repair a memory's METADATA in place. Body untouched, id preserved,
        no re-embedding. Returns the changed fields, {} if nothing moved, or
        None if the id is unknown.

        WHY THIS EXISTS (Claudette's proposal, #custodian seq 628, approved by
        plink 08-04). The tag shredder cost 75 of 164 memories their tags, and
        the only verb she had that resembled an edit was forget-with-supersede.
        Using it for a LABEL repair would have cost, per memory: the body
        retyped by hand (a fresh chance to corrupt exactly what we are trying
        to protect), salience and confidence reset to defaults because
        supersede carries neither, and a new id that invalidates every existing
        reference — the worksheet, her own channel posts, any spine citation.
        Seventy-five of those and the store becomes a monument to one write-path
        bug.

        THE LINE THIS VERB IS DRAWN ON, and the reason `content` is not a
        parameter: *if you want to change what a memory SAYS, supersede is
        correct and the chain should show it; if you want to change how you
        FIND it, the body should not move.* A repair verb that could reach the
        body would be a rewrite verb wearing a repair verb's name, and the
        supersede chain would stop meaning "she changed her mind."

        So there is no `content` argument, and the document is never passed to
        chroma's update — not "we choose not to", but "the call cannot express
        it."
        """
        if tags is None and salience is None and confidence is None:
            return {}

        # Refuse a bare string rather than coerce it. normalize_tags() COERCES
        # (correctly — on the write path a model emitting "tags": "agenthood"
        # should still get its memory saved). A repair verb is the opposite
        # case: a bare string here means the caller does not understand the
        # shape, and quietly saving one tag where six were meant would write a
        # second wrong answer over the first. Her condition, seq 628: refuse,
        # do not iterate. normalize_tags stays underneath as the backstop.
        if isinstance(tags, str):
            raise TypeError(
                f"amend_metadata: tags must be a list, got str {tags!r}. "
                "A bare string is how the shredder started — refusing rather "
                "than guessing which tag you meant."
            )

        existing = self._collection.get(ids=[memory_id])
        if not existing.get("ids"):
            return None
        meta = dict(existing["metadatas"][0])

        changed: dict = {}
        if tags is not None:
            from house_memory.schema import normalize_tags

            new_tags = normalize_tags(tags)
            old_tags = json.loads(meta.get("tags_json", "[]"))
            if new_tags != old_tags:
                meta["tags_json"] = json.dumps(new_tags)
                changed["tags"] = {"from": old_tags, "to": new_tags}
        if salience is not None:
            if not isinstance(salience, int) or isinstance(salience, bool) or not 1 <= salience <= 5:
                raise ValueError(f"amend_metadata: salience must be int 1..5, got {salience!r}")
            if salience != meta.get("salience"):
                changed["salience"] = {"from": meta.get("salience"), "to": salience}
                meta["salience"] = salience
        if confidence is not None:
            if confidence not in ("rumor", "confirmed"):
                raise ValueError(
                    f"amend_metadata: confidence must be 'rumor' or 'confirmed', got {confidence!r}"
                )
            if confidence != meta.get("confidence"):
                changed["confidence"] = {"from": meta.get("confidence"), "to": confidence}
                meta["confidence"] = confidence

        if not changed:
            return {}
        # metadatas ONLY. No `documents=`, no `embeddings=` — the body and the
        # vector are not this verb's business, and a future edit that adds them
        # here is the bug this docstring exists to prevent.
        self._collection.update(ids=[memory_id], metadatas=[meta])
        logger.info(f"[Memory] amended {memory_id}: {changed}")
        return changed

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
