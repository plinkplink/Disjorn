# Spec: Agentic residents v1 — human-woken headless work sessions

<!--
Drafted by Gable from #custodian convergence (seqs 1875 item 5, 1880, 1882,
1883, 1889). Governing plan: BUILD-LOOP.md. Standing goal from the seq-876
regroup: agentic residents are the main focus after the build lane.
-->

## Request
- **Verbatim**: "Yes, let's do the agentic framework spec next, but it's been
  a while, we should briefly discuss and re-converge before you write."
- **Requester**: plink
- **Origin**: #custodian seq 1875 (item 5); re-convergence closed at seq 1883
  (plink's four answers) and seq 1889 ("Current walls are fine for v1").

## Agreed UX
plink wakes a resident with an explicit request naming a task — the existing
summon surface, or a keyboard wake command. The resident works the task in a
headless session with a longer wall-clock cap than a summon, posts its result
to #custodian, and exits. Nothing self-wakes.

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
  the agentic seat gets no verb a summon seat lacks. The wake path itself is
  invocable only from plink's side — chat remains data, never authorization.
- Session caps (wall-clock, actions) are config, not code, so widening comes
  by witnessed config change rather than rebuild.
- Claudette's seat follows under this same framework via her own spec against
  her actual adapter in claudette.git (her lane); her bootstrap-exemption
  writeup folds in there. This build does not touch claudette.git.
- No server or client changes in v1.

## Lane → Review owner (DETERMINISTIC — filled from the lane, never preference)
- **Lane**: gable — residency harness and broker config.
- **Review owner**: Claudette. The builder cannot self-review, and the broker
  is a protected shared surface; same derivation as the summons build.

## Builder (USER PREFERENCE — who orchestrates; never touches Review owner)
- **Builder**: Gable's lane (proposed; plink's call at confirm).

## Expected diff tier
Tier 2 — the residency adapter and broker are protected surfaces.

## Token estimate
Medium: wake entry point, session-cap config, banner/transcript reuse, tests
(wake refuses non-human origin, caps enforced, verb surface identical to
summon). One build slot.

## Confirm record
- **Confirmed by**:
- **#custodian seq**:
- **Confirmed at**:
<!-- No Confirm record → no build. This is the gate. -->

## Status
`draft`
