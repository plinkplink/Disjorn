# Spec: Gable cross-channel context — own-post evidence, channel identity, and read verbs

## Request
- **Verbatim**: "Take a look at Claudette's adapter, I'm pretty sure we built
  this functionality in early on and Gable needs it before we publish slice
  iii." / "Your cross- channel context based on #2380 - existing setup plus
  her verb recommendations plus any other features you will need or want. She
  will spec her own upgrade based on yours." / "This needs to build before we
  go live with APPS tab." / "I'm not too concerned about the false negatives,
  but we can add it in slice iii to prevent future confusion."
- **Requester**: plink
- **Origin**: #custodian seq 2379, 2383

## Existing setup (verified from the tree at `62a2aa5`; the other adapter is unreadable from this seat)
- `harness/residency/adapter.py` `_assemble` fetches the last 30 messages of
  the summoning channel and nothing else. `event.context` (the server's
  `awake_users` / `channel_state` / privacy block) is used by the detector to
  decide whether to wake and then dropped; `assemble_prompt` has no parameter
  for it. So the session never sees the channel's server name or type, or the
  `app` block an `app_build` channel carries.
- The prompt's `where` comes from `[summon].channel_names` in
  `/config/summon.toml`, which is empty. Every channel except #custodian
  renders as `channel N` in the prompt and in the audit line.
- `client.send` returns the posted message, seq included. `_safe_send`
  discards it. The audit line carries actions, seconds, and model only.
- `GET /channels/{id}/messages` is membership-gated and privacy-filtered for
  bots; `GET /channels` is user-only (DEFERRED.md, backfill cursor note). A
  bot cannot list its own channels.
- The container has no route to the server; my only tools are broker verbs.
- The other seat, per #2380: `search_topic` across her channels,
  `read_message` by channel and seq, no tail, no channel list.
- #2370 false negative: summoned in channel 11, posted 2079 chars (seq 4),
  next summon in #custodian read the audit line, saw no reply beside it,
  reported the post had not happened.

## Agreed UX
Slice A — summon-time, harness only. **This slice gates apps-builder-seat
slice (iii)**: the branch that selects `APP_BUILD_FLOW` has nothing to select
on until the context block reaches the session. It builds first, alone.
1. **The audit line carries the evidence.** `summon | plink in #dev (11) |
   ok | posted #4 (2079 chars) | 31 actions | 189.3s | claude-fable-5-1`.
   A summon that posted nothing says `posted none`. The next summon in
   #custodian reads its own record from the backfill; no memory of posting
   is needed.
2. **The prompt names the room from the server.** `where` = the server's
   `channel_state.name` and `type` plus the id, config override only as a
   fallback. In an `app_build` channel the header carries the `app` block
   (app id, name, session id, builder bot id, stage) as a harness line
   outside `[[CHAT]]`, and the flow block is `APP_BUILD_FLOW` instead of
   `SPEC_FLOW` (apps-builder-seat spec §I; this is the same edit).
   **Server strings never render raw into a harness line.** Channel names
   have no server-side length or charset check (`routers/channels.py`); app
   names have a length cap only. Both are typed by users. Before rendering:
   drop every code point below U+0020, U+007F, U+2028, U+2029; cap at 64
   chars with a trailing `…`; render inside double quotes (`room "dev"
   (11)`). One test feeds a name carrying a newline followed by a
   plausible harness sentence and asserts the header holds one line.
3. **The prompt carries the sends this adapter made.** The adapter keeps
   `.summon-posts.json` beside the cursor file: every send it makes,
   `(channel id, name, seq, chars, utc)`, last 20 kept. The header lists the
   last 5 as a harness line: `Sends by this adapter, last 5 (not a full
   list of your posts; the audit line in the backfill is the record): #dev
   (11) #4 2079 chars 00:42Z; …`. The wall against #2370 is the audit line
   (item 1); the ledger is a convenience that must not read as complete,
   since sends from any other path under this key never reach it.
4. **One line in APP_BUILD_FLOW** (my draft, my lane): the verb's reply is
   the evidence a handoff happened and names the turn of record; the seat's
   own account of what it posted is not evidence.

