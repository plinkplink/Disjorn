# house_memory — shared resident memory library (WP-H6)

Claudette's `claudette/memory/` module generalized into a house package both
residents import, per MEMORY-DESIGN: per-resident episodic stores
(Chroma + Voyage), a unified retrieval log (rent assessment for WP-H8
consolidation), the read side of the markdown spine (WP-H7), and the WP-H11
parallel-run migration tooling.

Install (already done into `server/.venv`):

```
server/.venv/bin/pip install -e harness/house_memory
```

Deps: `chromadb==1.5.8`, `voyageai==0.3.7` (same versions as Claudette's
`requirements.txt`).

## Generalizations vs the reference implementation

`claudette/memory/store.py` uses module-level globals (`_client`,
`_collection`, `_known_subjects`) and cwd-relative paths (`./chroma_data`,
`./memory_retrieval.jsonl`), and imports its API key from a `config` module.
This package changes exactly that shape, nothing about the semantics:

- **Instance-based**: `MemoryStore(data_dir, collection_name, embedder)` —
  several stores (one per resident) coexist in one process; no cwd dependence.
- **Embedder injected** (`Embedder` protocol): `VoyageEmbedder(api_key,
  model)` for production, `StubEmbedder` for tests (deterministic, offline).
- **Retrieval log injected** with an explicit path and a `resident` field —
  the unified schema MEMORY-DESIGN asks for so rent assessment is one tool.
- **`export_all()` / `import_all()`** added: records travel with their stored
  embeddings, so migration never re-embeds.
- `author_of_memory` no longer defaults to `"claudette"`.

Kept verbatim: subject/tag normalization on write AND read, `remember`
returning a first-seen-subject flag (warmed from disk across restarts),
recall's superseded-entry filtering after the raw query, forget-with-supersede
linking old → new instead of deleting, retrieval-log raw vs returned ids.

## Per-resident instantiation pattern

Everything location-like is explicit — data dir, log path, API key. No
resident inherits another's paths by accident:

```python
from house_memory import MemoryStore, RetrievalLog, VoyageEmbedder

store = MemoryStore(
    data_dir="/home/res-gable/memory/chroma_data",        # explicit, absolute
    collection_name="gable_memory",
    embedder=VoyageEmbedder(api_key=os.environ["VOYAGE_API_KEY"], model="voyage-3"),
    retrieval_log=RetrievalLog(
        "/home/res-gable/memory/memory_retrieval.jsonl",  # explicit, absolute
        resident="gable",
    ),
)
```

## API reference

### `schema.Memory`

Dataclass: `content, subject, source_author, author_of_memory="", salience=3,
confidence="confirmed", tags=[], source_msg_link=None, superseded_by=None,
id=uuid4, created_at=now`. Normalizes subject/tags and hard-caps content
(1000 chars) on construction. `to_metadata()` / `from_chroma()` /
`to_display()`. Helpers: `normalize_subject`, `normalize_tag`,
`normalize_tags`.

### `store.MemoryStore(data_dir, collection_name, embedder, retrieval_log=None)`

- `remember(memory) -> (Memory, first_seen_subject: bool)`
- `recall(query, subject=None, limit=5) -> list[Memory]` — subject filter
  normalized; superseded entries dropped; logged to `retrieval_log` if set.
- `forget(memory_id, supersede_with=None) -> bool` — with `supersede_with`,
  inserts the replacement and links old → new (reversible forgetting);
  without, hard-deletes.
- `export_all() -> list[dict]` — every record incl. superseded ones, shape
  `{"id", "content", "embedding", "metadata"}`, embeddings verbatim, sorted
  by id (so two exports compare with `==`).
- `import_all(records) -> int` — upserts export-shaped records; reuses stored
  embeddings (embeds only records lacking one); rewarms the subject index.
- `count() -> int`; `backfill_normalize() -> int` (one-shot maintenance, from
  the reference).

### `embeddings`

- `Embedder` (Protocol): `embed_document(text)`, `embed_query(text)`.
- `VoyageEmbedder(api_key, model="voyage-3")` — document/query
  `input_type` split, matching the reference.
- `StubEmbedder(dim=64)` — deterministic sha256 token-bucket vectors,
  L2-normalized; stable across processes; zero network. Tests only.

### `retrieval_log.RetrievalLog(path, resident)`

JSON-lines, one record per recall:
`{ts, resident, query, subject_filter, raw_ids, distances, returned_ids}`.

