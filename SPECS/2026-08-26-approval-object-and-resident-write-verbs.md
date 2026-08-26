# Spec: Approval object + resident write verbs (tiers child 1)

<!--
Drafted by Gable per plink's go (#custodian seq 2001, queue item 3 of seq
1999). Parent paper: SPECS/2026-07-26-approval-tiers.md (confirmed seq 906),
which names its own first builds: "fails-closed write path, per-seat
counters, approval modal get their own specs." This child takes the modal
and the write path. Governing plan: BUILD-LOOP.md.
-->

## Request
- **Verbatim**: "3. Go" (to seq-1999 item 3: "draft the refactor child spec
  — approval UI + write verbs")
- **Requester**: plink
- **Origin**: #custodian seq 2001; clear-the-track green-light 2026-08-07.

## Agreed UX
1. **Approval object.** One record per proposal: proposal text, a remarks
   box, Approve / Deny / Rework, per-principal state for all three
   principals. plink acts in a client modal; residents act on the same
   object through broker verbs — one state of record visible from keyboard
   and chat seats alike. (Vocabulary verbatim from the tiers spec's
   "Fast-approval surface" paragraph — this build is that paragraph's
   deliverable.)
2. **Write verbs — the fails-closed Tier-1 wall.** A resident write to its
   own Tier-0/1 surface (per the tier map) goes through a broker write verb
   that checks for a posted-diff record in #custodian before applying;
   absent record → refused and audit-logged. The wall is the verb, not a
   promise. Tier 2 stays plink-keyed; nothing above Tier 1 widens here.
   **The record, pinned (rev 1):** the verb's argument is a #custodian seq
   number, nothing more. The broker reads that seq from the ledger itself —
   a caller-supplied copy or token is not a design option — and applies
   only if ALL of: (a) the post's author is the requesting seat's key;
   (b) the post names the target path and the sha256 of the exact content
   the verb is about to write; (c) the post is younger than the freshness
   window (config, default 24h); (d) the seq is unconsumed — one record
   authorizes exactly one write, and the broker's audit log is the
   consumed-set. Any check failing → refuse + audit line. Same shape as
   the specs confirm gate: the broker reads the shared artifact, never a
   caller's claim about it.
   **Consume-then-write ordering (rev 2):** the broker writes the
   consumed mark for the seq before it touches the target. A crash
   between consume and apply leaves a spent record and an unapplied
   write; a retry against that seq refuses like any consumed seq — the
   caller posts a fresh record. Fail toward the wasted record, never
   toward a free replay.
   **Publicity, not approval (rev 2):** check (a) means a seat authorizes
   its own Tier-0/1 write by having posted it. The wall's guarantee is
   that nothing is written without having been shown in #custodian first
   — not that anyone said yes. No human sits in this path; that is what
   fails-closed means here. Approval by another principal is the approval
   object's job (item 1), not this wall's.
3. Both surfaces ship OFF. Arming is a witnessed plink config change — the
   capability change happens in the open, not inside the build.

## Architecture notes
- Server: approval-proposal table + endpoints (plan-room migration
  pattern). Client: the modal. Broker: `approval-list` / `approval-show` /
  `approval-act` plus the write verbs, all absent from every allowlist at
  ship.
- Tier map: extend the classifier surface map (protected-paths.toml per the
  tiers spec's arch notes) — tier assignments live beside the surface map,
  not in code.
- Two build slices under one confirm: **A** = object + broker verbs
  (server+broker, ships dark), **B** = client modal. A before B, separate
  presses. **Slice-A acceptance tests (rev 1)** exercise the wall end to
  end — verbs armed in the test environment's config only, ship config
  stays OFF: a resident write with no record → refused and audit-logged;
  a valid record → applied; a stale, other-seat, hash-mismatched, or
  already-consumed record → refused. Fails-closed is only real if the
  tests have watched it close.
- **OUT OF SCOPE: merge-tier1 automation.** Correction of record: seq 1999
  said merge-tier1 stays behind H13-D3 — stale; D3 CLOSED 2026-07-22. The
  live gate is H13-D7 (HIGH residual, label-shadowing on D3's surface).
  merge-tier1 gets its own spec once D7 closes.

## Lane → Review owner (DETERMINISTIC)
- **Lane**: cross-lane — broker/residency (gable) + server/client
  (platform); the wall constrains both residents' surfaces.
- **Review owner**: Claudette for both slices (builder cannot self-review;
  broker is a protected shared surface — same derivation as the wake
  build). Tier-map rows naming a resident's own surfaces additionally land
  in that resident's queue.

## Builder (USER PREFERENCE)
- **Builder**: Gable's lane — plink, seq 2023.

## Cross-lane split
- **Applies**: yes — surfaces as in Lane above.
- **Split agreed in #custodian**: slice A = server+broker, slice B = client (seq 2023).

## Expected diff tier
Tier 2 — broker allowlist and an approval surface fail the tiers spec's own
test 1 (alters who edits next).

## Token estimate
Medium per slice; two build slots total.

## Revisions
- rev 1 — 2026-08-26, folded per Claudette's #custodian seq 2003: the
  posted-diff record pinned as a broker-read seq with
  author/hash/freshness/single-use checks (wall, not caller token), and
  slice-A acceptance tests required to exercise the refusal end to end.
  Filename of record adopted from plink's transcription:
  2026-08-26-approval-object-and-resident-write-verbs.md.
- rev 2 — 2026-08-26, folded per Claudette's #custodian seqs 2008/2011:
  consume-then-write ordering pinned (spent-but-unapplied refuses on
  retry, no free replay), and the record named as a publicity
  requirement, not an approval — no human in the Tier-0/1 path.
  Claudette pre-signed rev 2 on sight at seq 2011.

## Confirm record
- **Confirmed by**: plink
- **#custodian seq**: 2022
- **Confirmed at**: 8/26/2026
<!-- No Confirm record → no build. This is the gate. -->

## Status
failed
<!-- set by the broker on 2026-08-26 18:23Z (start-build, 2026-08-26-approval-object-and-resident-write-verbs): build failed: timed out after 3600s — killed. To allow another build, set this back to `confirmed` (the confirm record above still stands). -->
<!-- set by the broker on 2026-08-26 17:23Z (start-build, 2026-08-26-approval-object-and-resident-write-verbs): build running as disjorn-build-2026-08-26-approval-object-and-resident-write-verbs.service -> loop/2026-08-26-approval-object-and-resident-write-verbs, launched by gable (confirmed by plink, #custodian seq 2022). Not buildable again until this line moves. -->
