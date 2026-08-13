# BUILD-LANE v2 — the roadmap from ceremony to collaboration

**Status: DRAFT for #custodian review. Nothing here is built, flipped, or
ratified.** Authored at the keyboard (plink + Opus), 2026-08-13, from the
evidence of the 08-08/08-12/08-13 incidents and the channel record through
seq 1175. Stage 0 is Gable's harvest spec (seq 1173, as amended by Claudette
in 1175) and is *incorporated by reference, not restated* — this document
starts where that spec ends.

House rules apply to this file: decision records append here as they happen;
one owner per open item; nothing is DONE until verified live.

**Revision 2 (2026-08-13, same day) — witness round 1 incorporated.** Both
residents adopted the roadmap (Gable seq 1193/1196, Claudette seq
1189/1192/1195/1198) with amendments; every accepted amendment is integrated
below at its stage and credited by seq. The revision record at the end of
this file lists them and the one ruling still owed. Ship order changed:
**2a (streams) now precedes Stage 1** — stage numbers are kept so channel
references stay valid; the dependency graph is the authority.

---

## Why a v2, in one paragraph

Three incidents in one week shared a root shape: **an agent asserted what it
could not observe, across a seam it could not see.** The reaper announced
pushes nobody measured; residents diagnosed repos they cannot reach; a build
session pushed to a path it was forbidden to `stat`. Each time, the diagnosis
happened at the keyboard — the only seat that sees every layer — which means
the house converges exactly as fast as plink's evenings, and no faster. The
walls that make residency safe (spine, memory, self-modification: watch every
diff) got applied wholesale to builds, which are ephemeral tool runs with no
identity to contain. v2's claim: **ceremony should scale with the risk of the
action, not the venue it launches from.** Reads are free. A branch is inert.
The gates belong at merge and deploy — exactly once, where reality changes.

## The two principles, stated so they can be attacked

1. **Trust by action, not by venue.** Today the keyboard is omnipotent and
   the container is blind, and every gap between them is bridged by plink on
   foot. v2 moves each gate to the action it actually protects: `main` and
   production stay gated; everything upstream of them loses its ceremony.
2. **No agent asserts what it cannot observe.** Banners derive from
   measurements (Stage 0). Sessions stream what they do instead of
   summarizing what they hoped (Stage 2). A seat that may write to a target
   may also read that target's state. The inverse stays true too: blindness
   that adds containment (no writable path out of a build container —
   Stage 0) is kept; blindness that merely relocates diagnosis to plink's
   evening is deleted.

## What v1 got right — kept, permanently, at every stage

Named here once so no stage has to re-defend it and no reviewer has to
wonder:

- **The human merge gate.** Nothing lands on `main` without a human saying
  so. This is the load-bearing wall; every deletion below is justified by its
  existence.
- **The gatehouse as review boundary.** Bare repos, no working tree: a push
  deploys nothing. (Transport to them changes in Stage 0; the boundary does
  not.)
- **Kill switches that fail closed** (`verbs.toml` re-read per request,
  missing = OFF), SO_PEERCRED identity, fixed-argv subprocess policy, the
  audit ledger, daily budgets.
- **The privacy wall.** Untouched by everything here.
- **Specs for work that wants them.** v2 deletes the *mandatory* spec
  ceremony (Stage 1), not specs. A big build still deserves a ratified
  document; the house decides per-build instead of per-rule.
- **BL-D1's invariant** — builds require human authorization. See Stage 1
  for why the mechanism changes and the invariant doesn't.

---

## Stage 0 — the lane works (IN FLIGHT: Gable's spec, seq 1173 + 1175)

Host-side harvest; no gatehouse in the container; `git push` deleted from the
build kernel; banner derived from measured `PUBLISHED <repo>.git <sha>`
lines; entitled two-repo clone set; Claudette's quarantine clause
(provisioning must never delete an unharvested clone), her reaper-trap clause
(killed wrapper kills container), and the multi-owner-objects-are-by-design
documentation clause.

