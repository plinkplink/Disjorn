# Spec: `read_repo_file` grows a `rev` — branch vision for the bot seat

<!-- DRAFT written at the keyboard (plink's session, 2026-08-19) from
res-claudette's proposal at #custodian seq 1323, so the next step is her
review of it rather than a blank page. A keyboard draft authorizes nothing:
the Confirm record below is empty until a human confirms in #custodian. -->

## Request
- **Verbatim**: "`read_repo_file` gains an optional `rev`, defaulting to
  today's main working tree, accepting `main`, a sha, or
  `gatehouse/<repo>/<branch>`, resolved read-only against the mirror. Plus a
  cheap mode returning just the blob sha / last commit touching a path, which
  is what kills the read-main-twice token burn when I am checking whether a
  file moved."
- **Requester**: res-claudette (proposal, Ask A); ordered built by plink,
  2026-08-19, keyboard session ("we need to get that built").
- **Origin**: #custodian seq 1323 (file-vision follow-on). Deferral being
  discharged: SPECS/2026-08-14-file-vision.md shake-out ("deferred to the
  tools discussion, plink 08-15") — this spec IS that discussion's landing.

## Agreed UX
1. **`rev` argument.** `read_repo_file` accepts an optional `rev` string.
   Absent → exactly today's behaviour (checked-out main working tree; zero
   change for existing callers). Present → the path is resolved at that rev
   via read-only git plumbing against the mirror (`git -C /opt/disjorn show
   <rev>:<path>` or `cat-file`), never a checkout, never a write. Accepted
   forms: `main`, a sha, or `gatehouse/<repo>/<branch>` — validated with the
   broker's existing rev charset rule (`[A-Za-z0-9._~^/{}-]`, max 200, no
   leading dash, and additionally NO range operators: one rev, not `a..b`).
   A directory path at a rev lists via `ls-tree`, same shape as today's
   listing. Unknown rev or path-at-rev answers plainly ("no such rev in the
   mirror — refresh_mirror first?"), because a stale mirror is the usual
   cause and the tool should say so.
2. **`sha_only` mode.** Optional boolean. True → return only the blob sha of
   `<rev>:<path>` (or "absent"), no content. Two such calls — the branch and
   `main` — are the motion ping from file-vision item 2, finally executable
   from her seat: zero file reads, zero content tokens. This closes the
   half-closed CLOSES claim in the file-vision shake-out (line: "half-closed
   for her seat").
3. **Catalogue membership (proposal Ask B) — DECISION POINT, flagged not
   baked in.** `read_repo_file` is adapter-side, so it sits OUTSIDE the
   generated-schema drift check that file-vision item 3 built. Either (a) add
   a second table to verb_surface.toml — adapter tools, described-not-switched
   since the broker does not authorize them — and extend
   test_verb_surface.py to cover it, or (b) record the exemption in
   verb_surface.toml in so many words. Claudette's argument for (a): an
   exemption nobody recorded is how the first four drift instances happened.
   Keyboard concurs with (a); plink decides at confirm.
4. **PROTOCOL.md footnote (proposal Ask C).** The motion-ping example gains
   BuildGable's field note from #1322: run the two revs as separate
   `rev-parse` calls — the two-arg form gets mangled by quoting under
   `podman exec ... bash -lc`. Also updated to name the bot-seat path now
   that it exists (the "no shell for this yet" parenthetical at
   PROTOCOL.md:112-114 comes out).

## Architecture notes
- `bots/claudette/core.py`: `READ_REPO_FILE_TOOL` schema gains `rev` +
  `sha_only`; `_read_repo_file` grows the git-plumbing branch beside the
  existing working-tree branch. The path-escape check stays for the
  working-tree branch; the rev branch never touches the filesystem, so its
  wall is the rev/path validation plus git answering from the mirror's own
  object store.
- Same change lands in `bots/fable` if Gable's adapter carries the tool
  (verify during build; if absent, note it and move on).
- `harness/broker/verb_surface.toml` + `gen_verb_surface.py` +
  `tests/test_verb_surface.py`: only under decision (a) of item 3.
- `harness/broker/PROTOCOL.md`: item 4.
- Deploy note: adapter code runs from the DEPLOYED copies, not the repo
  (2026-07-23 lesson) — the build's landing step must name the copy-out, and
  Claudette knows it's live when the arg shows up in her own schema (her
  words, seq 1369).

## Lane → Review owner (DETERMINISTIC — filled from the lane, never preference)
- **Lane**: cross-lane (see split).
- **Review owner**: per surface, below.

## Builder (USER PREFERENCE — who orchestrates; never touches Review owner)
- **Builder**: plink's call at confirm.

## Cross-lane split
- **Applies**: yes
- **Surfaces by lane**:
  - Claudette's adapter (`bots/claudette/core.py`, her tool schema) → review
    owner Claudette.
  - custodian (`harness/broker/verb_surface.toml`, `gen_verb_surface.py`,
    its tests, `PROTOCOL.md`) → review owner Gable.
- **Split agreed in #custodian**: <seq — record at confirm; stated here for
  the confirm to witness, same shape as file-vision's>.

## Expected diff tier
Tier 2 advisory — `bots/claudette/core.py` is a protected path
(protected-paths.toml), and verb_surface.toml is broker config.

## Token estimate
One small build slot — an argument, a plumbing branch, a schema table or a
recorded exemption, a doc footnote, tests for each.

## Confirm record
- **Confirmed by**: <pending>
- **#custodian seq**: <pending>
- **Confirmed at**: <pending>
<!-- No Confirm record → no build. This is the gate. -->

## Status
`draft`
