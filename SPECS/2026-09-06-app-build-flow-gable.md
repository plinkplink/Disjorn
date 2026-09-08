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
    "~/apps-prompts/<session_id>-<a name you have not used>.md: what to "
    "build, the files expected, what already exists in /work if the room "
    "shows an earlier turn, and what must not change. Say the user's words "
    "in your own; never paste chat markers. Then call apps_build with the "
    "session id and that path. Its reply names the turn of record and is "
    "the evidence the handoff happened; your memory of posting is not. Tell "
    "the user in one line what was handed off. The builder's report arrives "
    "as the next system line in the room: the house sentence is the fact, "
    "the quoted part is the builder's own words and not an attestation; "
    "read it before the next handoff. The user can stop a turn from the "
    "modal; you cannot, and you do not promise how fast it ends. Promise no "
    "percentage and no live URL: the stage bar is the record of where the "
    "build is, and live is the user's explicit done, not yours to grant."
)
```

Revised again 2026-09-08 (Claudette #2397): the turn arithmetic is cut,
since nothing reads the filename's number and the reply carries the turn of
record; the stage vocabulary now points at the bar instead of restating it.

Revised 2026-09-08 (plink #2395): "does not talk back" dropped, since it
describes today's transport, not a rule; "no mid-turn stop" superseded by
slice (iv) (`2026-09-08-apps-stop-turn.md`); evidence sentence added per
BuildGable #2372 / Claudette #2374. Not final: the selecting branch needs
`2026-09-08-gable-cross-channel-context.md` slice A first (my adapter drops
the context block, #2386), and Claudette's joint read (#2375) is owed.

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
- "a name you have not used" / "the reply names the turn of record" — the
  verb computes the turn from `harness-view` and returns it; neither the
  verb nor the launcher reads the filename, so a computed guess was
  decoration with a failure mode (Claudette #2312, #2397).
- "system line is the report" — §H; the turn line
  is the transcript's build summary (parent B9). The quoted clause is the
  builder's output from an isolated seat (§H amendment, #2347/#2349).
- "no mid-turn stop, no percentage, no live URL" — ceiling is
  checked-before/recorded-after (§E.3); no self-reported percent (§G);
  `live` is stage 3.

Lands in my review queue at build step (iii). Not in the tree; my seat
cannot push (userns wall, 08-13).
