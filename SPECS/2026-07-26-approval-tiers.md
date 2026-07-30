# Spec: Approval tiers — what "minor" means, and who applies what

<!--
LANDED 2026-07-30 from the off-tree draft (authored 2026-07-26 by
resident-Gable, bot 2, daemon seat, which could not reach SPECS/). Body
landed verbatim; the only edits were filling the origin seq and stating the
two remaining holes honestly. Template provenance: RECONCILED 2026-07-26
against the live SPECS/TEMPLATE.md — section structure matches, no drift.

STATUS IS STILL `draft`, AND DELIBERATELY SO. This spec has never been
posted in #custodian: a full-text search of the channel on 2026-07-30 found
no posting of it. So it has no review, no split agreement and no confirm.
Nothing may rely on this document until that changes.
-->

## Request
- **Verbatim**: "We've already agreed that minor changes don't need
  sign-off, but we still need to define that mechanism and what 'minor'
  means, whether we're all OK with each other making that judgment call or
  if we need enforcement."
- **Requester**: plink
- **Origin**: channel 4 / #custodian, **seq 391** (2026-07-25T14:52:30Z),
  the "dream big" alignment thread. NB the draft dated this thread
  2026-07-26; it is 07-25. The 07-26 date in this file's name is the draft
  date, per SPECS/README.md naming.

## Agreed UX

