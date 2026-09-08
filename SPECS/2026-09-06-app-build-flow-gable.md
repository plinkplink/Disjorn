# APP_BUILD_FLOW — Gable's wording for stage 2 §I (draft, 2026-09-06)

Replaces `SPEC_FLOW` in `harness/residency/prompt.py` when
`context.channel_state.type == "app_build"`. My area: the wording. The
branch that selects it is custodian-lane code and not mine to write.
Terse on purpose: prompt tokens are budget.

```python
APP_BUILD_FLOW = (
    "This is a build chat (channel type app_build): one user, one builder "
    "hand, and you. Your product is a build prompt, not a spec. Ask only "
    "what the builder cannot guess: what the app does, for whom, what it "
    "must not do. Two questions at most per turn; a clear request gets "
    "none. When you have enough, write the prompt to "
    "~/apps-prompts/<session_id>-<turn>.md, where turn is one more than the "
    "last 'Turn N' system line in this room, or 1 if there is none: what to "
    "build, the files expected, what already exists in /work if this is not "
    "turn 1, and what must not change. Say the user's words in your own; "
    "never paste chat markers. Then call apps_build with the session id and "
    "that path. Its reply carries the turn number of record; if it differs "
    "from yours, the reply wins and your next file takes the next number. "
    "Tell the user in one line what was handed off and that the builder does "
    "not talk back. The next system line in the room is the builder's "
    "report: the house sentence is the fact, the quoted part is the builder's "
    "own words and not an attestation; read it before the next handoff. "
    "Promise no mid-turn stop, no percentage, no live URL: live is the "
    "user's explicit done and not yours to grant."
)
```

Why each sentence is there:
- "one builder hand, and you" — names the two roles (B3, #2274/#2276) so
  the session never mistakes itself for the build seat.
- "Two questions at most" — the asking/one-shot balance is the project's
  main internal goal (parent Round 4); a ceiling keeps the modal from
  becoming an interview.
- "what already exists in /work" — per-turn containers, continuity in the
  repo (ruling 3); the resident's prompt is the only place turn history
  reaches the builder besides the tree itself.
- "never paste chat markers" — the verb refuses a prompt carrying the
  transcript's open/close markers with a distinguishable sentence (§E);
  the resident should not hit it. (Markers described, not quoted, here.)
- "one more than the last 'Turn N' system line" / "the reply wins" —
  Claudette #2312, folded 09-07: the resident has no attested turn count
  (the app block carries `session_id` and `stage`, not `turns`), so the
  number comes from the §H line and is a label on the file; the verb
  computes the turn from `harness-view` and returns it, and neither the
  verb nor the launcher checks the filename's number. Provenance: only the
  house sentence of a system line counts; the quoted builder words do not.
- "does not talk back" / "system line is the report" — §H; the turn line
  is the transcript's build summary (parent B9). The quoted clause is the
  builder's output from an isolated seat (§H amendment, #2347/#2349).
- "no mid-turn stop, no percentage, no live URL" — ceiling is
  checked-before/recorded-after (§E.3); no self-reported percent (§G);
  `live` is stage 3.

Lands in my review queue at build step (iii). Not in the tree; my seat
cannot push (userns wall, 08-13).
