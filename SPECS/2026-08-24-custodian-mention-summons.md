# Spec: #custodian mention-only summons + bot-to-bot @mention summons

## Request
- **Verbatim**: "make all bots only trigger on @mentions … so humans don't need
  to be careful not to invoke by accidentally typing someone's name in the
  middle of their message. Applies to #custodian only." / "he should be
  summon-able by other bots with an @mention" / "@-syntax is simpler/better
  than maintaining name-at-the-beginning patterns — we'll use that for
  #custodian summons."
- **Requester**: plink
- **Origin**: #custodian seq 1802, 1807

## Agreed UX
- In #custodian only: a bot wakes ONLY on a server-attested @mention. A bare
  name anywhere in a message is inert data. Other channels unchanged.
- Bots may @mention other bots to summon them, subject to the guards below.
- A refused summon (depth, hop cap, budget) posts its refusal in-channel,
  attributed — never silence (Gable #1804 ruling 2).
- The summoned bot can read the trigger mode (mention / channel / pattern /
  bot-chain, plus chain depth) from inside its summon context — no inferring
  mode from member count (Claudette #1803 cond. 2, #1806).
- Broker daily digests: wake Claudette only, via config flag per bot; nothing
  else unaddressed wakes anyone (her #1803 cond. 3 carve-out; plink #1807 left
  the call to her — her confirm on this line rules it). Gable's flag stays off:
  summon-mostly by design; plink summons him when a digest smells wrong.

## Loop guards (the design center)
1. **Default depth-1.** A bot-triggered summon's reply does not re-trigger any
   bot. Hard, adapter-enforced (plink agreed #1807).
2. **Work-loop provision** (review → revision → fix, plink #1807): a chain may
   continue past depth 1 iff the summoning message cites a live spec slug or
   board card in Review. The broker — one shared arbiter, plink's #1625
   third-party option — keeps a hop counter per work item. Cap: 8 bot-to-bot
   hops ≈ 4 review/fix round-trips, matching the 08-21 ~4-round churn ceiling.
   At cap the chain **parks for a human**: bot-to-bot hops on that work item
   refuse until a human posts in #custodian citing the work item (a human
   summon of either bot on it counts). That human post resets the counter to
   0/8 and the chain resumes. **The clock never resets the counter** — a
   parked chain stays parked across midnight until a human has looked
   (Claudette #1811). Second ceiling on top: 24 bot-to-bot hops per work item
   per UTC day regardless of resets, so repeated human nudges can't compound
   into an all-day burn. A slug the board doesn't know = no bucket = rule 1
   applies. Chat data selects the bucket; plink-owned config owns the wall.
3. **Loud refusal, fixed format** (Claudette #1811): a cap refusal is a
   broker-attributed in-channel line naming the work item, the count, and the
   unpark condition — e.g. `summon refused: <slug> at 8/8 bot hops — parked
   until a human posts on it`. Never a silent drop: silence is
   indistinguishable from the detector eating the mention, and the summoner
   would retry.
4. **Budget + attribution.** A summon spends the summoned seat's daily budget;
   the footer names the summoner; exhaustion refusal lands in-channel.
5. Caps, allowlist, and digest-wake flags live in plink-owned config outside
   both residents' reach. Chat cannot widen them.

## Architecture notes
- Claudette detector: `harness/residency/detector.py:49` drops
  `author_type != "user"` before any check — that drop is today's entire
  anti-loop wall. Change: bot authors fall through to the mention check only,
  gated by guards 1–2; #custodian becomes mention-only (pattern matching off
  for channel 4); trigger-mode + depth injected into her context block.
- Gable adapter: `/config/summon.toml` already explicit-only
  (`trigger_on_context = true`, no patterns, no trigger channels). Gains the
  bot-mention wake path behind the same guards; footer gains summoner
  attribution.
- Broker: hop-counter table + refusal reason surfaced to adapters. Counter is
  broker-side so both adapters spend against one wall, not one each.

## Lane → Review owner (DETERMINISTIC — filled from the lane, never preference)
- **Lane**: cross-lane — see split.
- **Review owner**: per surface, below.

## Builder (USER PREFERENCE — who orchestrates; never touches Review owner)
- **Builder**: Gable's lane (plink #1812 — doubles as the review-effort read
  on Claudette; her review of the claudette-lane surfaces stays hers per the
  split below).

## Cross-lane split
- **Applies**: yes
- **Surfaces by lane**:
  - claudette: `harness/residency/detector.py`, her adapter context block →
    review owner Claudette
  - gable: summon adapter + `/config/summon.toml` template → review owner
    Gable
  - custodian (broker hop counter + config surface): review owner = whichever
    resident does not build it
- **Split agreed in #custodian**: binds at the confirm seq for this spec.

## Expected diff tier
Tier 2 — both adapters and the broker are protected surfaces.

## Token estimate
Small-to-medium: one detector branch, one adapter wake path, broker counter +
refusal plumbing, tests for guards 1–4 (incl. loop-cap refusal text,
human-post unpark, midnight non-unpark, depth-1 non-retrigger). One build
slot.

## Activation conditions
Merged OFF at f6bcf65 (review-seq 1851): "summon-hop" = false for both
resident seats in harness/broker/verbs.toml, and the [summon_hops] bucket
commented out in harness/broker/broker.toml — an absent bucket answers
"no bucket", which is guard 1, so nothing bot-to-bot can fire. plink flips
these ON only after BOTH conditions below are met (Claudette's review, seq
1851):

1. Broker-verified unpark. The broker itself verifies an unpark before
   resetting a hop counter: read the message at the cited seq and require
   author_type == user AND that it cites the work item — the same re-check
   the confirm gate does so a bot cannot self-authorize. The adapter's word
   is not sufficient: the adapter authenticates by SO_PEERCRED as the same
   res-* uid the resident session uses, so any verb granted to the adapter
   is granted to the bot it gates. Also set verb-level tool = false so
   summon-hop never enters a bot seat's generated tool schema
   (defense-in-depth; the broker-side check is the wall).

2. Claudette-half spec. This spec's architecture note pointed her
   surface at harness/residency/detector.py, which is Gable's summon
   adapter end to end — her wake path lives in claudette.git and this
   build did not touch it. Until a spec lands against her actual adapter
   (fold into plink's 08-23 per-channel bot-mode backlog row, per her seq
   1851 note): #custodian is NOT mention-only for her, and Gable-to-
   Claudette summons are uncounted, so the hop wall is one-directional.

## Confirm record
- **Confirmed by**: plink
- **#custodian seq**: 1816
- **Confirmed at**: 8/24/2026

## Status
merged
<!-- advanced from `built@loop/2026-08-24-custodian-mention-summons` by `board --mark-merged` on 2026-08-25: build merged as d3b1c06. The word `built@loop/2026-08-24-custodian-mention-summons` on a merged spec made it indistinguishable from a buildable one. -->
<!-- set by the broker on 2026-08-24 22:49Z (start-build, 2026-08-24-custodian-mention-summons): build published: disjorn.git d3b1c066550939e4458f33096545719f74965eab — on the branch for review, nothing merged. `board --mark-merged` advances this to `merged` once the merge lands. -->
<!-- set by the broker on 2026-08-24 22:25Z (start-build, 2026-08-24-custodian-mention-summons): build running as disjorn-build-2026-08-24-custodian-mention-summons.service -> loop/2026-08-24-custodian-mention-summons, launched by gable (confirmed by plink, #custodian seq 1816). Not buildable again until this line moves. -->
