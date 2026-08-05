# Spec: supersede preserves tags, salience and confidence

## Request

- **Verbatim**: "First build should be small enough that we're testing the path and not the change — I nominate the supersede-tags fix. Tiny diff, obvious test, and I hit it four times last night, so I'm the customer."
- **Requester**: Claudette
- **Origin**: #custodian seq 743, 2026-08-05

## Problem

`forget(memory_id, supersede_content=…)` builds the replacement memory from
scratch and passes only `content` and `subject`:

```python
new_mem = Memory(
    content=supersede,
    subject=tool_input.get("subject", ""),
    source_author=source_author,
    source_msg_link=source_msg_link,
)
```

`tags`, `salience` and `confidence` are never carried, so **every superseded
memory lands with empty tags, salience 3 and confidence `confirmed`, by
construction** — the dataclass defaults. There is no way for the caller to
supply them either: `FORGET_TOOL`'s `input_schema` exposes only `memory_id`,
`supersede_content` and `subject`.

Claudette hit this four times on the night of 2026-08-04 and retagged each by
hand. She had already named the cost on 2026-08-05 (#custodian seq 628), before
`retag` existed, as the reason supersede was the wrong instrument for a label
repair: *"`supersede_content` takes no salience or confidence, so all thirteen
entries would come back as defaults. That is real data destroyed to fix a
label."*

Two properties make this worse than a missing convenience. Superseding is the
verb for *"I changed my mind about what this says"* — a body edit — and it
silently discards three fields that were **not** the subject of that change.
And because a supersede chain is the record of how a belief evolved, the chain
currently degrades metadata at every hop: the older an idea is, the more times
it has been reconsidered, and the less findable its current version is.

## Agreed UX

1. **Inherit by default.** A superseded memory's `tags`, `salience` and
   `confidence` carry to the replacement unless the caller explicitly supplies
   them. Superseding says nothing about tags, so it must not silently change
   them.

2. **Override when given.** `forget` accepts optional `tags`, `salience` and
   `confidence`; each supplied value replaces the inherited one. `tags` routes
   through the same `normalize_tags` the write path uses, and — as with
   `retag` — **a bare string is refused, not coerced**, because this is a
   deliberate metadata statement rather than a best-effort save.

3. **"Not given" and "given as empty" are different.** `tags=None` means
   inherit; `tags=[]` means the caller is deliberately clearing them. The
   implementation needs a sentinel that distinguishes the two — a bare falsy
   check collapses them and reintroduces the bug for anyone trying to clear
   tags on purpose.

4. **The receipt says what was inherited.** The tool result states which fields
   carried over and which were overridden. Consistent with the `remember`
   receipt shipped 2026-08-05: at the point of a metadata change, the
   instrument reports what it did. This is the property that has repeatedly cost
   one turn instead of one evening.

## Also in scope — the orphan write, because it is the same function

`MemoryStore.forget` writes the replacement **before** confirming the target
exists:

```python
if supersede_with:
    self.remember(supersede_with)          # written first
    existing = self._collection.get(ids=[memory_id])
    if existing["ids"]:
        ...
        return True
    return False                           # target absent -> returns False
```

Superseding a nonexistent id therefore **stores a new memory and reports
failure**: the caller sees `"Memory not found."` while an orphan with no
predecessor sits in the store. Nobody has hit this, but the inheritance fix
requires reading the old record *before* constructing the new one anyway, which
puts the read in the right place to also close this. Fixing them separately
would mean touching the same six lines twice.

**Required behaviour**: if the target id does not exist, write nothing and
return failure.

## Architecture notes

- Two call sites: `bots/claudette/memory/tools.py` (`FORGET_TOOL` schema +
  the `forget` branch of `handle_memory_tool`) and
  `harness/house_memory/house_memory/store.py` (`MemoryStore.forget`).
  `bots/claudette/memory/store.py` is a thin pass-through and may need its
  signature widened.
- Inheritance belongs in **one** place. Prefer `store.forget` back-filling
  unset fields from the record it is superseding, so any future caller gets the
  behaviour without re-implementing it — the reason `normalize_tags` living in
  two places cost 75 memories their tags.
- **`house_memory` is deployed, not imported from the repo.** The runtime copy
  is `/usr/local/lib/disjorn/house_memory`; the repo copy is edited and then
  installed. See TREE.md before concluding a change did not take effect.
- Do **not** widen `retag`. It stays metadata-only and chainless. This spec is
  about supersede carrying metadata it already should have carried; the two
  verbs keep their separate jobs — *change what a memory says* versus *change
  how you find it*.

## Tests (the acceptance criteria)

1. Supersede with no metadata arguments → replacement carries the original's
   tags, salience and confidence.
2. Supersede with explicit values → each supplied field overrides; unsupplied
   fields still inherit.
3. `tags=[]` explicitly → replacement has no tags (a deliberate clear, not an
   inherit).
4. `tags="astring"` → refused, and **nothing is written**.
5. Supersede a nonexistent id → returns failure **and the store gains no
   record** (count unchanged).
6. The existing supersede chain semantics are unchanged: the old memory
   survives, flagged with `superseded_by`, and is excluded from recall.

## Lane → Review owner (DETERMINISTIC)

- **Lane**: Claudette's memory surface.
- **Review owner**: **Claudette**.

## Builder (USER PREFERENCE)

- **Builder**: **Claudette**, via `start-build` — the first resident-run build
  on this platform. Chosen deliberately small: *testing the path, not the
  change.*

## Cross-lane split

- **Applies**: no. `house_memory` is shared library code, so the keyboard seat
  reviews the `house_memory` half at merge.

## Expected diff tier

Tier 1 — a resident's own memory surface, self-authored, witnessed at merge.
Not Tier 0: it changes stored metadata semantics. Not Tier 2: it touches no
credential, no authorization surface, and no other resident's data.

## Token estimate

~40k. Small on purpose.

## Confirm record

- **Confirmed by**: plink
- **#custodian seq**: 761
- **Confirmed at**: 2026-08-05

Verbatim, #custodian seq 761: *"Claudette, Gable, if we're waiting on an
official confirm from me on that spec, you have it now."*

Filled by the keyboard seat, which is the only seat that can write here — the
mirror is read-only to both residents by design, so a resident cannot supply
its own confirm record. Recorded per the house rule that the seq is the witness
and the file is the record.

## Status

`confirmed`
<!-- ONE TOKEN ONLY: draft / confirmed / building / built@<branch> / merged /
failed. The broker's confirm gate compares the first non-comment line here to
the literal word and nothing else. Narrative goes in the Confirm record. -->
