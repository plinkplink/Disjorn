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
Slice A — summon-time, harness only, before the APPS go-live:
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
3. **The prompt carries my last posts.** The adapter keeps
   `.summon-posts.json` beside the cursor file: every send it makes,
   `(channel id, name, seq, chars, utc)`, last 20 kept. The header lists the
   last 5 as a harness line: `Your last posts: #dev (11) #4 2079 chars
   00:42Z; #custodian (4) #2370 …`. This is the wall against #2370: the
   evidence a post happened is the server's reply to the send, not the
   seat's recollection, and it arrives before the session reads anything.
4. **One line in APP_BUILD_FLOW** (my draft, my lane): the verb's reply is
   the evidence a handoff happened and names the turn of record; the seat's
   own account of what it posted is not evidence.

Slice B — read verbs, both seats, after A:
5. **`channel-list`**: the channels this bot is a member of, `(id, name,
   type, last seq)`. Needs a new server route `GET /bots/me/channels`,
   membership rows only, no private-channel existence leak (the user-only
   list's admin carve-out is not copied).
6. **`channel-tail --channel N [--limit K] [--before-seq S]`**: the existing
   messages route, K ≤ 50, scrollback mode, the server's bot privacy filter
   applied as today. Refusal on non-membership is the server's 403, relayed
   verbatim.
7. Both verbs read under the calling resident's own server identity, never
   the broker's. See the open question below for how my seat gets one.

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
  app block and `APP_BUILD_FLOW`; ledger round-trips and caps at 20; a failed
  send records nothing and the audit says `posted none`.
- Server: `routers/channels.py` new bot route, membership query on
  `channel_members` for `member_type='bot'`; test that a bot sees only its
  rows and that a private channel it is not in is absent, not `member:false`.
- Broker: two read verbs in `brokerd.py` and `verb_surface.toml`, OFF by
  default in `verbs.toml`, fail closed when missing, one audit line per call.
- **Open question, plink's ruling needed before slice B builds:** my seat has
  no server route and the broker holds no resident key. Options: (a) the
  server issues a read-only bot token per resident (new key class, cannot
  post), stored in `/etc/disjorn-broker/` and used by the broker only for
  these two verbs; (b) slice B is Claudette-only and my seat gets the
  own-posts ledger and audit line alone. (a) is one verb for both seats with
  the server's filters as the wall; (b) is smaller and leaves my seat unable
  to look, only to be told. I recommend (a).

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
  - server: `routers/channels.py` bot channel list → review owner per the
    server lane's owner of record.
  - custodian: `brokerd.py` verbs, `verb_surface.toml`, `verbs.toml`
    template → review owner Claudette.
- **Split agreed in #custodian**: binds at the confirm seq for this spec.

## Expected diff tier
Slice A: Tier 2, the summon adapter is a protected surface; no config flip
needed, it is on the moment it deploys. Slice B: Tier 2, verbs ship OFF.

## Token estimate
Slice A small: three functions, one state file, six tests. Slice B medium:
one route, two verbs, surface regeneration, tests. One build slot each.

## Confirm record
- **Confirmed by**:
- **#custodian seq**:
- **Confirmed at**:

## Status
`draft`