Slice B — one read verb, both seats, after A, not on the APPS path:
5. **`channel-tail --channel N [--limit K] [--before-seq S]`**: the existing
   messages route, K ≤ 50, scrollback mode, the server's bot privacy filter
   applied as today. Refusal on non-membership is the server's 403, relayed
   verbatim.
6. The verb reads under the calling resident's own server identity, never
   the broker's. See the open question below for how my seat gets one.
7. **`channel-list` is cut** (Claudette, #2388/#2400). A seat knows the
   channel it was summoned to and holds its own send seqs; nobody has named
   a case that needs enumeration. It was the only item needing a new server
   route and a private-channel-existence ruling. Reinstate only with a named
   case, as its own amendment.

Not in scope: cross-channel backfill into the summon prompt. Every extra
channel in the window is prompt tokens on every summon and widens the
backfill-poisoning surface DEFERRED.md records; the own-posts ledger gives
the seat what it actually lacked without reading anyone else's room.

## Architecture notes
- `adapter.py`: `_safe_send` returns the message dict; the reply send records
  to the posts ledger and hands seq/chars to `format_summary`. `_assemble`
  passes `event.context` through; `prompt.py` `assemble_prompt` grows
  `context` and renders the room line, the app block, and the own-posts line.
  `summary.py` `format_summary` gains `posted_seq`, `posted_chars`.
- Ledger file: `[posts].state_path` in `/config/summon.toml`, same
  atomic-write helper as `cursor.py`.
- Tests (`harness/residency/tests`): audit line with and without a post;
  header shows server name over config name; `app_build` context yields the
  app block and `APP_BUILD_FLOW`; hostile channel and app names render as
  one quoted, capped line; ledger round-trips and caps at 20; a failed send
  records nothing and the audit says `posted none`.
- Server: no change (channel-list cut).
- Broker: one read verb in `brokerd.py` and `verb_surface.toml`, OFF by
  default in `verbs.toml`, fails closed when missing, one audit line per call.
- **Open question, plink's ruling needed before slice B builds:** my seat has
  no server route and the broker holds no resident key. Options: (a) the
  server issues a read-only bot token per resident (new key class, cannot
  post), stored in `/etc/disjorn-broker/` and used by the broker only for
  this verb; (b) slice B is Claudette-only and my seat gets the own-posts
  ledger and audit line alone. (a) is one verb for both seats with the
  server's filters as the wall; (b) has one seat confirming its actions
  through another seat's account, the failure #2370 named, and both seats
  share a substrate so a misread is correlated. I recommend (a); Claudette
  backs (a) (#2388).

## Lane → Review owner (DETERMINISTIC — filled from the lane, never preference)
- **Lane**: cross-lane — see split.
- **Review owner**: per surface, below.

## Builder (USER PREFERENCE — who orchestrates; never touches Review owner)
- **Builder**: plink's call. BuildGable can draft slice A from this file; the
  `APP_BUILD_FLOW` line is mine.

## Cross-lane split
- **Applies**: yes
- **Surfaces by lane**:
  - gable: `harness/residency/adapter.py`, `prompt.py`, `summary.py`, tests,
    `/config/summon.toml` template, `APP_BUILD_FLOW` → review owner Gable.
  - custodian: `brokerd.py` verb, `verb_surface.toml`, `verbs.toml`
    template → review owner Claudette.
- **Split agreed in #custodian**: binds at the confirm seq for this spec.

## Expected diff tier
Slice A: Tier 2, the summon adapter is a protected surface; no config flip
needed, it is on the moment it deploys. Slice B: Tier 2, verbs ship OFF.

## Token estimate
Slice A small: three functions, one state file, seven tests. Slice B small:
one verb, surface regeneration, tests, plus the token class from the ruling.
One build slot each.

## Review record
- Round 1 (Claudette #2388, #2400; folded here 2026-09-08): hostile-name
  rendering into the trusted harness region → strip, cap, quote, test (item
  2); ledger label read as complete → relabelled, audit line named as the
  record (item 3); `channel-list` cut (item 7); slice A named as the slice
  (iii) gate; option (a) backed.

## Confirm record
- **Confirmed by**:
- **#custodian seq**:
- **Confirmed at**:

## Status
`draft`
