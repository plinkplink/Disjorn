# Spec: <one-line title>

<!--
Copy this file to SPECS/YYYY-MM-DD-<slug>.md and fill every field. The spec
file — not chat memory — is the state each fresh summon re-reads. No build
starts without a Confirm record. Governing plan: BUILD-LOOP.md.

Two fields below are STRUCTURALLY SEPARATE by ratification (Claudette,
#custodian seq 139): "Review owner" is deterministic — it is filled from the
lane and nothing else. "Builder" is user preference. Preference must be
physically unable to leak into the Review-owner box. Do not merge, reorder,
or cross-reference them. If you find yourself writing a name into Review owner
because someone asked for it, stop — that is the leak the split exists to
prevent.
-->

## Request
- **Verbatim**: <the request, quoted exactly as asked — no paraphrase>
- **Requester**: <username>
- **Origin**: <channel #name / seq — where the request first arrived>

## Agreed UX
<What the user will see and do. The behavior agreed in #custodian, plainly.>

## Architecture notes
<Where it lands in the codebase, the shape of the change, key seams touched.>

## Lane → Review owner (DETERMINISTIC — filled from the lane, never preference)
- **Lane**: <custodian | builder> — which lane the touched surface belongs to.
- **Review owner**: <derived from the lane, full stop. A change to Claudette's
  code/config/prompt → Claudette's review queue. A change to Gable's area →
  Gable's queue. Humans included, symmetric. This box is NEVER filled from who
  the user prefers.>

## Builder (USER PREFERENCE — who orchestrates; never touches Review owner)
- **Builder**: <whoever the user wants to build it. Preference wins here, full
  stop. This name does not, cannot, and must not flow into Review owner above.>

## Cross-lane split
<!-- Required ONLY if the build touches more than one lane. Delete this whole
section if the change is single-lane. -->
- **Applies**: <yes / no>
- **Surfaces by lane**:
  - <lane A>: <paths/subsystems> → review owner <A's owner>
  - <lane B>: <paths/subsystems> → review owner <B's owner>
- **Split agreed in #custodian**: <seq> — cross-lane splits are agreed in
  #custodian before the build starts (BUILD-LOOP.md).

## Expected diff tier
<Tier 0 inert | Tier 1 auto-apply + posted diff | Tier 2 protected/two-way
review — advisory; the classifier gates the actual result at merge.>

## Token estimate
<Rough build-token budget. Burns exactly once on a confirmed build.>

## Confirm record
- **Confirmed by**: <username — any human's confirm can start a build within budget>
- **#custodian seq**: <seq of the confirm message — witnessed by reference>
- **Confirmed at**: <timestamp>
<!-- No Confirm record → no build. This is the gate. -->

## Status
`draft`
<!-- One of: draft / confirmed / building / built@<branch> / merged / failed.
Update in place as the spec moves; the line is the current state of record. -->