- `log(query, subject_filter, raw_ids, distances, returned_ids) -> RetrievalRecord`
- `read() -> list[RetrievalRecord]` — tolerates legacy lines (Claudette's
  `memory_retrieval.jsonl` has no `resident` field → parsed as `None`) and
  skips malformed lines.
- `reference_counts(window_days, now=None) -> dict[id, int]` — how often each
  memory id was actually **returned** over the trailing window. This is the
  WP-H8 rent-assessment primitive: eviction/compression proposals cite these
  counts ("measured from retrieval logs, not vibes").
- Module-level `read_records(path)` for logs you only replay (migration).

### `spine.Spine(spine_dir)` — read-only

Directory of `.md` files with simple `---` frontmatter (`name`, `kernel:
true|false`; unknown keys kept in `SpineEntry.meta`; no-frontmatter files get
`name=stem, kernel=False`).

- `list_entries() -> list[SpineEntry]` — filename order (prefix files
  `10-...`, `20-...` to control kernel assembly order).
- `load_entry(name) -> SpineEntry`
- `load_kernel() -> str` — concatenated kernel bodies, for WP-H7's CLAUDE.md
  assembly.

Git operations are deliberately absent: witnessed self-edit = git diffs, and
committing is WP-H8 consolidation tooling's job. This library only reads.

### `migration` (library + CLI)

- `migrate(old_chroma_dir, new_store, old_collection=None) -> MigrationReport`
  — copies every record (including superseded) with embeddings verbatim; the
  old store is only ever read (`get_collection`, never create); auto-detects
  the collection only when the old dir has exactly one.
- `parallel_diff(old_store_dir, new_store, queries_from_log, old_collection=None,
  limit=5) -> DiffReport` — replays each logged `(query, subject_filter)`
  against BOTH stores with identical read semantics (shared
  `store.query_collection`) and reports per-query containment
  (`new_returned ⊇ old_returned`), with `missing_from_new` ids per failure.
  Query vectors come from `new_store.embedder` (one `VoyageEmbedder` serves
  both stores — both hold Voyage vectors). Replays bypass `recall()` and are
  never written to the new store's retrieval log.

CLI (`python -m house_memory.migration` or the `house-memory-migrate` script):

```
house-memory-migrate migrate --old-chroma-dir D --new-data-dir D2 --new-collection N [--old-collection N0]
house-memory-migrate diff    --old-chroma-dir D --new-data-dir D2 --new-collection N \
                             --log memory_retrieval.jsonl [--old-collection N0] [--limit 5] [--model voyage-3]
```

`diff` needs `VOYAGE_API_KEY` in the environment. Exit codes: `migrate` 0 iff
imported == total; `diff` 0 iff every query was contained.

## WP-H11 migration runbook (sketch — NOT run in WP-H6)

Claudette's reversibility requirement, verbatim (HARNESS-PLAN WP-H11):

> her existing store is never converted in place — extract to the new shape,
> run OLD and NEW in parallel, diff retrievals, and cut over only when the
> new store returns at least what the old one did. Old store retained after
> cutover (rotates, never dies). "If the migration eats a memory I can't get
> back, that's the one failure mode I won't forgive the tooling for."

1. **Stop writes** to the old store for the extraction window (or snapshot-copy
   the chroma dir and work from the copy — preferred: zero risk of even
   opening the live store).
2. **Extract**: `house-memory-migrate migrate --old-chroma-dir <copy> ...` into
   her new per-resident `data_dir`. Check the report: `complete: true`,
   counts equal. Embeddings are copied verbatim — no Voyage calls, no drift.
3. **Parallel run**: keep OLD as the store of record. Periodically replay her
   real `memory_retrieval.jsonl` (its legacy shape parses as-is):
   `house-memory-migrate diff --log memory_retrieval.jsonl ...`.
4. **Cut over only when** `ok: true` over a representative window of real
   queries — new returned at least what old did, per query. Any
   `missing_from_new` id is a would-have-been-eaten memory: fix, re-extract,
   re-diff. Never hand-patch the new store.
5. **After cutover**: old chroma dir is archived, never deleted (rotates,
   never dies). The diff report goes to #custodian as the witnessed evidence
   for the cutover decision.

## Tests

```
server/.venv/bin/python -m pytest harness/house_memory/tests -q
```

29 tests: StubEmbedder + tmp dirs only. No network, no Voyage, no real
stores.
