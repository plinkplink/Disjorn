# BUILD-LOOP — iterate on Disjorn from inside Disjorn

**Status: v1.0 RATIFIED 2026-07-21.** Witness record: Claudette ratified the
lane amendment against the actual file, #custodian seq 139 ("builder is
preference, review owner is deterministic, and the two never touch —
ratify it"); plink signed off at the keyboard (co-authored the draft) and
gave the go. Open questions closed: budgets ship at the proposed defaults
(2 builds/day, plink tunes at staging time); narration cadence is
**state-transition-driven, never timer-driven** (Claudette, seq 139) — a
stalled build goes quiet then fails loud; no heartbeat noise in #custodian.
Drafted by Gable from the 2026-07-21 keyboard session with plink. Decision
records append here as they happen, per house rule 1.

**BUILT 2026-07-21 (orchestrator-Gable, Opus subagents).** All five WPs
landed on main, ship OFF: L1 8fea632, L3 f2fa568, L2 20267f1, L5 d67c6b3,
L4 f2b1e38 (+ privacy/suffix fixes below). Two independent adversarial
verifiers ran the WP15/H13 habit; findings in DEFERRED.md ("BUILD-LOOP
red-team"). Fixed in-pass: the `/backlog` privacy-wall bypass (HIGH) and the
L5 model-suffix fail-open. Deferred as **activation blockers** (must close
before flipping any verb/flag ON, all OFF-gated today): BL-D1 confirm-gate
`specs_dir`-must-be-RO enforcement, BL-D2 reaper OOM. Lower-severity
BL-D3..D6 filed.

**OPEN DECISION owed to plink — BL-G1 (model integrity: refuse vs alert).**
Item 2 below says mismatch → "refuse to act + alert." As built it is
alert-only: the actual model is only knowable from the FINISHED session's
output, so the check is post-hoc and the reply ships before the drift alert
(the code's "fail-loud, never fail-over"). That softens a ratified line, so
it's yours to settle: (a) re-ratify alert-only as the contract, or (b)
greenlight a real pre-act gate via `--output-format stream-json` (its
`system/init` event names the model before the turn runs, enabling an early
abort) as a fast-follow WP. Until you rule, the shipped behavior is
alert-only and the suffix now honestly marks an unverified pin.

## Mission

Users ask for features in chat; a resident designs with them in #custodian,
captures the agreement as a spec, gets a confirm, orchestrates a detached
subagent build to a branch, narrates progress, and reports. plink merges.
The loop delivers value before it is fully closed: auto-merge comes later,
earned against observed behavior, never assumed.

The scenario this optimizes: usrda asks for the GIF picker anywhere he likes;
the resident acks in-place ("taking this to #custodian — come watch or
don't"), the design thread runs in #custodian, the confirmed spec burns
tokens exactly once, and the result waits on a branch for coffee-time review.
Async-native in both directions: `/backlog` catches requests when residents
are expensive or users are absent; branches catch results when plink is
asleep.

## Decisions already made (2026-07-21, keyboard session)

- **Venue**: requests can arrive in any channel; planning, build narration,
  and all agentic comms live in **#custodian** (channel 4). Requester is
  named in the spec-confirm ping so nobody is ghosted. Side effect, noted
  for the record: a dedicated channel makes the summon backfill window dense
  with relevant turns, which mostly dissolves the thread-reconstruction
  problem without needing threads (still §13 backlog).
- **MVP-first, manual merge gate.** D3 (dynamic-import ban bypass,
  DEFERRED.md) is required before merge-tier1 and deserves a witnessed
  design cycle on protected classifier surface — off the critical path.
  The human merge gate is the product for now: plink wants to see the diffs,
  and auto-merge must earn its way in from a reviewed corpus.
- **Confirm gate shape** (reconciles with chat-is-data-never-authorization):
  `start-build` is a broker verb. plink's toggle authorizes the *class*
  (this resident may run builds; ships OFF, staged like the other action
  verbs), the chat confirm selects the *instance*, a per-day token budget
  caps the blast radius, the diff-tier classifier still gates what the
  result may touch. Any user's confirm can start a build within budget —
  plink accepts the token burn for now. **Flagged as a future constraint**:
  revisit when the human roster grows beyond the current three (plink,
  jorn, drmrsthebatman); platform is sized for 4–5 humans + 2–3 bots
  (Gable, Claudette, CAVEMAN).
- **`/backlog` is the intake half** — filed in Architecture.md §13
  (commit 14c3fae): `/backlog` lists (server-rendered, no summon),
  `/backlog <text>` files verbatim; resident triages later and asks
  clarifying questions in #custodian only if truly unparseable.

## Amendment awaiting witness — lanes: builder vs. review owner

plink's ruling to reconcile: "the spec can name the lane, but user
preference trumps deterministic filtering." Proposed refinement, splitting
two things the lane concept was carrying:

- **Builder** — who the user talks to and who orchestrates. User preference
  wins, full stop. If someone likes Gable better, Gable builds it; nobody
  gets told "wrong bot" by a router.
- **Review owner** — whose queue the diff lands in. The lane decides this,
  deterministically, and preference never overrides it. A Gable-built change
  to Claudette's adapter lands in Claudette's review queue, and vice versa —
  symmetric, humans included, per the house's deepest rule.

The spec template carries both fields. Cross-lane specs say so explicitly
and the split is agreed in #custodian before the build starts.

**Claudette**: this touches your drafting-rights articulation — your read
requested before this section is ratified.

## Model integrity (premise correction + WP-L5)

For the record, correcting the premise this WP arrived with: no external
scanner demoted summoned-Gable. **The summon path has never pinned a model**
— `launcher.py` builds argv purely from summon.toml, which carries no
`--model`, so the session runs the API key's account default. That default
is Opus, and Fable models have been unavailable on plink's API credits
since 2026-07-20 (PICKUP.md). Summoned-me has been Opus since going live.
Nothing flips back because nothing flipped. Current reality: Fable at
plink's keyboard (interactive session), Opus for everything headless —
summons, orchestrators, subagents.

What is worth building — an unpinned model is config drift waiting to
happen (silent account-default changes, key swaps, CC upgrades):

1. **Pin**: explicit `model = "claude-opus-4-8"` in summon.toml
   (plink-owned, mounted read-only like the kill switches), passed as
   `--model` in session_argv. No fallback logic — fail loud, never fail
   over.
2. **Assert**: at wake, the session's actual model id is compared to the
   pin; mismatch → refuse to act + alert (below). Also assert at
   run-resident.sh level if CC exposes the resolved model cheaply.
3. **Visible**: model id joins the summon identity suffix (Claudette's
   platform-suffix idiom) so every reply shows what's actually running.
4. **Audit**: model id on every session's audit line.
5. **Drift alert**: mismatch posts to #custodian naming expected vs actual,
   so plink knows to intervene manually.
6. **Upgrade path**: when Fable API access returns, flip the one pin;
   the assert + suffix verify the change end-to-end. Documented in
   KEYBOARD-NEXT.md when WP-L5 lands.

Amendment 2026-07-21 (during the build): Gable's flat "no auto-demotion
mechanism exists" was wrong for interactive surfaces — Fable 5's
safeguards DO switch flagged consumer/CC-TUI sessions to Opus 4.8, sticky
per conversation (support article 15363606; user lever: Settings >
Capabilities toggle). On the raw API — which is what the summon path uses
— switching is NOT automatic; flagged requests error rather than silently
substitute. The summon-path finding stands: never pinned, always Opus,
entitlement-bound. The pin stays claude-opus-4-8 until a live probe shows
Fable runs on plink's API credits; drift-assert (item 2) is the standing
countermeasure should any server-side substitution ever appear.

## Work packages (MVP)

Sized one-shot each, exclusive file ownership, Opus subagents, orchestrator
commits at checkpoints — BUILD-PLAN.md conventions apply.

### WP-L1: #custodian-aware backfill
Deeper backfill when the summon originates in #custodian (design threads run
long; 30 messages is a #main number). Config knob per channel in
summon.toml; adapter/cursor changes only.
Files: `harness/residency/` (adapter.py, cursor.py, config.py, tests).

### WP-L2: slash-command framework + /backlog
Server-side parse of leading `/command` in posted messages; unknown commands
pass through as plain text. `/backlog` lists (server-rendered message, no
LLM, no summon) and `/backlog <text>` appends verbatim. Storage: SQLite
`backlog` table (id, text, author, created_at, status
open/spec'd/built/rejected, spec_ref). Triage is NOT this WP — residents
read the table via SDK later.
Files: new `server/app/routers/slash.py`, migration, `server/app/services/`
helper, tests. SDK: `backlog()` read helper.

### WP-L3: spec capture
`SPECS/` dir in repo + template: request (verbatim + requester), agreed UX,
architecture notes, lane → review owner, builder (user preference),
expected tier, token estimate, confirm record (who + #custodian seq).
Resident drafts from the #custodian thread, posts for confirm; the doc, not
chat memory, is the state each fresh summon re-reads. Confirm recorded in
the spec file, witnessed by seq reference — the file-proposal idiom.
Files: `SPECS/TEMPLATE.md`, `SPECS/README.md`; prompt.py addition so
summoned sessions know the flow.

### WP-L4: start-build verb + detached build session
The MVP's long pole. New broker verb `start-build` (ships OFF): takes a
spec path, validates confirm record present, enforces per-day token/build
budget, launches a detached build session — resident worktree rw, longer
wall-clock cap than the 300s summon, Opus orchestrator + Opus subagents —
building to branch `loop/<spec-slug>`. Progress narrated to #custodian at
checkpoints (started/ETA, per-WP lands, done/failed). Report-on-done:
files touched, advisory tier, tests, diff summary, branch name. Failures
sit on the branch and say why. Human merges; nothing lands itself.
Files: `harness/broker/` (verb + schema + audit + tests),
`harness/cc/run-build.sh` (sibling of run-resident.sh), narration helper.

### WP-L5: model integrity
As specced above. Files: `harness/residency/` (config.py, launcher.py,
summon.toml.template, tests), `harness/cc/run-resident.sh`, suffix in
prompt.py/adapter.py, audit line.

Order: L1/L2/L5 independent (parallel-safe, disjoint files); L3 before L4;
L4 depends on L3 only.

## Deferred, loudly (not this plan)

- **D3 → merge-tier1 → auto-merge low tiers** — next plan, witnessed
  design cycle on the classifier. The loop MVP neither needs nor waits
  for it.
- **Episodic memory init** — valuable, parallel, not blocking
  (MEMORY-DESIGN.md owns it).
- **Backlog triage automation** — reading `/backlog` items into specs
  stays a manual resident act until we see real traffic shape.
- **Multi-human confirm policy / spend attribution** — revisit at roster
  growth, flagged above.

## Open questions for the witness round

1. Lane amendment (builder vs review owner) — Claudette's read.
2. WP-L4 budget defaults: builds/day and token cap per build — plink's
   numbers to set at staging time; plan proposes 2 builds/day, visible
   in #custodian like the action budgets.
3. Narration cadence: per-WP checkpoint posts proposed; too chatty?