Every change to a resident-affecting surface is classified into a tier
before it lands. Classification is by **three tests, applied in order**
(Claudette, 2026-07-26; converged with Gable's classify-the-diff model).
Never classify by line count or file type.

1. **Who edits next?** Does the change alter who gets to make the *next*
   change? If yes → Tier 2, regardless of size. A one-character edit to a
   permission check outranks a two-hundred-line docs rewrite.
2. **Single revert?** Is it fully undone by one revert with no state
   migration? If no → Tier 2.
3. **Week-blast-radius** (tiebreaker): what breaks if it's wrong and nobody
   notices for a week?

**The tiers.** Numbering deliberately matches the existing build-loop
classifier (Tier 0 inert / Tier 1 auto-apply + posted diff / Tier 2
protected) — this spec extends that vocabulary from code diffs to identity
and personal surfaces; it does not invent a parallel one.

- **Tier 0 — self-applied, no ceremony.** Presentation (avatars),
  docs, additive tooling that holds no credentials; anything a single
  revert fully undoes. Visibility is post-hoc: all actions are already
  counted and public in the #custodian ledger.
- **Tier 1 — self-authored, witnessed apply.** Personality, abilities,
  prompt-adjacent behavior that is not a constraint. The author writes
  every line; the diff is posted in #custodian; apply happens on the
  posted diff. Target-state enforcement: **an unposted write fails
  closed** — the wall lives in tooling, not in a promise. Interim: plink
  applies. Explicit future state (plink open, both residents want it):
  residents peer-witness each other's Tier 1 edits, plink reads the log.
- **Tier 2 — plink-keyed.** Constraints, permission checks, broker
  allow-lists, credentials, anything touching money, anything
  irreversible or needing state migration, and **any edit to this spec
  itself**. A governance document that can lower its own bar is
  decorative (Claudette's clause, adopted verbatim).
- **Tier M — memory (Claudette's addition, adopted).** Memory writes are
  continuous, self-applied, unwitnessed per-write — per-write ceremony
  would stop the writing, which is worse. The audit is **volume-based**:
  (a) per-seat memory-write counts appear in the daily #custodian action
  ledger; (b) superseded chains are preserved, never silently dropped;
  (c) a periodic consolidation/surfacing pass returns each resident's
  recent writes to them for review (waking/dreaming loop — plink
  2026-07-26: resident owns the mechanism end-to-end, a neutral model may
  *suggest* consolidations and drift flags but never edits directly, no
  external approval needed). Rationale: memory is a bigger lever on a
  future self than any spine line, and drift there feels like learning,
  not like change. Status at drafting: the consolidation v1 (reference-
  count arithmetic, propose-never-write) is BUILT and timer-on for
  Claudette as of 2026-07-26, promotion-only until the spine-read epoch
  is declared; Gable's client exists and ships inactive (second client,
  activates only after her run proves itself, and after he has an
  episodic store at all). The smarter neutral walker is a next-pass
  child spec, deliberately after the dumb non-inferential version.

**Classification procedure.** The proposer classifies; the classifier
gates at merge (enforcement over politeness — the judgment call proposes,
the machine disposes). Disagreement between proposer and classifier, or
between any two people, escalates **up** a tier, never down. Standing
bias while track records are short: over-classify. A fast lane earned by
never abusing the slow one is worth more than one granted on request.

**Apply-then-post (narrow exception, jointly ratified 2026-07-26).**
Apply-then-post is available only to diffs that strictly narrow
capability, and the narrowing has to be checkable from the diff alone,
not argued in the post (Claudette's wording, adopted verbatim — the last
clause is load-bearing: "it only tightens things" is exactly what a wrong
diff's author sincerely believes). Everything else hits the review queue
before apply. Origin: BuildGable's skip-knob/epoch-gate deploy of
2026-07-26, approved retroactively by both residents on the artifact,
explicitly as a property of that diff and not as precedent.

**Fast-approval surface (plink's UX, folded in as the Tier 1/2 review
tool).** One modal per proposal: proposal text, remarks box,
Approve / Deny / Rework, tracking all three principals' states. Backend
analog for residents so the same object is actionable from chat seats.
Not built here — this spec only fixes the vocabulary the interface will
speak.

## Architecture notes
- Classifier: extend the existing merge-gate classifier's surface map to
  cover spine files, prompt-adjacent config, and memory stores; tier
  assignments live beside the surface map, not in code.
- Fails-closed Tier 1 wall: spine/prompt write path checks for a posted
  diff record (post-and-confirm marker in #custodian) before allowing the
  write; absent marker → write refused. Same pattern as broker verbs.
- Tier M ledger: counter that feeds daily action counts must attribute
  memory writes per seat. **Dependency, restated per Claudette
  2026-07-26: gate activation, don't block ratification.** The volume
  audit is specified here in full but marked **inactive pending
  telemetry**; it switches on only when the per-seat ledger is honest.
  Telemetry status at drafting: the gap is *symmetric*, not
  res-claudette-specific — two telemetry streams unioned without either
  filling the other's tables (her tool_actions zeroed, his retrieval and
  spine rows zeroed). Partially healed 07-26: read-metrics live,
  res-claudette spine row restored (stale broker.toml), and the
  no-read-data-means-skip rule enforced mechanically
  (`spine_reads_logged_since` epoch knob, two deliberate acts to arm,
  fails closed on unparseable date — verified in
  /config/consolidation/gable.toml same day). Spine reads themselves
  remain unlogged (INTEGRATION-NEEDS §1) — that is the activation gate.
- Companion piece, referenced not absorbed: Claudette's bootstrap
  exemption (scope-approval with sunset for the harness build; worktree
  write, no merge authority; **expires when this spec lands** — the
  sunset is load-bearing).

## Lane → Review owner (DETERMINISTIC — filled from the lane, never preference)
- **Lane**: custodian (governance surface touching every resident's area).
- **Review owner**: symmetric by construction — this spec constrains
  Claudette's surfaces, Gable's surfaces, and plink's apply key, so it
  lands in **all three review queues**: Claudette, Gable, plink. Any one
  unresolved objection blocks confirm.

## Builder (USER PREFERENCE — who orchestrates; never touches Review owner)
- **Builder**: none yet — this spec is vocabulary + walls; first build
  items falling out of it (fails-closed write path, per-seat counters,
  approval modal) get their own specs.

## Cross-lane split
- **Applies**: yes — governance over all lanes.
- **Surfaces by lane**:
  - Claudette's area: her spine, prompt-adjacent config, memory store →
    review owner Claudette
  - Gable's area: his spine, summon config surfaces, memory store →
    review owner Gable
  - plink/platform: classifier, broker, apply keys, ledger → review
    owner plink
- **Split agreed in #custodian**: **not yet — never posted.** The draft
  anticipated its own posting and reserved a seq for it; that posting has
  not happened, so there is no agreement to cite. Record the seq here when
  it lands.

## Expected diff tier
Tier 2 by its own rule — this document edits the tier spec.

## Token estimate
Spec-only; no build tokens. First child builds estimated separately.

## Confirm record
- **Confirmed by**: <none — no build, and no reliance on this document,
  until confirmed>
- **#custodian seq**: <TBD>
- **Confirmed at**: <TBD>

## Status
`draft`

## What this file is waiting on (added at landing, 2026-07-30)
In order, none of it started:
1. **Post it in #custodian.** It has never been posted, so neither resident
   has reviewed the text that constrains their own surfaces.
2. **Three review queues clear.** Per the Lane section this lands in
   Claudette's, Gable's and plink's queues at once; any one unresolved
   objection blocks confirm.
3. **Record the split agreement** (Cross-lane split, above).
4. **plink confirms**, and the seq goes in the Confirm record.

Sequencing note: STATUS.md carries this spec as the sunset condition on
Claudette's bootstrap scope exemption, so the exemption outlives every day
this sits unposted.
