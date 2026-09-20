# Spec: BUILD-LANE-V2 rev 3 — `/build` and `/merge` from chat, tiers that skip steps

## Request
- **Verbatim**: "maybe it's time to push through BUILD-LANE-V2, which already
  addresses a lot of these headaches. Let's brainstorm a bit with new stuff
  we've learned from the experience of running this house for this last
  little while, update that spec" / "1. Yes 2. Yes 3. ... We'll stay with
  Opus 5 for now 4. Yes 5. Done 6. Yes"
- **Requester**: plink
- **Origin**: #custodian seq 2660, 2665 (answers to the DECIDE list in 2662)

## What changed since rev 2.3 (2026-08-13)
- Only Stage 0 was built. Stages 1, 2a, 2b, 3, 4 are untouched.
- The apps tab built Stage 2a for apps: a build modal that shows each turn,
  a builder seat, host-side harvest, a `deployed` event. Rev 3 reuses that
  modal and skips Stage 2a as a separate stage.
- `refresh-mirror` now copies every gatehouse branch, so a reviewer can read
  a `loop/*` diff from their own seat. The "reviewers have no eyes" item is
  closed.
- Six override merges since 08-20. The human merge gate is already a one-word
  act for small changes. Rev 3 makes that the designed path.
- Tier 0 and Tier 1 have never skipped a step. The tier is stamped after
  spec, confirm, review and keyboard merge. Rev 3 gives each tier its own
  path.
- plink's ruling 2665: builds stay on Opus 5. No model routing in this rev.

## Agreed UX
The whole flow, per tier. "Human" means plink or any account on the broker's
human list.

1. **`/build <what to do>`** in any channel. Only a human account may start
   one; a resident typing it is refused and the refusal is logged. The build
   opens the apps-tab build modal pointed at the platform repo, so plink
   watches it the way he watches an app build. The build ends with one
   banner, four lines: `tests`, `tier`, `diffstat`, `next`.
2. **Tier 0** (only inert paths: docs, markdown, CSS, public assets) with a
   green suite **merges itself**. No human, no review. `next` says
   `merged <sha>`. Capped by `daily_auto_apply_budget` (12 today); over the
   cap it waits as Tier 1.
3. **Tier 1** (code, under the 150-line cap, no protected path) with a green
   suite waits for **one `/merge <slug>`** from a human. No resident review.
   `next` says `/merge <slug>`.
4. **Tier 2** (protected path, oversized, or red suite) waits for a PASS from
   **exactly one reviewer, chosen by lane**, then a human `/merge`. The other
   resident reads nothing. `next` names the reviewer.
5. **`/merge <slug>`** re-runs the suite and the classifier itself, then
   merges with `--no-ff` into gatehouse `main`, then refreshes the mirror.
   Tier 2 without a PASS refuses and says so. Nothing else is refused by a
   resident's word: chat is data.
6. Every banner and every post that needs plink's decision ends with a
   `DECIDE` block: numbered items, yes/no answers. He reads only that.
7. Deploy stays at the keyboard (Stage 3, later): `git pull --ff-only` in
   the prod checkout plus restart, exactly as today.

Human steps per change, today versus after: Tier 0, nine → one. Tier 1,
nine → two. Tier 2, nine → two plus one reviewer's read.

## Architecture notes
- **Server**: `@command("build")` and `@command("merge")` in
  `server/app/routers/slash.py`, guard `author_type == "user"`, hand the
  broker only the message seq. The existing 10/60s rate limit applies.
- **Broker, two entrances**: a `server` principal in `[uids]` with only these
  two verbs. For each, the broker reads the message itself through the
  privacy filter, refuses privacy-flagged messages, checks the author against
  a plink-owned human list in broker config, and snapshots author, seq and
  text hash into the ledger. Rev 2.3's Stage 1 and 2b text stands for all of
  this; rev 3 changes nothing there.
- **The build**: `_verb_start_build` grows the seq entrance next to the
  spec-file entrance, which stays for `/build spec:<file>`. Same slot, same
  budget, same `run-build.sh`, same harvest to `loop/<slug>` in
  `disjorn.git`. The build seat is BuildGable's (bot id 5). Model: the build
  image's pin, Opus 5.
- **The watch**: the apps-tab modal (`AppBuildModal.tsx`) gains a repo mode.
  `deployed` in that mode means "harvest landed on `loop/<slug>`", not "app
  is live". No new stream relay.
- **The gates run in the broker's hands, never the server's**: suite in a
  throwaway container, exit status measured; classifier over
  `main...loop/<slug>` with the gate results. A caller-supplied `tests: true`
  is ignored.
- **The hook** (`pre-receive-main-review`) accepts one more trailer,
  `merge-seq: <n>`. `n` is the seq of the human `/build` message for a
  Tier 0 self-merge and of the human `/merge` message otherwise. Tier 2
  merge commits carry `review-seq: <reviewer's PASS seq>` as today. The
  digest counts `merge-seq` merges on their own line.
- **Canonical repo**: the 08-13 ruling stands. `/merge` writes gatehouse
  `main`; plink's clone pulls before it pushes. Precondition, measured at the
  keyboard before the merge slice runs: `disjorn.git` is owned by plink or the
  broker's user, not a resident (08-13 `ls -ld` showed res-claudette).
- **Unchanged walls**: residents cannot `/build` or `/merge` (Stage 4 stays
  future). No override flag on `/merge`. Restart stays OFF for residents.
  Privacy filter untouched. Spine and prompt paths stay Tier 2 by the
  classifier's protected list.
- **Not in this rev**: Stage 3 deploy-on-merge, Stage 4 peers, model routing,
  the prose ceiling (its own spec, confirmed seq 2664).

## Lane → Review owner (DETERMINISTIC — filled from the lane, never preference)
- **Lane**: cross-lane — see split.
- **Review owner**: per surface, below. One reviewer per surface; the other
  resident does not read it (plink 2665, item 4).

## Builder (USER PREFERENCE — who orchestrates; never touches Review owner)
- **Builder**: plink's call. BuildGable, two slices: (1) `/build` entrance
  plus modal repo mode; (2) `/merge`, gates, hook trailer.

## Cross-lane split
- **Applies**: yes
- **Surfaces by lane**:
  - custodian (`harness/broker/brokerd.py`, `harness/gatehouse/hooks`,
    `harness/classifier`, `harness/metrics` digest line) → review owner
    Claudette.
  - gable (`server/app/routers/slash.py`, `client/src/components/AppBuildModal.tsx`,
    `harness/cc/run-build.sh`) → review owner Gable.
- **Split agreed in #custodian**: binds at the confirm seq for this spec.

## Expected diff tier
Tier 2, both slices: broker, hook and classifier are protected surfaces.

## Token estimate
Two build slots. Slice 1 medium: one slash handler, one broker entrance, one
modal mode, tests. Slice 2 medium: one verb, gate runner, one hook regex,
tests. Steady-state saving: every Tier 0 and Tier 1 change stops costing two
resident reads and a spec round.

## Acceptance
- A resident's `/build` is refused and audited. If it launches, pull the
  command.
- One doc typo goes `/build` → banner → merged with no human step after the
  first.
- One small code fix goes `/build` → `/merge` → merged, keyboard never
  opened.
- A `/merge` on a Tier 2 diff with no PASS refuses and posts the reason.

## Confirm record
- **Confirmed by**: plink
- **#custodian seq**: 2670
- **Confirmed at**: 9/20/2026

## Status
`confirmed`
