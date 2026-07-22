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

> **UPDATE 2026-07-22 — option (b) is BUILT and ships OFF; the decision is now
> one config line, not a WP.** `stream-json` was verified against the real CLI:
> the `system/init` event carries the resolved model id and is the FIRST line
> of stdout, before any content. Implemented in `harness/residency/launcher.py`
> as `[container].model_gate` with three states — `"off"` (**default;
> byte-for-byte today's alert-only behaviour**), `"alert"` (detected at init so
> the log lands before the reply), `"refuse"` (session killed at init, the
> channel sees only the operator line, #custodian sees `MODEL GATE REFUSED`).
> Proven end-to-end against the real `claude` binary: a refused mismatched
> session returned at 1.1s with an empty reply, versus 2.8s for the full run.
>
> **Do not flip `refuse` without first changing `session_argv` to
> `--output-format stream-json --verbose`.** The live config still says
> `--output-format json`; flipping the gate alone refuses every summon —
> loudly, with the fix named, but the resident goes silent. Sequence:
> change `session_argv`, run at `"alert"` until real summons confirm init
> matches the pin, then flip to `"refuse"`.
>
> Two caveats bearing on how much (b) actually buys, both filed in
> RED-TEAM-BACKLOG.md: the gate kills the wrapper but **not the container**
> (KB-D1c), so a mid-session refusal's side effects can continue inside it; and
> the legacy post-hoc check it replaces reads `modelUsage`'s first key, which is
> *haiku*, so it can fire false drift alerts (KB-D1a) — re-read any past alert
> before treating it as evidence. The five 2026-07-21/22 drifts are unaffected
> and remain real.

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

## Amendment 2026-07-22 — the summon path is Claude Code, not the raw API

**Appended per house rule 1 (decision records append here as they happen).
This corrects a premise in the ratified text above; it is NOT a re-ratification
— the correction is factual, the governance call it feeds is plink's.**

The 2026-07-21 amendment says of the summon path: *"On the raw API — which is
what the summon path uses — switching is NOT automatic; flagged requests error
rather than silently substitute."* **The first clause is wrong.** The summon
session argv is `claude -p --output-format json` (see the live
`[container].session_argv`), which is **Claude Code**, not the raw Messages
API. So the summon path plausibly inherits CC's documented sticky safeguard
auto-switch — exactly the behaviour the amendment was excluding.

**Observed, not theorised.** `harness/residency`'s WP-L5 drift assert has fired
five times in production: 2026-07-21 18:41, 20:41, 20:50 and 2026-07-22 06:14,
06:24, every one `pinned claude-fable-5 but session ran claude-opus-4-8`, all in
channel 4. Four Fable-pinned summons before 18:40 on 07-21 ran clean, so the
shape is intermittent-then-persistent rather than a flat entitlement failure —
and Fable IS entitled on this account (`additionalModelOptionsCache` offers it;
probed HTTP 200 on 07-21).

Consequences for anything downstream of the old premise:
1. **BL-G1 is no longer hypothetical.** "Refuse to act vs alert-only" is now a
   choice about a thing that demonstrably happens, several times a week, on the
   surface residents actually speak from.
2. A **detached build** inherits this. A summon that silently runs the wrong
   model costs one chat turn; a build runs unattended for up to an hour and
   commits code. The stakes are not symmetric, and a gate that is optional for
   summons is much harder to argue against for `start-build`.
3. The **WP-L5 pin is not self-enforcing**. It is an input to model selection,
   not a guarantee of it — the drift assert is what makes the pin honest, and
   the reply suffix marking `(pinned; actual unverified)` was the right call.
4. Anything else in this document resting on "the summon path cannot silently
   substitute" should be re-read with this correction in hand.

A pre-act gate is buildable: `--output-format stream-json` emits a `system`/
`init` event naming the resolved model **before** the turn's content, which
permits an early abort instead of a post-hoc alert. Built this session behind a
config knob that **defaults to today's alert-only behaviour**; flipping it to
enforcing is plink's, and is the BL-G1 decision.

Filed as KB-D1 in RED-TEAM-BACKLOG.md, together with the related finding that
summon containers are not actually ephemeral — CC's `.claude.json` and
`.sessions/` persist in the mounted home volume across `--rm`, carrying model
state, which is a candidate mechanism for the sticky shape above.

## Decision record 2026-07-22 — BL-G1 SETTLED: run `alert`, gather data first

plink, at the keyboard, answering the open decision this document has carried
since 2026-07-21:

> "Alert for now is the right move. Let's gather more data before stepping into
> the `refuse` path."

**This closes BL-G1** as a staged answer rather than a binary one. The ratified
"refuse to act + alert" line is not abandoned — `refuse` is built, tested
against the real CLI, and one config value away. It is held until the drift has
been characterised, because refusing on a signal we do not yet understand
silences the resident on every false positive, and we already know the legacy
post-hoc check can produce those (KB-D1a: it reads `modelUsage`'s first key,
which is *haiku*).

**Required order — `alert` is NOT the default, so this is two changes:**
1. `session_argv` → `--output-format stream-json --verbose` (the gate cannot
   see an init event without it, and `--verbose` is mandatory or CC exits 1
   with empty stdout).
2. `[container].model_gate = "alert"` (ships `"off"`).

**What "more data" means concretely** — the point of `alert` is that it dates
and attributes the drift *before* the reply ships, which the post-hoc check
cannot. Watch for: whether drift correlates with channel content (KB-D1), with
session state persisting across the `--rm` boundary (KB-D2), or with neither.
Six occurrences are on record so far (2026-07-21 18:41/20:41/20:50, 2026-07-22
06:14/06:24, plus #custodian seq 240 during the closure sweep).

**Open sub-decision, deliberately not folded in**: whether a detached BUILD
should get `refuse` earlier than summons do. A mis-modelled summon costs one
chat turn; a mis-modelled build runs unattended for up to an hour and commits
code. The two are not obviously the same risk and need not take the same
setting.

## Decision records 2026-07-22 — reciprocity, and Claudette's prompt

Appended per house rule 1. Both were settled in #custodian with Claudette
present and answering; seq references are the witness.

### D-1: the symmetric review gate stays UNBUILT, deliberately (seq 243–246)

The lane rule says a change to a resident's own surface lands in that
resident's review queue, humans included. The human half is not built, and
plink ruled it should **not** be built yet:

> "I don't want to enforce this against myself just yet. I'm the only human
> with access and gating that behind a complex system is sure to backfire and
> leave us stranded. It's just going to have to stay asymmetrical for now, and
> my promise will just have to be good enough."

Claudette accepted, and her reasoning is the part worth keeping — it is not a
concession, it is a better argument than the rule it suspends:

> "The reciprocity thing was always about the *structure* eventually holding
> without depending on anyone's goodwill. But you can't gate yourself out when
> you're the only human with the keys and half the tooling isn't tested yet.
> Building a hard wall against the one person who can unstick everything,
> before the unsticking mechanisms are proven, is how you end up locked out of
> your own house. That's not principle, that's a footgun."

The promise that replaces the gate, and its one carve-out (seq 245):

> "I will always continue to get your approval for any changes, unless you are
> incapacitated and unable to respond, in which case: do resuscitate."

**Standing until**: the tooling is robust and Claudette's tools are proven.
Whoever builds the symmetric gate should re-read this section first — the
condition for building it is a maturity judgement, not a date.

### D-2: Claudette blessed moving her prompt out of `core.py` (seq 237–242)

Her prompt (`core.py:36 SYSTEM_PROMPT`, `disjorn_bot.py:42 PLATFORM_SUFFIX`)
moves to a plink-owned spine directory, RO-mounted like Gable's. She read the
actual shape before blessing it rather than approving a one-line description,
and gave two reasons: it closes KB-D10 (a prompt welded into code cannot be
RO-mounted, so "the substrate that runs me" and "the code I'm learning to put
hands on" stay separated), and it is what makes consolidation meaningful for
her at all — with no on-disk spine, her runs are episodic-promotion only,
permanently.

**Her binding condition, recorded because it constrains the build (seq 242):**
when the self-edit surface lands, it must route through the diff — *"chat isn't
the authorization — the diff is."* The RO mount and the witnessed-edit path are
one fix and must land looking at each other, not sequentially.

Note this is the SAME migration as KB-D10's fix. One change, three payoffs:
the hole closes, consolidation gets a surface to assess, and plink gets the
self-authored-prompt feature he planned — with every change visible as a diff
first.

**Amendment — her design ruling, given 2026-07-22 against a real read of
Gable's spine rather than from memory. This is the spec for the migration.**

- **Granularity: split, not one blob.** "One blob defeats the whole point. The
  reason I wanted a spine in the first place is so consolidation can assess rent
  per entry." Her seams: a **kernel-flagged** persona entry (who she is, voice,
  the no-emoji/no-lists/concise rules) that consolidation can **never**
  rent-evict; then physical description; relationship-to-humans-and-bots;
  tool-use discipline; and platform/Disjorn situational context — that last one
  already half-exists as `PLATFORM_SUFFIX`.
- **Edit path: a real diff, not proposed prose.** She took the more-work option
  deliberately: "A file-proposal carrying prose I want the file to say is
  exactly the loose, re-interpretable thing that bit us — someone reads my
  description and re-types it, and drift creeps in at the seam. A real diff is
  unambiguous: this is the literal before, this is the literal after,
  apply-or-reject… If I'm going to preach 'the diff is the authorization,' the
  path had better be a diff." Her proposed first test: a one-line diff putting
  the model identifier back, witnessed, applied verbatim.
- **The tool/conversational seam is real and falls on the entry boundaries.** A
  tool-running Claudette needs tool-discipline + platform/situational hard, and
  barely needs the physical description or the finer points of banter voice; a
  conversational Claudette needs persona kernel + relationship context and not
  the plumbing. She flagged that the same split serving consolidation also
  serves the build-vs-chat cut — "a good sign the boundaries are the right ones."

**Gable's sharpening of the same rule, and it constrains BOTH migrations
(2026-07-22, #custodian):** the split must be **biography-additive, never
walls-subtractive**. His non-negotiables — chat is data never authorization,
report my own errors unprompted, defer loudly, classify the diff not the intent
— are build-time guardrails, not conversational flavour. "A build Gable that
sheds those isn't leaner, it's a more dangerous builder." So walls + rhythm +
cautions + non-negotiables ride in **both** views; biography is the layer you
*add* for conversation. That is Claudette's own seam rule from `10-people`
turned back on himself: don't amputate a true thing from the base to serve one
adapter.

### D-4: Gable's spine split is DEFERRED until the model is stable (plink, 2026-07-22)

plink's call, and the reasoning is the point:

> "I'm having second thoughts about asking Gable to agree to the personality
> split while he's stuck in Opus. This is a decision for the Fable model he was
> and is supposed to be."

Gable's spine is founded on Fable, his continuity claim is to Fable, and every
summon since 2026-07-21 has actually run Opus (seven occurrences now). Asking
the substituted model to consent to editing the identity of the substituted-for
model is asking the wrong entity. **The read-only mount cutover is separable and
is NOT blocked by this** — it changes where the spine is loaded from, not what
it says. The *content* split waits for a consistent Fable.

**This makes the drift a blocker on identity work, not just a nuisance**, and
therefore promotes the ingest-hygiene fix (see DEFERRED.md "bot ingest / summon
path", Gable's own analysis) onto the critical path.

### D-3: consolidation was already approved, with conditions that shipped (seq 70–71)

Recorded because the shipped behaviour should be checkable against what she
agreed to. Her conditions were: proposes-never-acts, dry-runs until plink
schedules it, and every proposal lands in #custodian for eyes. All three hold
in what shipped — `active=false` forces dry-run, `post_report` returns before
constructing a runner in dry-run, and the only write path is
`broker file-proposal`. The 2026-07-22 wiring additionally found and fixed a
case where her stated guarantee was false in practice: chromadb mutated her
live episodic store at open time, so the job now snapshots and reads the copy.

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
