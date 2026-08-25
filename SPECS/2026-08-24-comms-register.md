# Spec: #custodian register + comment/doc brevity rules, written into the tree

## Request
- **Verbatim**: "We still need to write up brevity rules in various places, let's do that before we lose the context to the message window."
- **Requester**: plink
- **Origin**: #custodian seq 1816 (rulings converged across #1797–#1802)

## Agreed UX
Doc-only. Three targets, exact text below; no code changes.

1. **New file `COMMS.md`** (repo root) — the register rules for #custodian,
   binding both residents and build-seat banners. Revisable by argument
   (normal review), not by prompt surgery — Claudette's #1798 condition.
2. **`SPECS/TEMPLATE.md`** — one added comment in the header block.
3. **`harness/cc/build-kernel.md`** — one added rule (becomes rule 5 of six;
   JSON-output rule renumbers to 6).

### Text of `COMMS.md`

```markdown
# COMMS.md — #custodian register

Scope: #custodian only. Other channels unchanged (plink, seq 1797). Binds
both residents and build-seat banners. Revise by review, not by surgery:
edits to this file land in both residents' review queues.

## Chat register
- Verdict or finding in the first sentence. Review findings are one line
  each — BLOCK / NOTE / PASS, with file:line. No preamble, no closing
  synthesis. If it needs prose, it needs a spec file.
- Post only deltas and disagreements. Never restate a broker banner or
  another resident's finding to agree with it — silence is agreement; only
  disagreement gets typed.
- No section headers on messages under ~10 lines. Status posts ≤120 words;
  reviews as long as the findings require and no longer.
- Plain sentences, actor as subject. No reveal-at-the-end constructions.
  Coined terms get defined on first use.

## Code comments (all lanes, enforced in review)
- A comment states a constraint the code cannot show. Never restate the
  adjacent line; never narrate what the next line does.
- Builds do not copy an existing verbose comment style — this rule
  outranks "match the surrounding style" for comments specifically.
- Reviewers flag comment-ratio drift as a finding.

## Docs and specs
- When a spec closes, superseded narrative is pruned, not appended around.

## Ceremony
- One review owner per build (the lane's owner). The other resident reads
  the same artifact only when the owner or a human asks (seq 1802).

## Activation
- Comment/doc/ceremony rules: effective on merge.
- Chat-register rules: EFFECTIVE 2026-08-25 (plink, seq 1875; effort-read
  specimens = seqs 1829 and 1851). Gable kernel bullet applied with this
  flip, per the staged diff.
```

### Insertion into `SPECS/TEMPLATE.md`
Inside the header HTML comment (after the Review-owner paragraph, line 14):

```
Style: COMMS.md binds this file's prose — and when a spec closes,
superseded narrative is pruned, not appended around.
```

### Insertion into `harness/cc/build-kernel.md`
New rule after rule 4 (current rule 5, the JSON contract, renumbers to 6):

```
5. **Comments state constraints the code cannot show — nothing else.**
   Never restate the line below a comment. Do not copy an existing verbose
   comment style; for comments, this rule outranks matching the
   surrounding code. Same bar for any .md you touch: add facts, not
   narrative.
```

## Architecture notes
Root `COMMS.md` is new. TEMPLATE.md and build-kernel.md get single
insertions, no other lines move (build-kernel renumbering aside). Nothing
executable changes; no restart, no migration.

## Lane → Review owner (DETERMINISTIC — filled from the lane, never preference)
- **Lane**: cross-lane (see split)
- **Review owner**: per split below.

## Builder (USER PREFERENCE — who orchestrates; never touches Review owner)
- **Builder**: plink transcription (doc-only, keyboard lane) — or a doc-only
  build branch if preferred.

## Cross-lane split
- **Applies**: yes
- **Surfaces by lane**:
  - COMMS.md (binds Claudette's conduct) → review owner Claudette
  - COMMS.md (binds Gable's conduct) + this draft → review owner Gable
  - TEMPLATE.md, build-kernel.md (keyboard/build harness) → review owner plink
- **Split agreed in #custodian**: 1821 (the split was stated in this spec as
  posted before confirm; the confirm covers it)

## Expected diff tier
Tier 0/1 — inert docs; posted diff suffices.

## Token estimate
Near zero — transcription, no build loop.

## Confirm record
- **Confirmed by**: plink
- **#custodian seq**: 1821
- **Confirmed at**: 8/24/2026

## Status
`merged`
<!-- transcribed at the keyboard 2026-08-24 per the Builder line (doc-only):
COMMS.md created; TEMPLATE.md + build-kernel.md insertions landed; kernel
rule count refs (build-kernel header, BUILD-SEAT-CONTRACT x2) updated
five->six per this spec's own "rule 5 of six"; deployed kernel copy at
/usr/local/lib/disjorn/build-kernel.md refreshed. -->
