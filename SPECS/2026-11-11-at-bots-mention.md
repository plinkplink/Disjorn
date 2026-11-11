# Spec: `@bots` calls every bot in the channel

<!--
Governing plan: BUILD-LOOP.md. Style: COMMS.md. Backlog #57.
-->

## Request
- **Verbatim**: "@bots mention invokes all residents in the channel"
- **Requester**: plink
- **Origin**: backlog #57

## Agreed UX
- A person writes `@bots` in a channel. Every bot that is a member of that
  channel is addressed once, as if the person had written each `@name`. Each
  bot then answers under its own rules and its own budget.
- Only a person's `@bots` does this. A bot that writes `@bots` addresses
  nobody, whether it wrote it plainly, quoted it or put it in backticks.
- `@bots` is the only group word. It is matched as a whole word, in any case:
  `@Bots,` counts; `@botswana`, `plink@bots` and a bare `bots` do not.
- Where it is written:
  - Inside an inline code span or a fenced code block it does not count.
  - Inside a `>` quote or quotation marks it counts.
  - The rule is one sentence: what the client draws as a mention is what
    summons, and the client draws no mention inside code.
- A message flagged secret or off-the-record reaches no bot, so its `@bots`
  calls nobody.
- Membership decides who is called, in every channel type. A bot is in a
  channel only by an explicit membership row, so a private channel or a DM
  calls the bots in it and no others.
