# SPECS — the build loop's unit of state

A spec is the durable record of one agreed build. Chat memory evaporates
between summons; the spec file does not. Each fresh summon re-reads the spec,
not the conversation, to know where a build stands.

Governing plan: **BUILD-LOOP.md** (ratified 2026-07-21). This README is the
flow in brief; the plan is the authority.

## The flow

1. **Draft.** A design discussion in #custodian converges. The resident drafts
   a spec from `TEMPLATE.md`, filling the request verbatim (with requester and
   origin channel/seq), the agreed UX, architecture notes, the lane → review
   owner, the builder, expected tier, and a token estimate.
2. **Post for confirm.** The resident posts the drafted spec in #custodian and
   names the requester in the confirm ping — nobody gets ghosted.
3. **Record the confirm.** When a human confirms, the confirm record goes into
   the spec file: who confirmed + the #custodian seq of their confirm message.
   The seq is the witness. **No confirm record → no build.**
4. **State lives in the file.** Status moves `draft → confirmed → building →
   built@<branch> → merged` (or `failed`), updated in place. The next summon
   reads the file to pick up where the last left off — never chat scrollback.

## Review owner vs. builder — kept apart on purpose

The template splits two things the "lane" idea once carried, and the split is
structural by ratification (Claudette, #custodian seq 139):

- **Review owner** is *deterministic* — the lane decides whose queue the diff
  lands in, and user preference never overrides it.
- **Builder** is *user preference* — whoever the user wants orchestrates.

Preference must be physically unable to leak into the review-owner box. A
cross-lane build (a change spanning both lanes) carries an explicit split
section, and the split is agreed in #custodian before the build starts.

## Naming

One file per spec: `SPECS/YYYY-MM-DD-<slug>.md`, e.g.
`SPECS/2026-07-21-gif-picker.md`. Date is the draft date; slug is a short
kebab-case handle that also names the build branch (`loop/<slug>`).
