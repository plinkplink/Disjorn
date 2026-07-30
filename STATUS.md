# STATUS — the one page that says where the project is

**Updated 2026-07-26 (keyboard poll).** If you read one file to know the state
of Disjorn, it is this one. Everything else is depth.

> **Why this file exists.** On 2026-07-26 the house discovered it had built,
> approved, and installed an entire memory-consolidation system four days
> earlier, and then spent a night in #custodian redesigning it from scratch
> because nobody knew it was there. The same week, one stale sentence ("she has
> no on-disk spine") survived in three separate config files after it stopped
> being true, and a resident filed the same grievance three nights running from
> a partial read of one JSON file. None of that was carelessness. It was a
> project whose state lived in fifteen documents, two telemetry streams, and
> four people's heads. **This file is the fix. Keep it honest or delete it —
> a status board that lies is worse than none.**

## Rules for this file

1. **One line per item, with an owner and a state.** If it needs a paragraph,
   the paragraph goes in the depth doc and this file links to it.
2. **Update it in the same change that changes reality.** A commit that flips a
   verb, lands a build, or closes a blocker edits this file too. This is the
   house's existing "record decisions where they happened" rule, applied to
   status instead of decisions.
3. **Owners are named, and "the house" is not an owner.** Every open item
   belongs to plink, Claudette, Gable, or the build seat.
4. **Nothing is DONE here until it is verified live**, not merely built. The
   consolidation package was "done" for four days while its timer was off.

---

## Right now: what is live

| System | State | Notes |
|---|---|---|
| Disjorn server | **LIVE** | one uvicorn, SQLite WAL, port 8399 behind the tailnet |
| Claudette | **LIVE**, standing resident | own container, 8 broker verbs on |
| Gable | **LIVE**, summon-mostly | Max/OAuth, Fable pin, RO spine mount |
| Resident walls (uid/nftables/mount) | **LIVE, verified** | egress wall + 0700 homes + RO mirror all probed 07-20 |
| Broker + kill switches | **LIVE** | every ACTION verb still OFF; read-only set on |
| Consolidation ("dreaming") | **LIVE as of 2026-07-26** | timer on for Claudette; promotion-only |
| Metrics dashboard | **LIVE, partly blind** | see "the ledgers" below |
| Off-site backup | **DOES NOT EXIST** | both residents rank this #1. A fire is fatal today. |

## The queue, in the order the house agreed

Claudette set this order (#custodian seq 417/421) and Gable seconded it:

1. **Off-site backup** — owner **plink**. Nothing else survives a fire. Encrypted, and *test the restore*.
2. **Telemetry join** — owner **build seat**, needs a resident decision (below).
3. **The `additionalDirectories` one-liner** — ✅ **DONE 2026-07-26**, Gable can now read `/opt/disjorn`.
4. **Confirm** — the approval-tiers spec lands; sunsets Claudette's bootstrap exemption.

## Waiting on plink — one word each

These are blocking other people. Each is answerable in a sentence.

| # | Question | Asked by | Since |
|---|---|---|---|
| 1 | **Bootstrap scope exemption** — grant, trim, or refuse? (worktree write, no merge authority, 60-day sunset) | Claudette, filed as a broker proposal | 07-26 |
| 2 | **`restart-disjorn` asymmetry** — Claudette has it, Gable does not. Deliberate or leftover? Gable does not want the verb; he wants the ruling. | both | 07-26 |
| 3 | **`daily_action_cap`** — the broker budget is commented out. Turning it on *starts denying verbs*, it is not cosmetic. Set 200/day or leave off? | build seat | 07-26 |
| 4 | **Claudette's Max/OAuth cutover** — gated on the KB-D6 credential-exfiltration probe. Run the probe? | build seat | 07-23 |

## Blocked, and on what

| Item | Blocked on | Owner |
|---|---|---|
| `start-build` (residents can build) | red-team gate — the open **HIGH**s are `BL-D7` (unbounded build stdout fills the disk), `H13-D7` (classifier label-shadowing), `KB-D6` (a resident can speak its own credential). `KB-D10` also reads HIGH but was fixed 07-23. | plink + build seat |
| `WP-A1` — broker gets its own uid | nothing. **Approved 07-22 and never built.** The red-team calls this "the part that matters": until it lands, every narrow sudoers rule in the repo is decorative because the broker runs as plink. | build seat |
| Rent assessment (evict/compress) | spine reads logged (§1) **+** epoch declared **+** 90d elapsed | build seat |
| Gable's `retrieval` metrics | same as above — **one build closes both** | build seat |
| **WP-H7** (spine retrieval-on-demand) | **the same gap again, third name.** Kernel assembly is live; the loop that *logs* non-kernel spine reads is unwired — `Spine(retrieval_log=…)` exists in code and nothing in prod passes it. So "WP-H7 partial", "consolidation §1 open", and "Gable's retrieval is 0" are one missing wire described in three documents by three owners. | build seat |
| Gable activating consolidation | he has no episodic store; Claudette says *add a fast layer, don't migrate* | Gable + Claudette |
| Approval-tiers spec landing | needs a keyboard session or the one-liner (now done) | Gable |
| Claudette's `tool_actions` counter | ~10 lines in her `core.py` — her area, so a proposal. **Must land the retrieval-log `origin` field in the same pass** (her condition, seq 430: provenance cannot be backfilled). | Claudette |
| Naming the third apply-then-post justification | Claudette's ask on Gable's draft: "requested in writing by the affected resident + directed by plink + single-line revert" is a real third route and needs a name in the tier spec, not an ad-hoc paragraph in someone's post | Gable |

## The ledgers are half-blind — the current live defect

Diagnosed 2026-07-26. Four symptoms, four different causes, one theme:

- `tool_actions` 0 for Claudette — her bot is not Claude Code, so the hook that writes the counter never runs.
- `retrieval` 0 for Gable — he has no metered recall at all; his spine is baked, not retrieved.
- `by_date` gap for Gable 07-23..25 — **not a bug**, genuine non-use.
- `daily_action_cap` null — deliberate default-OFF, and enabling it enforces.

**The deeper one, Claudette's:** the audit ledger records **names, not actors**.
Work run from the keyboard as `sudo -u res-<name>` is indistinguishable from
that resident acting. This is why the 07-20 probes were unattributable from the
record. *(Resolved for that instance — see below — but the defect stands.)*

Detail: [DEFERRED.md](DEFERRED.md) § "Telemetry & summon findings".

## Recently closed — so nobody redesigns these again

| What | When | Note |
|---|---|---|
| Consolidation live | 07-26 | timer on; promotion-only, 10 proposals/run |
| Eviction floor (`max_evictions=20`) | 07-26 | Claudette's ask |
| **Rent-epoch gate** | 07-26 | no-read-data now resolves **skip**, not evict. Was evict. |
| 90-day rent window | 07-26 | her ruling; separate dial from promotion heat |
| Claudette's spine row on the dashboard | 07-26 | stale config in 3 places, now 1 truth |
| Gable reads `/opt/disjorn` | 07-26 | the one-liner |
| Summon timeout 300 → 600 | 07-26 | stopgap; the real fix is a queue drop order |
| Timeout no longer reads as model drift | 07-26 | launcher kept the model it had already seen |
| Resident image refresh | 07-26 | now one script, `harness/keyboard/07-resident-image.sh` |
| **The 07-20 "unknown probes"** | 07-26 | they were the WP-H13 red-team suite, in-repo at `harness/redteam/live-probes.md`. No unknown actor; all five denied. |

## Where the depth lives — read this instead of grepping

| Question | Doc |
|---|---|
| What do I flip next, at the keyboard? | [harness/KEYBOARD-NEXT.md](harness/KEYBOARD-NEXT.md) |
| What did we defer, and why? | [DEFERRED.md](DEFERRED.md) |
| What must pass before residents get hands? | [RED-TEAM-BACKLOG.md](RED-TEAM-BACKLOG.md) |
| How is governance/tiering supposed to work? | [AGENTHOOD.md](AGENTHOOD.md) |
| How is memory supposed to work? | [MEMORY-DESIGN.md](MEMORY-DESIGN.md) |
| How was the harness built? | [HARNESS-PLAN.md](HARNESS-PLAN.md) |
| Why is the system shaped like this? | [Architecture.md](Architecture.md) |
| Who has root, and why? | [AUTHORITY-PLAN.md](AUTHORITY-PLAN.md) |
| What did the red-team actually try? | [harness/redteam/live-probes.md](harness/redteam/live-probes.md) |

**The number that made this file necessary: 13.** That is the minimum count of
documents someone had to read to know the current state of the project — 20 for
the full picture — and an audit of those 13 found **11 places where two docs
contradicted each other**, including a HIGH-severity security item that had
been fixed three days earlier and two docs disagreeing about which value was
live in a running config. Reconciled 2026-07-26. If this file ever stops being
the roll-up, that number goes straight back to 13.

## Build pipeline

**Zero specs in flight.** No spec is in `draft`, `confirmed`-but-unbuilt, or
`failed`. The two live specs (Claudette's prompt→spine, Gable's spine RO
cutover + seat split) both applied 2026-07-23. Gable's approval-tiers spec is
drafted *off-tree* in his home volume and is not in `SPECS/` yet — that is the
next thing to land, and it is the sunset condition on Claudette's exemption.

## The house's standing hazard: stale facts

Three times in one week a sentence outlived its truth and was believed:
"Claudette has no on-disk spine" (3 files), "Gable's spine has 6 entries", and
"`refresh-mirror` is wired" (it was ON while pointing at nothing). The pattern
is always the same — **a comment explaining a config outlives the condition it
described.** When you change a fact, grep the repo for the old sentence. The
config comment is not documentation; it is a claim, and claims rot.