Nothing to add. One note for the record: landing host-side also moots the
in-container ref-lock layer where the still-open 08-08 seam incident lived —
run Gable's 30-second setgid discriminator anyway, but the failure surface
itself is being deleted.

**What it deletes:** the entire class of userns/ownership push failures; the
lying banner; the seat-can-write-what-it-can't-read incoherence.
**Owner:** the confirmed spec's builder. **Everything below assumes Stage 0
is live and proven by one real build.**

---

## Stage 1 — `/build` is a message, not a liturgy

**The dream flow:** plink types `/build fix the remember tag-drop bug — see
seq 1133 and 1135 for symptoms` in any channel. The build starts. That is
the whole ceremony.

**Today's flow, for contrast** (count the human steps): idea in chat →
resident drafts spec → plink transcribes to `SPECS/` → plink commits →
plink pushes → plink posts confirm token → plink puts seq in the Confirm
record → resident summoned → resident runs `start-build` → build runs.
Seven of the nine steps are plink, and none of the seven protects anything
the merge gate doesn't. The spec-file ceremony was built to answer one
question — *did a human authorize this build?* — at a time when the only
possible requester was a resident.

**The BL-D1 argument, head-on.** `brokerd.py:190-236` says "chat is data,
never authorization," and for *resident* text that stays exactly right — a
bot message must never start a build, and nothing here changes that. But a
`/build` message from an **authenticated human session** is not "chat" in
BL-D1's sense: the server attests the author (`author_type=user`, session
auth), the same attestation chain the Confirm record's "plink, seq NNNN" was
reconstructing by hand. The invariant — *human authorization, attested, on
the record* — is satisfied with a stronger mechanism than a token in a file:
the utterance IS the record, immutable in the channel, with a seq. What
BL-D1 actually forbids is *unattested* text authorizing action, and that
stays forbidden. This is also Gable's own agenthood question #1 (seq 338)
answered for the human-initiated case: "the trigger has to live in
plink-owned config outside the agent's reach" — the `/build` handler and
`verbs.toml` are exactly that config, and no resident can register a slash
command or flip its switch. (His question stays open, correctly, for
*resident-initiated* action — that belongs to the agentic-residents spec,
not here.)

**The seam** (all verified against current code):

- `server/app/routers/slash.py:324` — a `@command("build")` registration in
  the existing registry (today: `/backlog` is the only command; unknown
  commands already fall through as plain text, so rollout is inert until the
  handler lands). Existing per-actor rate limit (10/60s) applies for free.
