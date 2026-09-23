# Spec: activate #custodian mention-only summons (both conditions of 08-24)

## Request
- **Verbatim**: "see if we can also get `2026-08-24-custodian-mention-summons` deployed."
- **Requester**: plink
- **Origin**: keyboard session 2026-09-23, after #custodian 2867–2872

## Agreed UX
Everything in `SPECS/2026-08-24-custodian-mention-summons.md` § Agreed UX and
§ Loop guards, unchanged. This spec builds the two things its Activation
section held back, then switches it on.

After activation, in #custodian only:
- A resident wakes on an explicit, server-attested `@name`. A bare name is
  inert, and so is a broker post that says `res-claudette`, which is what woke
  her three times at 2852–2854.
- A bot's `@name` summons a resident only if the bot is on that seat's
  allowlist. It also passes the broker's hop wall: depth 1 by default, and up
  to 8 hops on a work item in Review.
- The daily digest still wakes Claudette. It does not wake Gable.
- Refusals are posted in-channel, attributed.

Other channels, and her Discord runner, are unchanged.

## Architecture notes
**Condition 1: the broker verifies an unpark** (custodian lane).
- `summon-hop unpark` requires `seq`. The broker reads that #custodian message
  itself, through `_build_message`: not deleted, not private, and
  `author_type == user`. It then requires the message to cite the work item,
  matched with the same bounded pattern the adapters use. Only then is the
  counter reset. The adapter's word is no longer enough: it authenticates as
  the same res-* uid as the model it gates.
- Verb-level `tool = false` in `verb_surface.toml`: `tool_schemas` skips the
  verb, so `summon_hop` never enters a bot seat's generated tools, even when
  that seat's section grants it. The shell seat's CLI keeps it, because
  Gable's adapter calls it through the socket, not the CLI.

**Condition 2: her adapter** (claudette.git `disjorn_bot.py`, custodian lane).
- Classification mirrors `harness/residency/detector.py`. All flags come from
  plink-owned `/srv/disjorn-resident-config/res-claudette/env` (read-only to
  her) and default OFF: `CUSTODIAN_MENTION_ONLY`, `BOT_SUMMON`, `PEER_BOTS`,
  `WAKE_ON_DIGEST`, `DIGEST_AUTHOR_IDS`, `DIGEST_PATTERN`.
- In #custodian with mention-only on:
  - A user's message wakes her iff the server attached context AND the content
    has `@claudette` as a whole word.
  - A bot's message wakes her only in three cases:
    - The digest: an author in `DIGEST_AUTHOR_IDS` whose content matches
      `DIGEST_PATTERN`, with `WAKE_ON_DIGEST` on.
    - A bot chain: `BOT_SUMMON` on, context present, `@claudette`, the author
      in `PEER_BOTS`, and the broker's `summon-hop spend` allowing it.
    - Everything else is inert. Trigger phrases are off here.
- A bot-chain reply at depth 1 has every `@name` demoted to the bare name
  (guard 1); a reply on a granted chain keeps them. Refusals (not on the
  allowlist, or a hop refused) post in-channel with the broker's line.
- Her context block gets one line naming the trigger: mode, summoner, author
  type, depth, and the work item if any (her #1803 condition 2).
- Unparks are reported by Gable's adapter, which already does it for every
  human post citing a work item. The broker's check (condition 1) is the same
  whichever adapter reports, so hers does not duplicate it.

**Activation** (plink-owned config, after both builds merge and deploy):
1. `verbs.toml`: `summon-hop = true` for `[res-claudette]` and `[res-gable]`.
2. `broker.toml`: the `[summon_hops]` block, with `state_path`, `hop_cap = 8`
   and `daily_hop_cap = 24`.
3. Gable's `summon.toml [summon]`: `bot_summon = true`, `peer_bots`.
4. Her env: `CUSTODIAN_MENTION_ONLY=1`, `BOT_SUMMON=1`, `PEER_BOTS`,
   `WAKE_ON_DIGEST=1`, `DIGEST_AUTHOR_IDS=3`.
5. Regenerate her `broker_tools.py`: `summon_hop` must stay out (condition 1).
   Then restart the broker, `gable-summon` and `resident-cc`.
6. Live checks:
   - a bare name wakes nobody;
   - a human `@` wakes the named resident;
   - a bot `@` inside a Review work item chains, and past 8 it parks with the
     fixed refusal;
   - a human post citing the item unparks it;
   - a bot post citing the item does NOT unpark it;
   - the digest wakes Claudette only.

**DECIDE (plink) before step 3/4: who is on the allowlists.** The
recommendation is each other plus BuildGable on both. BuildGable on Gable's
list ends "a bot post cannot reach Gable": the keyboard could `@Gable` for a
review, counted against the same wall. The cost is that the keyboard's
summons of either resident become bot hops, spending the hop budget on the
cited work item.

## Lane → Review owner (DETERMINISTIC — filled from the lane, never preference)
- **Lane**: custodian (broker verb and generator; her adapter).
- **Review owner**: Claudette.

## Builder (USER PREFERENCE — who orchestrates; never touches Review owner)
- **Builder**: BuildGable, the keyboard seat.

## Cross-lane split
- **Applies**: yes, for config only. Gable's `summon.toml` flags are plink-owned
  config on Gable's seat: pre-notice to Gable, and plink sets them. No gable-lane
  code changes.
- **Surfaces by lane**:
  - custodian: `harness/broker/brokerd.py`, `gen_verb_surface.py`,
    `verb_surface.toml`, tests; claudette.git `disjorn_bot.py`, tests →
    review owner Claudette
  - gable: live `summon.toml` flags only → pre-notice to Gable, plink decides
- **Split agreed in #custodian**: at the confirm seq.

## Expected diff tier
Tier 2 — the broker and a resident adapter's wake path.

## Token estimate
One keyboard session.

## Confirm record
- **Confirmed by**: <plink>
- **#custodian seq**: <seq>
- **Confirmed at**: <timestamp>

## Status
`draft`
