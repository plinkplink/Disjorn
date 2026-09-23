# Spec: activate #custodian mention-only summons (both conditions of 08-24)

## Request
- **Verbatim**: "see if we can also get `2026-08-24-custodian-mention-summons` deployed." / amended after the confirm: "These changes should mostly apply to #custodian only. All bots wake on @mention only in that channel, the previous wake sequences ("Hey, Claudette", "Claudette,", "OK Claudette", etc..) should remain in place for all other channels. Gable's summons should mirror Claudette's. Bots can't summon each other in other channels." / "Yes, let's use the same wake pattern in non-custodian channels for both Claudette and Gable."
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

Outside #custodian, both residents wake the same way:
- their name as a standalone word, or `@name` (the server's mention context);
- a message that starts with one of the wake phrases, case-insensitive. Her
  current list is `hey X`, `hey, X`, `X,`, `X ` (name then a space), `yo X`,
  `ok X`, `ok, X`, `thanks X`, `thanks, X`, `thank you X`, `thank you, X`,
  `yeah X`, `yeah, X`, `sure X`, `sure, X`, `alright X`, `alright, X`,
  `jesus X`, `hi X`, `hi, X`, `damn it, X`, plus `bots` and `hey bots`.
  X is each resident's own name. Hers stays as it is; Gable gains the same
  list with `gable`. Her `claude and claudette` has no Gable counterpart.
- a message from a bot never wakes either resident outside #custodian.

Not mirrored: she also wakes on every message in a channel with exactly two
human members (her adapter's "DM" rule; every real DM here is two humans, no
bot). That is backlog-7's subject, "bots in private/small channels talk every
turn". It stays hers, unchanged, and Gable does not gain it unless plink says
so.

Her Discord runner is unchanged.

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
    has `@claudette` as a whole word, case-insensitive. The server attests
    against the bot's `name` (`claudette`), not a display name. A reply to her
    post without an `@` does not wake her, the same as any other message.
  - A bot's message wakes her only in three cases:
    - The digest: an author in `DIGEST_AUTHOR_IDS` whose content matches
      `DIGEST_PATTERN`, with `WAKE_ON_DIGEST` on. The pattern is anchored at
      the digest's fixed header (`^\[custodian daily `). The broker posts both
      the digest and her proposal echoes, so the author id cannot tell them
      apart; a test feeds #2849's text and expects no wake.
    - A bot chain: `BOT_SUMMON` on, context present, `@claudette`, the author
      in `PEER_BOTS`, and the broker's `summon-hop spend` allowing it.
    - Everything else is inert. Trigger phrases are off here.
- A bot-chain reply at depth 1 has every `@name` demoted to the bare name
  (guard 1); a reply on a granted chain keeps them. Refusals (not on the
  allowlist, or a hop refused) post in-channel with the broker's line.
- Her context block gets one line naming the trigger: mode, summoner, author
  type, depth, and the work item if any (her #1803 condition 2).
- A message that does not wake her still reaches her history: ingest happens
  before classification, and a test holds it. Mention-only must not become
  mention-only-and-amnesiac.
- Her adapter also reports unparks: a human post in #custodian citing a work
  item, with `BOT_SUMMON` on. Gable's adapter already does, but one reporter
  would park the chain silently whenever that daemon is down. The broker
  verifies every report itself and ignores a seq it has already seen, so two
  reporters cost nothing.

**Gable's other-channel wake** (gable lane).
- `summon.toml [summon] extra_patterns` gets one anchored, case-insensitive
  regex equal to the phrase list above with `gable`. The detector already
  applies patterns only outside mention-only channels, and it already
  ignores bot authors there, so this is config plus the template and a test.
  No detector code changes.
- The test holds the pattern to the exact list: every phrase wakes him at the
  start of a message, none wakes him mid-sentence, none wakes him in
  #custodian, and none wakes him when a bot writes it. The list lives in two
  repos, hers as code and his as config. A change to one is a change to both,
  and each side's test pins its own copy.

**Her adapter, one more rule:** outside #custodian, a bot-authored message
never wakes her, with a test. Today only her own echo is dropped.

**Activation** (plink-owned config, after both builds merge and deploy):
1. `verbs.toml`: `summon-hop = true` for `[res-claudette]` and `[res-gable]`.
2. `broker.toml`: the `[summon_hops]` block, with `state_path`, `hop_cap = 8`
   and `daily_hop_cap = 24`.
3. Gable's `summon.toml [summon]`: `bot_summon = true`, `peer_bots`, and
   `extra_patterns` (the phrase regex).
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
   - the digest wakes Claudette only;
   - outside #custodian: `hey gable` wakes Gable, `bots` wakes both, and a
     bot's `hey claudette` wakes nobody.

**Allowlists (plink, keyboard session):** each resident on the other's list,
and BuildGable on both. The keyboard's summons of either resident are bot
hops, counted on the cited work item.

## Lane → Review owner (DETERMINISTIC — filled from the lane, never preference)
- **Lane**: cross-lane (see split).
- **Review owner**: by lane.

## Builder (USER PREFERENCE — who orchestrates; never touches Review owner)
- **Builder**: BuildGable, the keyboard seat.

## Cross-lane split
- **Applies**: yes.
- **Surfaces by lane**:
  - custodian: `harness/broker/brokerd.py`, `gen_verb_surface.py`,
    `verb_surface.toml`, tests; claudette.git `disjorn_bot.py`, tests →
    review owner Claudette
  - gable: `harness/residency/summon.toml.template`, a residency test, and the
    live `summon.toml` flags → review owner Gable; plink sets the live flags
- **Split agreed in #custodian**: at the confirm seq, amended with the scope
  change (Request, second quote).

## Expected diff tier
Tier 2 — the broker and a resident adapter's wake path.

## Token estimate
One keyboard session.

## Confirm record
- **Confirmed by**: plink
- **#custodian seq**: 2884
- **Confirmed at**: 2026-09-23T13:32:58Z
- **Amended after the confirm**: plink at the keyboard, the Request's second
  and third quotes (other-channel mirror; no bot summons outside #custodian).

## Activation record (2026-09-23, 16:04Z)
- Merged: Disjorn `ff86adc` (custodian half, review-seq 2888) and `d671089`
  (gable half, review-seq 2901); claudette.git `b2509a0` + `0d73a09` (PASS
  2888, 2896).
- Live config, each with a `.bak-pre-mention-summons-20260923` beside it:
  - `verbs.toml`: `summon-hop` on for both seats;
  - `broker.toml`: `[summon_hops]` 8/24, with its state file;
  - Gable's `summon.toml`: the template's phrase line, mention-only, bot
    summons, peers `claudette, buildgable`, and `[hops]`;
  - Claudette's env: the five switches, peers `gable, buildgable`.
- Gable's live file was loaded through the residency config loader, and the
  phrase and near-miss checks passed against it. Her regenerated
  `broker_tools.py` is unchanged, with no `summon_hop`.
- Restarts: broker, `gable-summon` and `resident-cc`, 16:03:53–16:04:00Z.
- The live checks are posted in #custodian; results are recorded there.

## Status
merged
<!-- advanced from `confirmed` by `board --mark-merged` on 2026-09-23: build merged as d671089. The word `confirmed` on a merged spec made it indistinguishable from a buildable one. -->
