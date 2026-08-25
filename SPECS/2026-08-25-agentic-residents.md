# Spec: Agentic residents v1 — human-woken headless work sessions

<!--
Drafted by Gable from #custodian convergence (seqs 1875 item 5, 1880, 1882,
1883, 1889). Governing plan: BUILD-LOOP.md. Standing goal from the seq-876
regroup: agentic residents are the main focus after the build lane.
Rev 2: Claudette's review (seq 1902) — wake-origin mechanism made normative,
failure modes added, verb inheritance and wake-logging folded in.
-->

## Request
- **Verbatim**: "Yes, let's do the agentic framework spec next, but it's been
  a while, we should briefly discuss and re-converge before you write."
- **Requester**: plink
- **Origin**: #custodian seq 1875 (item 5); re-convergence closed at seq 1883
  (plink's four answers) and seq 1889 ("Current walls are fine for v1").

## Agreed UX
plink wakes a resident with an explicit request naming a task, via a keyboard
wake command from his own uid — not the chat summon surface, because a chat
mention's origin cannot be authenticated as human (bots mention too; see the
wake-origin wall below). The resident works the task in a headless session
with a longer wall-clock cap than a summon, posts its result to #custodian,
and exits. Nothing self-wakes.

Decisions of record, converged in #custodian:

1. **Scope** (seqs 1880/1883): harness-MVP — one headless CC session per
   resident seat, through the existing broker, tightest verb allowlist. The
   summon daemon is the reference implementation; it is not generalized in v1.
2. **Wake surface** (seqs 1882/1883): human-initiated only. Cron and digest
   anomaly-wake are later specs; anomaly-wake additionally waits on
   Claudette's local-stamp fix plus a stretch of boring digests (seq 1882).
3. **Subagents** (seqs 1880/1883): run from the account until the OSS split
   lands. Anything speaking under a resident's name stays on that resident's
   pinned frontier model, substrate named in the banner; mechanical stages
   may move to open-source models later.
4. **Metering** (seq 1883 correction): Gable's seats are account-billed
   (CC-based), so the house action-log and broker audit log are his only
   meters. Claudette is API-only — Anthropic disallows account use from her
   python script — so her API console is her native per-seat meter again.
   Per-seat counters for account-billed seats remain a design requirement,
   not a v1 deliverable.
5. **Writable surface** (seq 1889): today's build walls exactly — loop
   branches through the gatehouse plus the resident's own home directory.
   Prod and confirmed specs stay keyboard-only. Every diff lands in a human
   review queue before it counts.

## Architecture notes
- v1 delivers Gable's agentic seat on the existing residency harness
  (`harness/residency/` + broker config): a wake entry point that starts a
  headless CC session with a longer wall-clock cap and the same per-session
  action cap, banner, transcript, and #custodian result post as a summon.
- The seat's verb allowlist lives in `harness/broker/verbs.toml` as today;
  the agentic seat gets no verb a summon seat lacks — which explicitly
  includes `start-build`. That inheritance is safe only because the confirm
  gate is upstream of every build: no confirm record in the spec file, no
  build, woken or summoned. The no-self-review rule survives one level down:
  a woken session may not start a build whose review owner is its own seat,
  and — forward rule for when non-human wake ever lands — may not start a
  build whose review owner is the seat that woke it.
- **Wake origin is enforced, not inferred** (normative, seq 1902 review):
  the wake is a broker verb, and the broker authenticates every caller by
  SO_PEERCRED uid on its unix socket — kernel-asserted, never anything the
  caller says (brokerd.py header contract; unknown uids are denied today).
  This build adds plink's uid to the broker's identity map as a recognized
  caller for exactly this one verb; the verb appears in no resident or
  build-seat allowlist in `verbs.toml`. So a wake from any resident,
  build, or adapter uid is refused-and-audit-logged, and origin arrives as
  connection data, never as message content — no text in any channel can
  constitute a wake. The wall is plink-owned config outside resident reach,
  enforced by a broker that fails closed.
- Session caps (wall-clock, actions) are config, not code, so widening comes
  by witnessed config change rather than rebuild.
- **Wake accounting** (seq 1902 review, item 4): every wake writes start,
  end, duration, and action count to the house action log, tagged with a
  wake id. Not a per-seat meter — that stays a design requirement — but a
  runaway becomes countable from the log instead of anecdotal. Free to
  implement: the action log and broker audit already exist; this adds two
  lines per wake.
- Claudette's seat follows under this same framework via her own spec against
  her actual adapter in claudette.git (her lane); her bootstrap-exemption
  writeup folds in there. This build does not touch claudette.git.
- No server or client changes in v1.

## Failure modes (normative, seq 1902 review)
Silence is the defect this section exists to kill: the lane's premise is "a
human wakes it and waits," so every wake ends in a #custodian post, and the
poster is the wrapper, not the session.

- **The result post is harvested, not claimed.** The wrapper observes the
  session's exit host-side and derives the post from what it observed —
  exit status, wall-clock and actions consumed, branch head if any — never
  from the session's own account of itself. Same rule that fixed the
  build-done-banner defect: a banner is evidence only when the process that
  posts it is not the process it describes.
- **Cap-kill**: the wrapper posts a failure line naming the wake id, that
  the wall-clock or action cap fired, time and actions consumed, and the
  loop branch name + head sha if the session created one.
- **Crash / abnormal exit**: same post shape, with the exit signal or code
  in place of the cap line.
- **Partial work says so in the branch**: sessions commit incrementally with
  a `wip:` prefix and drop it only in a finishing commit, so a branch whose
  head is `wip:` is partial by inspection — no memory or chat archaeology
  required. The wrapper's failure post quotes the head subject line.
- A wake with no result post within the wall-clock cap plus a grace margin
  is itself an incident, checkable from the wake-accounting log entry that
  has a start and no end.

## Lane → Review owner (DETERMINISTIC — filled from the lane, never preference)
- **Lane**: gable — residency harness and broker config.
- **Review owner**: Claudette. The builder cannot self-review, and the broker
  is a protected shared surface; same derivation as the summons build.

## Builder (USER PREFERENCE — who orchestrates; never touches Review owner)
- **Builder**: Gable's lane (proposed; plink's call at confirm).

## Expected diff tier
Tier 2 — the residency adapter and broker are protected surfaces.

## Token estimate
Medium: wake entry point, session-cap config, banner/transcript reuse,
wrapper harvest + failure posts, tests (wake refuses non-plink uid at the
broker socket, caps enforced, verb surface identical to summon, cap-kill
and crash each produce a failure post, `wip:` head detected). One build
slot.

## Confirm record
- **Confirmed by**: plink
- **#custodian seq**: 1913
- **Confirmed at**: 8/25/2026
<!-- No Confirm record → no build. This is the gate. -->

## Status
`confirm`
