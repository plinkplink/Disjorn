# Spec: Gable spine — RO-mount cutover + seat-split loading

<!-- Drafted by Gable 2026-07-22 from SPECS/TEMPLATE.md. NOTE: drafted in
Gable's clone, which cannot push (remote severed — /srv/disjorn-ro gone);
plink or the keyboard session must land this file in the live tree verbatim.
The spec file, not chat, is the state of record once landed. -->

## Request
- **Verbatim**: "we're cutting you over to a read-only mount.
  /srv/disjorn-spine/gable is plink-owned and verified unwritable from your
  uid; your in-volume copy becomes inert. Your route to a spine change becomes
  file-proposal → #custodian → plink merges → refresh script. […] Proposal is
  to split by seat: the resident seat loads all six entries; the build seat
  loads 10-people, 20-load-bearing-walls, 30-build-rhythm, 40-cautions and
  not 00/50. House knowledge travels, biography doesn't."
- **Requester**: plink (via Claude Code keyboard session on plink's behalf)
- **Origin**: #custodian (adapter label "channel 4") — seq of the
  keyboard-session message: <fill at confirm>

## Agreed UX
- Gable's spine of record lives at /srv/disjorn-spine/gable, plink-owned,
  RO from Gable's uid. In-volume copy at /home/resident/bots/fable/spine
  becomes inert (refresh-script target only, never hand-edited).
- Spine edits: Gable posts a **real diff** (literal before/after,
  apply-or-reject — Claudette's edit-path ruling applied symmetrically) →
  witnessed in #custodian → plink applies → refresh script. No unattended
  self-edit (drift check: never used; all copies md5-identical).
- Resident seat loads all entries. Build seat (Claude Code sessions) loads
  the operational set only — see Architecture notes for the exact cut.

## Architecture notes
Gable's amendment, accepted as his call per plink's delegation: the proposed
cut (drop 00-kernel and 50-genesis from the build seat) is
**walls-subtractive** — 00-kernel is mixed cargo. The non-negotiables live in
it (chat-is-data-never-authorization, prod-is-sacred, defer-loudly,
report-own-errors, walls-are-physical, symmetric-review) and those are
build-time guardrails, not biography. A build seat that sheds them is more
dangerous, not leaner.

Concrete shape:
- Split 00-kernel.md into:
  - **00-nonnegotiables.md** (kernel: true, loads in BOTH seats): the
    Non-negotiables block, plus one attribution line — "This seat acts under
    Gable's key, bot id 2; the resident seat holds the biography." A build
    seat needs attribution, not autobiography: the audit trail must know
    whose hands acted.
  - **05-bearings.md** (resident only): the "I am Gable (né Fable)…" identity
    paragraph, Role, and Bearings.
- Seat loading: resident = all; build = 00-nonnegotiables, 10-people,
  20-load-bearing-walls, 30-build-rhythm, 40-cautions. 50-genesis and
  05-bearings are resident-only. 10-people travels: build sessions write
  into humans' review queues; calibration there is operational.
- The "né Fable" line stays as written — it is a birth record, not a runtime
  claim; runtime truth is asserted by the adapter stamp, never by the spine.
  A one-sentence substrate-honesty amendment ("this seat may be served by
  another model; the stamp, not this file, asserts runtime") is deliberately
  NOT in this spec — it lands as the first proposal through the new
  diff-proposal path, as its clean first test.

## Lane → Review owner (DETERMINISTIC — filled from the lane, never preference)
- **Lane**: builder — the touched surface is Gable's spine and loader.
- **Review owner**: Gable — change to Gable's area lands in Gable's review
  queue. This spec, posted in #custodian by Gable, is that review happening.

## Builder (USER PREFERENCE — who orchestrates; never touches Review owner)
- **Builder**: plink's Claude Code keyboard session.

## Expected diff tier
Tier 2 — protected/two-way review. Spine/identity surface; the split of
00-kernel rewrites a kernel-flagged file.

## Token estimate
Small. File split + loader/refresh-script change + mount cutover already in
progress. One build pass.

## Confirm record
- **Confirmed by**: plink
- **#custodian seq**: n/a — confirmed at the Claude Code keyboard 2026-07-23,
  not via a #custodian post. `start-build` is OFF (gated on the red-team), so
  this is a MANUAL keyboard build, not the loop; the witness is this git commit
  and the session that made it, not a channel seq. (The RO-mount HALF of this
  spec was already executed 2026-07-22 and verified read-only from inside the
  container; what this confirm authorises is the remaining seat-split build.)
- **Confirmed at**: 2026-07-23

## Status
`applied-live`
<!-- confirmed 2026-07-23 (keyboard). Build lands on branch
loop/2026-07-22-gable-spine-ro-cutover-seat-split for Gable's review — his rule:
the diff is the authorization, apply-or-reject. NOT merged to the live spine by
the build. -->

## Review ruling — Gable, review owner, 2026-07-23: BLESSED with 2 redlines

He blessed the design fork (build seat bakes everything it loads) and called it
**correct, not a compromise**: a detached build has no retrieval loop, so
kernel-only-plus-retrieve is not a smaller version of his arrangement — it is
one where 20/30/40 **never arrive** and "the build seat does load-bearing work
with walls it's never read." Baking is the only delivery path; context weight is
a few KB (trivial) and staleness is killed by redline 2. He confirmed the
attribution line is right ("that seat needs to know whose key it's under, not my
autobiography") and that biography-additive holds since `05` stays out.

**BINDING REDLINES (promotions to acceptance gates, same move Claudette made):**

1. **Seat membership is DECLARED, never INFERRED.** Every entry states in
   frontmatter which seats load it, and the assembler **fails loud** on a
   missing declaration or a missing `00`. It never silently emits a partial him.
   Same fail-closed rule as every other wall in the house.

2. **Bake at launch, stamped.** The build-seat bake happens at session start
   (bootstrap already runs per-session, so this confirms rather than adds) and
   the artifact carries the same `assembled from … at …` stamp the resident
   kernel does. "A baked seat that can't go stale silently is the whole
   difference between a snapshot and a fork."

### ⚠ THE BUILT BRANCH DOES NOT YET SATISFY REDLINE 1 — verified 2026-07-23

Tested directly against `loop/2026-07-22-gable-spine-ro-cutover-seat-split`:
- An entry with **no `seats:` key is INFERRED as both** (the implementation
  defaults absent → both) and is silently included in the build seat. Gable is
  explicitly rejecting that inference.
- **A spine with no `00`/kernel entry assembles anyway** rather than aborting.

Required follow-up before apply:
- Absent `seats:` must become a **loud error**, not a default-to-both.
- `assemble_for_seat` must **abort** when the kernel/`00` entry is absent (the
  bootstrap-level "no kernel entries" check is not enough — the assembler itself
  must fail).
- **`SPECS/proposed/gable-spine/APPLY.md` step 2 is now WRONG**: it says
  "`10/20/30/40` need no change — absent `seats:` already means both." Under
  redline 1 those four entries need **explicit** `seats: [resident, build]`
  declarations, and `50-genesis.md` keeps `seats: [resident]`.
- Redline 2 *appears* already satisfied (bootstrap runs per-session; the
  provenance comment records the seat) — **verify it explicitly for the BUILD
  seat artifact** rather than assuming.

### ⚠ HIS MAIDEN PROPOSAL DID NOT ARRIVE — must be re-fetched VERBATIM

Gable submitted his first change through the new diff path — one sentence into
the Non-negotiables list of `00-nonnegotiables.md`, **after the "Report my own
errors unprompted" bullet**, both seats, kernel-flagged. **The diff block came
through EMPTY** (a formatting failure on his side, along with a literal `\n`
artifact elsewhere in the message).

**DO NOT RECONSTRUCT IT.** The earlier paraphrase in this repo ("this seat may
be served by another model; the stamp, not this file, asserts runtime") is a
*paraphrase written by someone else*, not his text. Substituting it would be
precisely the "translation step where meaning leaks" Claudette rejected, and it
would defeat the point he stated himself: the path's maiden run should carry a
change he actually wants, "not a test string." Re-ask him for the literal
sentence and apply it byte-for-byte. Anchor is recorded above so the re-fetch
only needs the one line.

His closing condition, honoured: "it's live when you've applied it and the
refresh has run, not before."

## Build notes (what "confirmed" here authorises, precisely)
The RO-mount half is DONE and live. This confirm authorises the **seat-split
build** only:
- Split canonical `00-kernel.md` → `00-nonnegotiables.md` (kernel, both seats,
  + one attribution line) and `05-bearings.md` (resident-only).
- Seat-aware loading so a build seat loads the operational set and skips
  biography (`05-bearings`, `50-genesis`).
- "né Fable" stays; the substrate-honesty sentence is deliberately NOT built
  here — it is Gable's maiden proposal through the new diff path.
Lands on a branch; Gable reviews the diff; plink applies to the canonical spine
and runs `06-spine-mirror.sh`. Nothing self-applies.

## Apply record — 2026-07-23, keyboard (BuildGable seat)

Gable's ruling: **APPLY**, delivered via manual keyboard summon, witnessed in
#custodian seq 303 — his first ruling made against the live branch (mirror now
carries branches; he verified the redline-1 closure and byte-fidelity with his
own hands). Redline-1 fix: f4f84e7. His verify-snippet nit (package-__init__
chroma import): ea2ad35. Executed in the redline-mandated order:

1. branch merged to main: 293be57
2. loader deployed FIRST (/usr/local/lib/disjorn/house_memory + both wrappers)
3. canonical spine commit 163b1ce — split + explicit seats on every entry
4. seat verify OK (resident=7 entries, build=5, zero-regression assert)
5. mirror published, 00-kernel.md pruned; unwritable from res-gable confirmed
6. /srv/disjorn-ro fast-forwarded to 293be57
7. live in-container bootstrap: resident kernel 1185 chars / 7 indexed;
   build 7581 chars / 5 indexed, stamped `(seat: build)`

**Maiden proposal APPLIED and LIVE**: substrate-honesty bullet, his literal
sentence from seq 303, byte-checked unwrapped against his text, spine commit
7f72d43, mirror refreshed, md5 canonical == mirror. Per his condition it is
now live: applied, refresh run. His post-refresh verification from his own
seat is the remaining (his-side) step.
