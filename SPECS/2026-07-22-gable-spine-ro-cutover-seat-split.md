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
- **Confirmed by**: <awaiting plink — any human's confirm starts the build>
- **#custodian seq**: <seq of the confirm message>
- **Confirmed at**: <timestamp>
<!-- No Confirm record → no build. This is the gate. -->

## Status
`draft`