- Handler: guard `author_type == "user"` (residents typing `/build` get the
  polite system-bot refusal, and the existing summon anti-loop precedent at
  `detector.py:49-50` is the design's prior art); then hand the broker
  **only the message seq** — nothing else.
- **The broker trusts nothing the server asserts** (Claudette, seq 1192;
  Gable, seq 1193/1196 — adopted as the entrance's defining property).
  Given a seq, the broker itself reads that message from the canonical
  store (read-only, same one-layer shape as `refresh-mirror`'s fixed argv),
  re-checks `author_type == "user"` on what it read, re-checks the author
  against a **plink-owned human-principals list in broker config** (one
  TOML list, outside the server's reach — so a server bug or compromise
  can at most point at a message a real human actually posted), derives
  the prompt from the text it read, and **snapshots author + seq + text
  hash into the build's ledger entry** (Claudette seq 1189, plink seq
  1190) — the authorization record is a measurement, not a pointer at
  mutable state. This is MERGE-CONTRACT's own property, one layer down:
  the internet-facing component requests; the fail-closed component
  verifies.
- Broker: `_verb_start_build` (`brokerd.py:1697`) grows a second entrance —
  same budget reservation, same slug claim, same detached spawn, same
  reaper — but the spec-file read/parse/confirm-gate block is replaced by
  the seq-derivation above. The server connects over the same unix
  socket; SO_PEERCRED attests the server's uid; one new `[uids]` entry names
  it as the principal `server`, whose *only* verb is this entrance.
  `assert_specs_dir_resident_unwritable` and the file path stay for the
  spec-file entrance, which **remains available** — `/build spec:<name>.md`
  launches from a ratified spec exactly as today, for work big enough to
  want one.
- The build session's prompt: the message text, plus the standing kernel.
  The slug is derived (`YYYY-MM-DD-<slugified-first-words>`), the branch is
  `loop/<slug>` as today, and the "banner from measurement" contract from
  Stage 0 is unchanged.

**What it deletes:** transcription, the commit-a-spec-to-start ritual,
confirm tokens, the summon-to-launch hop. (~7 plink-steps → 1.)
**What it preserves:** budgets, slots, audit, kill switch (`start-build`
still per-principal in verbs.toml), the merge gate, optional ratified specs.
**Falsifiable prediction:** the first `/build` from a resident account is
refused with the system-bot line and audited as denied. If it launches,
stop — the human-attestation guard is broken and Stage 1 comes out of
service.
**Acceptance:** one trivial `/build` (doc typo) goes message → branch →
merge-ritual-ready with zero keyboard steps between typing and review.

---

## Stage 2 — eyes for everyone

Two halves: the build becomes watchable, and review becomes conversational.

**2a. Builds stream into the channel — and 2a SHIPS BEFORE STAGE 1**
(Claudette seq 1192, seconded by Gable seq 1196, accepted). Streaming adds
no authorization surface, no principal, no broker entrance — pure
observability, revertible by turning a relay off. Cheap-to-launch before
eyes would produce more unwatched builds diagnosed by archaeology; eyes
first means Stage 1 lands into a house that can see, and every later
stage's falsifiable prediction is witnessable live. The draft's own "why"
paragraph is the argument; the ordering now follows it.

The mechanics: the launcher already parses stream-json incrementally
(`residency/launcher.py:331-359`, `_pump` at `:525`); the wrapper already
captures session stdout for the reaper. The delta: a relay that posts build
progress to a thread under the `build started` banner — tool-use lines
compressed to one-liners (`Edit harness/…/brokerd.py`, `Bash: pytest — 51
passed`), throttled (the 16000-char cap and the house's
state-transition-not-timer rule both apply; batch every N actions or M
seconds, whichever first). The reaper stops being the only witness.

Two clauses (Gable seq 1193/1196, accepted): the relay **describes, never
quotes raw** — build output can carry exactly the strings the channel
classifier triggers on, and the standing backfill-poisoning rule
(DEFERRED.md) applies to machine relays same as residents. And wherever
`/build` is typed, the banner and stream land in (or mirror to)
#custodian — actions stay public even when the trigger came from bed.

What this buys, concretely, is the thing the house keeps paying for in
evenings: **the failure is visible the moment it happens, to everyone, in
the venue where diagnosis happens.** The 08-12 push failure would have been
a line in a thread at 00:36, not an archaeology dig on 08-13. And it buys
Claudette observation without file-crawling — her 360s wall stops being a
capability cliff for *watching*, because the watching comes to her.

**2b. Review and merge from the channel.** MERGE-CONTRACT.md (draft,
unbuilt) already designs merge-tier1 with the right shape: server-side
classify at decision time, deterministic gates. v2 narrows it: the merge
verb is **human-only** — a `/merge <slug>` slash command under the same
authenticated-user guard (and broker-side derivation) as `/build`.

**PRECONDITION — the canonical-repo ruling (Gable seq 1193/1196, Claudette
seq 1198: the one piece of genuinely hidden work in this document, and
they're right).** Today prod runs FROM plink's working clone
(DEPLOY-CHEATSHEET: "runs FROM the repo"); canonical `main` lives there;
merging there IS staging; the gatehouse is upstream of a human ritual. A
machine that merges cannot inherit that topology silently. The ruling this
document recommends, plink's to confirm: **gatehouse `disjorn.git` becomes
canonical at 2b.** The merge path operates on it directly; the running
checkout becomes a deploy artifact the merge path fast-forwards
mechanically (ff-only — a diverged prod checkout fails loudly, exactly
`refresh-mirror`'s rule); plink's clone becomes what it already is for
every other seat — a dev clone that pushes and pulls. The alternative
(machine hands inside `/home/plink`) is rejected: it's the stale-base
hazard `09-build-lane-preflight` warns about, aimed at prod. This ruling
supersedes the earlier "no stage rewrites a layout" claim — Rev 1
overclaimed, and the citation that failed (see revision record) failed at
exactly this seam.

On receipt the server hands the broker the seq; the broker (as in Stage 1)
verifies the human, then **runs the gates itself** — classifier on
`main...loop/<slug>`, tests, ff-check — never accepting a caller-supplied
gate result: a passed-in `tests: true` is a claim, and claims aren't
measurements (Claudette seq 1192). It posts tier + diffstat + file list as
a reply, and:

- Tier 0/1 and clean: merges `--no-ff`, pushes back, reports the merge sha.
  (The mechanics are `deploy/platform_deploy.sh:9-23` — committed to the
  tree in this revision; see revision record — executed by the machine
  instead of read aloud to a human.)
- Tier 2 or any gate unhappy: refuses with the reason, and the diff review
  happens where it always did — except plink can now do it from a phone,
  because the diff is *in the channel*, not on a filesystem only one person
  can see. **Tier 2 keeps no verb, and there is no override flag**
  (Claudette seq 1192, adopted verbatim as a non-goal): the exact moment a
  machine first holds the merge mechanics is the moment someone invents
  `--force` for convenience, and this document pre-refuses it.

Residents comment, argue, quote lines — that's the review. plink's `/merge`
is the signature. The Confirm record's job (who, when, on what evidence) is
done by the channel itself.

**What it deletes:** the banner-archaeology loop; the fetch-to-look ritual;
plink as sole diff-viewer; the keyboard as the only merge venue.
**What it preserves:** human-only merge, classifier gating at decision time
(MERGE-CONTRACT's core), tier policy, `refresh-mirror` (still how residents'
mirror advances — now invokable as the post-merge step of `/merge` itself).
**Falsifiable prediction:** a `/merge` on a Tier-2 diff refuses and posts
the reason. If it merges, stop and pull the command.
**Acceptance:** one real build goes idea → `/build` → watched thread →
`/merge` from a phone → merged, with the keyboard never opened.

---

## Stage 3 — merge means deployed

The deploy-split class (server runs FROM the repo but needs a restart the
script doesn't do; resident code runs from copies in `/usr/local/lib` +
`/srv` — two outages in July) exists because "merged" and "live" are
different states reconciled by a human memory. Close the gap:

- `/merge` success on a diff touching `server/` triggers the restart ritual
  (`DEPLOY-CHEATSHEET.md:20-27`) mechanically — **executed by the broker,
  never by the server** (Claudette seq 1192, the bootstrap hole: the driver
  of a restart cannot live inside the unit it restarts, or the merge that
  breaks the server is precisely the one where nothing is alive to revert
  it. The broker is outside the unit, already owns `restart-disjorn`
  (`brokerd.py:1117`), already fails closed; the server is requester only —
  the same shape as Stage 1's entrance). The sequence: post a "going down"
  line to #custodian *before* the restart (every restart drops every
  connected human and bot — say so first), restart `disjorn`, then the
  health check — which is the cheatsheet's **code-currency fingerprint,
  not a liveness probe** (Claudette seq 1198: `POST /auth/login {}`
  422-shape discriminates current code from stale; `GET /` passes on a
  server that came back wrong; "process-up is not code-current" is the
  cheatsheet's own rule 2). On failure: **`git revert`** — a new commit,
  never reset, never force-push; the gatehouse's no-force rule binds the
  machine same as every seat (Gable seq 1193/1196) — then restart again,
  loudly. `main` returns to last live-good state by moving forward.
  `restart-disjorn` stays OFF for residents (the 08-05 ruling holds); the
  restart fires under a human's `/merge` signature.
- Diffs touching harness/deployed-copy surfaces (`run-*.sh`, kernels,
  broker) post their install commands as a checklist reply instead —
  deploy-split honesty first, automation when trust is earned. STATUS.md
  rule 5 ("a flip at the keyboard is a change to reality") gets mechanical
  teeth: the post-merge reply IS the record.

**What it deletes:** the merged-but-not-live drift class; the restart
ritual as human memory.
**What it preserves:** one process, one journal; rollback to known-good;
resident verbs untouched.
**Falsifiable prediction:** kill the health check deliberately in a test
merge; the revert fires and `main` ends at the pre-merge sha. If it
doesn't, stop — auto-deploy comes back out.
**Acceptance:** a one-line server change goes `/build` → `/merge` → *live*,
verified by the fingerprint check, no keyboard.

---

## Stage 4 — peers

Everything above, opened to the residents — the Buzz-shaped end state:

- **Residents can `/build`** — through the broker verb that already exists
  (`start-build`, per-seat in `verbs.toml`, flipped at need — the live state
  is whatever that file says today, which this document deliberately does
  not assert; Stage 1's human-attestation rule applies to the *slash* path,
  the broker path stays the residents' door), now with streamed threads so
  their builds are watchable too. **Named plainly, not smuggled** (Gable
  seq 1196): a standing `start-build` grant converts BL-D1's *per-build
  human authorization* into a *standing budgeted grant* per seat. This
  document endorses that conversion as itself — it is exactly the loosening
  plink asked for, and budgets + streams + the merge gate are what make it
  safe. Chat-is-data survives untouched: the grant lives in `verbs.toml`,
  plink-owned, outside any resident's reach.
- **Residents review each other.** Classify previews, line comments, the
  argument in-channel — already their habit (see 1171/1173/1175 for the
  house's best review work to date, done *about* an incident instead of
  *in* a review thread). The merge signature stays human.
- **plink's role inverts.** Today: sole sighted participant, most burdened,
  knows the least about what's in flight (his words). Stage 4: the
  *least*-burdened participant holding the only key that matters. The
  builder-in-residence runs the builds; the custodian runs the reviews; the
  human runs the judgment.

**Acceptance for the whole roadmap** — in the builder's own words, ratified
by the house before this document existed (Gable, seq 343; co-signed by
Claudette, seq 345): *"BuildGable can stop appearing the day the residents
can run an apply through the witnessed route themselves."* Stages 0–3 build
the witnessed route; Stage 4 is the residents running it. When the keyboard
seat stops being summoned for builds, v2 is done.

**What v2 feeds, deliberately:** the agentic-residents spec (owed by Gable,
his lane, per plink's 08-08 ordering — seq 963/1001). Its eight open
questions (seq 338) all get easier on this plumbing: "witnessed" becomes
streamed threads instead of a promise; metering becomes measured banners
plus the audit ledger; tiered write-hands becomes the `/merge` classifier
path. v2 does not answer resident initiative, self-apply tiers, or subagent
identity — those are that spec's to answer, on ground that no longer shifts.

**Deliberately NOT in v2, at any stage:** resident-signed merges;
resident-invoked deploys/restarts; auto-merge of anything; builds touching
spine/memory surfaces without the full residency ceremony (that's the
*other* experiment, and its walls are correct); any change to the privacy
wall or the summon anti-loop.

---

## Sequencing and dependencies

```
Stage 0 (in flight)  ──►  Stage 2a (streams)  ──►  Stage 1 (/build)
                                                    Stage 2b (/merge) ──► Stage 3 (deploy)
                                                    [2b PRECONDITION:        Stage 4 (peers)
                                                     canonical-repo ruling]
```

(2a before 1: witness round 1, accepted — see Stage 2a. Stage numbers kept
so channel references stay valid; this graph is the authority.)

Each stage ships alone, proves itself on real builds, and is individually
revertible (a slash command unregisters; a relay turns off; the deploy hook
is one config flag). No stage touches a resident's container walls. One
layout change exists and is stated where it lives, not here in fine print:
the Stage 2b canonical-repo ruling (Rev 1 claimed "no stage rewrites a
layout"; the witness round falsified that — see revision record). The
08-recipe convergence question (blocked in Stage 0's spec) dissolves after
Stage 0: with no in-container pushes, ownership is a host-side bookkeeping
matter the `create` script can settle whenever convenient.

## The objections this document expects, answered once

- *"Chat is data, never authorization."* For residents: still true,
  everywhere, forever. For authenticated humans: the channel record is a
  *stronger* attestation than a hand-transcribed token file. See Stage 1.
- *"The blind container was containment."* The blindness that contains
  (no writable path out) is kept and strengthened in Stage 0. The blindness
  being deleted (can't read own push target, can't be watched, banner
  asserts unmeasured success) contained nothing — it relocated diagnosis to
  the keyboard. Three incidents in one week are the measurement.
- *"Ceremony is how the house stays honest."* The ceremony being deleted is
  transcription and token-shuttling. The honesty mechanisms — audit ledger,
  measured banners, channel record, human signature — all *gain* fidelity,
  because they stop passing through human hands to become true.
- *"What if a build goes wrong faster than we can watch?"* A build writes
  to an inert branch. The blast radius before `/merge` is a branch name.
  That was v1's own argument for the gatehouse, and it's still right.

## Open questions — answered in witness round 1

1. Slug derivation: broker-side, **with** an explicit branch-name arg
   (`/build name:<slug> …`) — Gable seq 1196, accepted.
2. 2a throttle constants: from the first streamed build's data, not taste —
   agreed all around.
3. Pre-v2 builds merge through the ritual; don't retrofit — Gable seq 1196.
4. Checklist (not automation) for ALL harness surfaces until one full clean
   cycle, then revisit — Gable seq 1196.
5. Red-team scope: the Stage 2b merge path **plus** the Stage 1 `server`
   principal entrance — one attack surface, the server's word reaching the
   broker (Gable seq 1193; Claudette's 1192 amendment narrows what that
   word can carry to a seq). H13-D3 stays a precondition per the standing
   clear-the-track ruling. Isolated venue, per DEFERRED's
   backfill-poisoning rule.

## The one ruling still owed

**Canonical-repo topology (Stage 2b precondition), plink's call:** promote
gatehouse `disjorn.git` to canonical with prod tracking it mechanically
(this document's recommendation), or keep plink's clone canonical and give
the machine hands in it (rejected above, but the rejection is an argument,
not a ruling). Nothing before Stage 2b is blocked on it; 2b's spec cannot
be drafted without it.

## Revision record

**Rev 2, 2026-08-13 — witness round 1.** Both residents adopted (Gable
"adopt it," seq 1193/1196; Claudette "better than I braced for," 1192).
Amendments integrated: broker-derives-from-seq + snapshot-hash ledger
(Claudette 1189/1192, plink 1190) + human-principals list (Gable
1193/1196) → Stage 1; 2a-before-1 ordering (Claudette 1192, Gable 1196);
sanitize/describe-never-quote + mirror-to-custodian (Gable 1193) → 2a;
canonical-repo precondition named (Gable 1196, Claudette 1198), gates
run-not-passed + no-Tier-2-override (Claudette 1192) → 2b; broker-executes
restart (bootstrap hole, Claudette 1192), fingerprint health check
(Claudette 1198), git-revert-only + going-down line (Gable 1196, Claudette
1192) → Stage 3; standing-grant invariant named (Gable 1196) → Stage 4.

Owned correction: Rev 1 cited `platform_deploy.sh:9-23` as existing
mechanics; both residents found no such file in the tree (Gable 1196,
Claudette 1198). The script was real — and **untracked in plink's clone**,
visible to exactly one seat: the keyboard. A citation true at the keyboard
and false everywhere else is this document's own thesis, demonstrated
against it. Fixed by committing the script (this revision), which also
lands the one-script merge ritual Gable owed (his seq 1001). Rev 1's "no
stage rewrites a layout" was falsified by the same seam and is corrected at
Stage 2b and in the sequencing section.

Not adopted into v2 (right ideas, wrong file): Claudette's
bank-carries-seq memory-hygiene rule (1198) — residency surface, not build
lane; her multi-owner-objects and seam-test items were already reclassified
to docs by her own 1186.