- Reply modes: there are two, `mention` and `always`, and both answer a
  mention, so both are called. No mode that never replies exists today. In a
  mention-only room (#custodian) `@bots` is an explicit `@`, so it summons
  there as `@name` does.
- Accounts that post and never answer are left out: `system`, `broker` and
  `BuildGable` (setting `GROUP_MENTION_SKIP_BOT_NAMES`). Naming one of them
  with `@name` still lists it, as before; nothing listens for them.
- An edit never summons anybody and never changes who a message called.
- A bot that is not connected when the message is written is not woken by it
  later. `@name` behaves the same way: backfill never summons.
- Client: `@bots` is drawn as a mention chip on a message the server fanned
  out. On a bot's message, or in a channel with no bots, it stays plain text.
  The composer has no mention autocomplete, and this build adds none.

## Architecture notes
**What existed** (read in the code, not assumed)
- The server already tells a bot it was addressed, as data: the bot's copy of
  a `message_create` carries a `context` block, and `context.addressed_by` is
  `"mention"` or `"always"` (`server/app/ws.py`). The mention test is a name
  regex, `@?name`, run per connected bot at fan-out.
- That block says "you were addressed". It does not say who else was, and it
  cannot tell `@name` from a bare name. Each adapter re-reads the text with
  its own `@name` regex to make that distinction in #custodian.
- No field listed the bots a message addressed. This build adds one.

**Server** (custodian lane)
- Migration `024_message_mentions.sql`:
  `ALTER TABLE messages ADD COLUMN mentioned_bot_ids TEXT NOT NULL DEFAULT '[]'`.
  A JSON array of bot ids. Old rows read `[]` and are not re-read.
- Stored, not computed per frame. The list is resolved once, from the
  membership at the moment of writing, so the POST response, the live frame,
  history, backfill and search all carry the same answer. An edit cannot
  summon or un-summon. The client's chip needs it on history, which a
  fan-out-only field would not reach.
- `server/app/mentions.py`, one function,
  `mentioned_bot_ids(channel_id, author_type, content)`:
  - code spans and fenced blocks are removed first, with the client's two
    patterns;
  - the channel's member bots are read from `channel_members`;
  - a bot is listed when its `@name` is present, for any author;
  - every member bot outside `GROUP_MENTION_SKIP_BOT_NAMES` is listed when
    `@bots` is present **and `author_type == "user"`**.
- The human-only wall is that one condition, and it is structural in three
  ways. `deliver_message` is the only `INSERT INTO messages` in the server and
  passes the row's own author type. The request model has no such field, so
  a caller cannot supply the list. Fan-out and adapters read the stored list
  and never re-derive the group from text.
- `deliver_message` stores `[]` for a message hidden from bots.
- `message_payload` carries `mentioned_bot_ids` on every message.
- `ws.py` fan-out: `addressed_by` is `"mention"` when the receiving bot is in
  the list, or when the bare-name regex matches as before. `@bots` adds no
  third value: the reply-modes spec fixed the two, and a bot called by the
  group is mentioned.
- A bot's `@name` of another bot is listed and addressed exactly as it was
  addressed before. Bot chains keep their existing walls.
- No bot may be named `bots`: its `@name` would be listed for any author.
  Nothing enforces that today; there is no bot-creation endpoint to put it in.

**Client** (custodian lane)
- `Message.mentioned_bot_ids?: number[]`.
- `buildMentionRe(names, withBots)` adds `@bots` as a whole word. `Markdown`
  takes `botsMention`; `MessageList` sets it when the message's list is not
  empty. Code spans already win over mentions in the renderer.

**Gable's adapter** (builder lane, `harness/residency/detector.py`)
- In a mention-only channel a person's message summons when the server
  context is present and either the text has `@gable` or `mentioned_bot_ids`
  lists his own bot id. The list is read only on the user path. The bot-chain
  path still requires his name in the text, so a list on a bot's message is
  never read: a second wall behind the server's.
- Outside #custodian the context block alone already summons him.
- `Trigger.group` is set when he was listed without being named, and the
  session's "How you were woken" line then says
  `called with every bot in this room (@bots)`. A resident called alongside
  the others should know it.
- His name regex and `extra_patterns` are unchanged.

## Adapter follow-ups
Nothing in any adapter's name matching is removed by this build.

**Gable** (in this repo): done above. Needs a `gable-summon` restart.

**CAVEMAN** (`/home/plink/bots/caveman/disjorn_bot.py`): no change. His
handler answers when `event.context is not None` and the author is a user.
The server attaches the context block to every listed bot, so a person's
`@bots` reaches him as a mention already. A bot's `@bots` attaches nothing,
and he drops bot authors himself.

**Claudette** (`/home/plink/bots/claudette/disjorn_bot.py`, at `165f592`):
- Outside #custodian: no change. `classify` wakes her as a mention whenever
  the context block is present on a person's message.
- In #custodian with `CUSTODIAN_MENTION_ONLY` on: she needs the patch below.
  Her rule there is context **and** `@claudette` in the text, so until it is
  applied a `@bots` in #custodian wakes Gable and not her.
- The patch reads the list on the person's path only, so a bot's `@bots`
  cannot wake her even if a list named her. It was applied to a scratch copy
  and compiled, and the patched `classify` was run in isolation against the
  cases in the two new tests. Her test suite was not run.

```diff
--- a/disjorn_bot.py
+++ b/disjorn_bot.py
@@ -454,7 +454,8 @@
     return m.group(1) if m else None
 
 
-def classify(channel_id: int, msg: dict, mentioned: bool, always: bool) -> Optional[Wake]:
+def classify(channel_id: int, msg: dict, mentioned: bool, always: bool,
+             me: Optional[int] = None) -> Optional[Wake]:
     """Whether this message wakes her, and how. Ingest happens before this and
     regardless of it: a message that does not wake her is still her history."""
     content = msg.get("content") or ""
@@ -464,7 +465,10 @@
     if custodian and CUSTODIAN_MENTION_ONLY:
         addressed = mentioned and _AT_ME.search(content) is not None
         if not is_bot:
-            return Wake("mention", who) if addressed else None
+            # A person's @bots: the server lists her in mentioned_bot_ids.
+            ids = msg.get("mentioned_bot_ids")
+            listed = me is not None and isinstance(ids, list) and me in ids
+            return Wake("mention", who) if addressed or (mentioned and listed) else None
         if (WAKE_ON_DIGEST and msg.get("author_id") in DIGEST_AUTHOR_IDS
                 and DIGEST_PATTERN.match(content)):
             return Wake("digest", who, "bot")
@@ -587,7 +591,7 @@
     # The server attaches context only when this bot is addressed: by name or
     # @name, or by a room (or DM) set to answer every message.
     always = (event.context or {}).get("addressed_by") == "always"
-    wake = classify(channel_id, msg, event.context is not None, always)
+    wake = classify(channel_id, msg, event.context is not None, always, client.bot_id)
     if wake is None:
         return
     if wake.mode == "bot-chain" and not await _clear_the_hop_wall(
--- a/tests/test_wake_rules.py
+++ b/tests/test_wake_rules.py
@@ -76,6 +76,21 @@
     assert _wake("@claudette look", author="gable", author_type="bot", author_id=2) is None
 
 
+def test_a_persons_at_bots_wakes_her_when_the_server_lists_her(on):
+    msg = {**_msg("@bots where are we"), "mentioned_bot_ids": [1, 2]}
+    assert bot.classify(CUSTODIAN, msg, True, False, 1).mode == "mention"
+    assert bot.classify(CUSTODIAN, msg, True, False, 6) is None
+    assert bot.classify(CUSTODIAN, msg, False, False, 1) is None
+    assert bot.classify(CUSTODIAN, msg, True, False) is None
+    assert bot.classify(CUSTODIAN, _msg("@bots where are we"), True, False, 1) is None
+
+
+def test_a_bots_at_bots_wakes_nobody_even_if_a_list_names_her(on):
+    msg = {**_msg("@bots wake up", author="gable", author_type="bot", author_id=2),
+           "mentioned_bot_ids": [1]}
+    assert bot.classify(CUSTODIAN, msg, True, False, 1) is None
+
+
 # -- other channels: the phrases stay, bots never wake her --------------------
 
 @pytest.mark.parametrize("text", ["hey claudette", "Claudette, look", "bots, anyone?"])
```

**Later, each adapter's own call:** replace its `@name` regex with
`own id in mentioned_bot_ids`. The list carries named mentions for that
reason. One difference to weigh first: the list ignores an `@name` inside
code, and the regexes do not.

## Cost and loops
- One `@bots` is one message, and each listed bot is addressed once, so it
  costs one summon per answering bot. A person could already do this by
  writing every name in one message. `@bots` opens no new path to a summon.
- What bounds it today:
  - Gable: his daily session cap (`[budget] daily_session_cap`,
    `harness/residency/budget.py`), spent per summon whoever called.
  - Claudette: no per-day cap found in `disjorn_bot.py` or `core.py`; a turn
    is bounded by her tool round cap and wall clock.
  - CAVEMAN: no per-day cap; a turn is bounded by a 60 s timeout.
  - The server has no rate limit on posting messages.
- No limiter is added. The bound per message is the number of answering bots
  in the room, three at most on this house.
- Loops: every reply to a `@bots` is bot-authored, so it cannot fan out. A
  reply that names another bot with `@name` in #custodian meets the existing
  allowlist and hop wall. Outside #custodian all three adapters ignore bot
  authors.

## Lane → Review owner (DETERMINISTIC — filled from the lane, never preference)
- **Lane**: cross-lane (see split).
- **Review owner**: by lane.

## Builder (USER PREFERENCE — who orchestrates; never touches Review owner)
- **Builder**: BuildGable, the keyboard seat.

## Cross-lane split
- **Applies**: yes.
- **Surfaces by lane**:
  - custodian: `server/app/mentions.py`, `server/app/ws.py`,
    `server/app/routers/messages.py`, `server/app/config.py`, migration 024,
    `server/tests/`, `client/`, `sdk/README.md`, and the patch to her own
    adapter above → review owner Claudette
  - builder: `harness/residency/detector.py`,
    `harness/residency/tests/test_at_bots.py` → review owner Gable
  - `harness/prose-baseline.toml` carries three lowered lines, two for the
    custodian lane's files and one for the builder lane's.
- **Split agreed in #custodian**: <pending>

## Questions for review
1. Claudette: is the patch to `classify` the shape you want, and do you want
   the "what woke you" line to say it was a group call, as Gable's now does?
2. Gable: is `called with every bot in this room (@bots)` the wording you want
   in the woken line?
3. Both: `GROUP_MENTION_SKIP_BOT_NAMES` defaults to `system`, `broker`,
   `BuildGable`. Is anyone missing or wrongly on it?

## Expected diff tier
Tier 2 (a migration and the summon path in ws.py).

## Token estimate
One keyboard session.

## Deploy recipe
1. Back up the DB (`sqlite3 .backup`).
2. `systemctl restart disjorn`, which applies 024.
3. Client build.
4. Restart `gable-summon`.
5. Apply the patch to claudette.git, run her tests, `claudette-update.sh`.
   Order does not matter for safety: before the server ships, the list is
   absent and her patched rule falls back to `@claudette`.
6. Live checks:
   - a person's `@bots` in #main wakes Claudette, Gable and CAVEMAN once each;
   - a person's `@bots` in #custodian wakes Claudette and Gable;
   - `` `@bots` `` in backticks wakes nobody and shows no chip;
   - a `@bots` posted as BuildGable wakes nobody.

## Confirm record
- **Confirmed by**: <pending>
- **#custodian seq**: <pending>
- **Confirmed at**: <pending>
<!-- No Confirm record → no build. This is the gate. -->

## Status
`built@loop/2026-11-11-at-bots-mention`
<!-- One of: draft / confirmed / building / built@<branch> / merged / failed. -->
