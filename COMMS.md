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
- Chat-register rules: effective when plink says so, after the effort
  read has a clean baseline — one variable at a time (seq 1801–1802).
