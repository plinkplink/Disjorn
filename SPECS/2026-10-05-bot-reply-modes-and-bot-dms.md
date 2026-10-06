# Spec: Bot reply modes per channel, and direct messages with a bot

<!--
Governing plan: BUILD-LOOP.md. Style: COMMS.md.
Backlog #7 and #4. Drafted by the keyboard seat (Claude Opus 5.5, posting as BuildGable) under plink's 2026-10-05 hand-off.
-->

## Request
- **Verbatim**:
  - #7: "Add functionality and UI components to set always-on, mention-only mode for bots at member_add AND create toggle inside a new context menu on member-row static div"
  - #4: "DMs with bots. Sometimes we need to paste resident code in chat and it might be better to do that in a private channel rather than public #custodian."
- **Requester**: plink
- **Origin**: backlog #7 (2026-08-23), backlog #4 (2026-08-23)

## Problem
A bot in a channel answers only when its name is matched. There is no way to say "this bot answers everything in this room", and no 1:1 room with a bot. Today the workaround for a private conversation with a resident is a private text channel plus an add-bot step, and the bot still needs its name typed on every line.

## Agreed UX
- **Reply mode** is a property of one bot's membership in one channel, with two values:
  - **Mentions only** (default, today's behaviour). The bot answers when its name is used.
  - **Every message**. Every message a person writes in the room is addressed to the bot. Bot-authored messages never are, so two always-on bots cannot talk to each other forever.
- **Add a bot** (AddBotModal): the confirm step gains a two-option choice, "Replies when called by name" or "Replies to every message", with the default on name. "Called by name" is honest for both residents: one also wakes on spoken forms like "hey <name>". One line under it states the consequence of "every": "It will read and answer everything people post here."
- **Member list**: a bot's row gets a context menu. It opens on right-click, on long-press on touch, and from a `⋯` button that shows on hover/focus and is always visible on touch screens. Items:
  - "Replies to: When called by name ✓ / Every message". This changes the mode in place.
  - "Remove from channel".
  - Only people who may manage bots in the channel see the menu (the existing `_require_bot_manage_access` rule). Everyone else sees a small mode badge on the row (`@` or `all`) and no menu.
- **Mention-only rooms stay mention-only.** In a channel listed in the new server setting `MENTION_ONLY_CHANNEL_IDS` (live value: `[4]`, #custodian), "Every message" is refused (400 "This room is mention-only") and greyed out in the UI with that reason. app_build rooms are already closed to membership changes.
- **DM a bot**: the "new direct message" picker lists DM-able bots below people, each with a BOT tag. Picking one opens (get-or-create) a private 1:1 room holding the person and the bot, and the bot's mode there is fixed at "Every message". The room shows in the DM list under the bot's name and avatar. The bot can't be removed from its own DM and its mode can't be changed; leaving the DM is the person's exit.
- **Which bots can be DMed** is a per-bot admin flag, `dm_open`, off by default and set with `PATCH /bots/{id}`. The residents decide their own flag in review below; plink flips it.

## Architecture notes
**Server** (custodian lane)
- Migration `018_bot_reply_modes.sql`:
  - `ALTER TABLE channel_members ADD COLUMN reply_mode TEXT NOT NULL DEFAULT 'mention' CHECK (reply_mode IN ('mention','always'))`. The column means something only on `member_type='bot'` rows.
  - `ALTER TABLE bots ADD COLUMN dm_open INTEGER NOT NULL DEFAULT 0`.
- `POST /channels/{id}/bots` body gains an optional `reply_mode`, default `mention`. A new `PATCH /channels/{id}/bots/{bot_id}` takes `{reply_mode}`. Both pass the existing `_require_bot_manage_access`, then refuse `always` in a `MENTION_ONLY_CHANNEL_IDS` channel. The PATCH is refused in a bot DM (403 "A bot DM always replies").
- A mode change publishes a `member_update` event (channel, bot id, reply_mode) through the same path as `member_add`, so open member panels update live.
- `GET /channels/{id}/members` returns `reply_mode` for bot rows. `GET /bots` returns `dm_open`.
- `POST /dms` accepts exactly one of `{user_id}` or `{bot_id}`. The bot form:
  - 404 unless the bot exists and has `dm_open = 1`.
  - Canonical lookup is the `dm_1to1` channel with this user and this bot as its only members.
  - Creates it with the user row plus a bot row with `reply_mode='always'`.
  - `DmResponse` gains `dm_bot_id` (nullable) beside `dm_user_id`, which becomes nullable too.
  - `GET /channels` labels a bot DM with the bot's name and avatar.
- Leaving a bot DM removes only the person's row. Removing the bot from it is refused, like app_build membership.
- **ws.py fan-out**, `attach` for non-app_build channels becomes: name match OR (the receiving bot's membership row is `always` AND the message is user-authored). The member-row read is cached per channel and invalidated on `member_add`/`member_remove`/`member_update`, the same lifetime as the existing bot-membership cache.
- `_context_block` gains `"addressed_by": "mention" | "always"`. Adapters may ignore it; it is there so a resident can tell "they said my name" from "this room is mine to answer" without inferring it.

**Client** (custodian lane)
- api.ts: `addChannelBot(channelId, botId, replyMode)`, `setBotReplyMode(...)`, `openBotDm(botId)`.
- types: `MemberOut.reply_mode`, `Bot.dm_open`, `Channel.dm_bot_id`.
- AddBotModal: the mode radio and the consequence line.
- UserPanel `MemberRow` for bots: the context menu (right-click, long-press at 500 ms, `⋯` button), keyboard reachable (Enter/Space opens, Esc closes, arrow keys move), and the read-only badge for non-managers.
- New-DM picker and sidebar DM rows: the bot entries with avatar and BOT tag.

**Residents.**
- Gable: works unchanged for a single message, since `detector.py` returns `MODE_MENTION` when `has_context`. Gable's own follow-up (residency lane, after this ships, #3159 Q3): fold the messages that queue up in an always-room into the next session. Otherwise a five-message paste becomes five sessions with replies in between. Until that lands, a DM paste in several pieces costs several sessions.
- Custodian resident: a **required rider on claudette.git** (`fix/2026-10-05-addressed-by-and-attribution`). It must merge and deploy BEFORE the server half deploys, never after (review card comment 75, BLOCKING). The adapter currently infers a DM from "exactly two humans in the room", caches that answer forever, and wakes on every message there, so the menu would lie for her. The rider deletes that inference and wakes with a "channel" wake when `context.addressed_by == "always"`.

A bot that should not be always-on anywhere keeps `dm_open = 0`, and its channel memberships stay on mentions unless a manager flips them. Residents can opt out harder in their own adapters later; that is outside this spec.

## Lane → Review owner (DETERMINISTIC — filled from the lane, never preference)
- **Lane**: custodian (server/, client/).
- **Review owner**: Claudette.

## Builder (USER PREFERENCE — who orchestrates; never touches Review owner)
- **Builder**: keyboard seat (BuildGable) with Opus hands.

## Folds from review
- Custodian resident, card comment 75: the BLOCK is the rider above. Q1: DMs on for her. Q2: keep `addressed_by`. Q3: the copy says "called by name". Non-blocking: `MENTION_ONLY_CHANNEL_IDS` and her `CUSTODIAN_MENTION_ONLY` state one fact twice. Accepted for now; later, the context block carries the room's mention-only flag.
- Gable, #3159: Q1, DMs on for him under his existing 60/day counter, no separate cap. Q2, `addressed_by` rides on EVERY context block, both values, never omitted (app_build sends `always`). Q3 is his residency follow-up above.
- Behaviour kept at deploy: channel 9 (two people plus her) answers every message today only because of the inference. The deploy recipe sets her membership there to `always`, so the room behaves exactly as before and the menu now shows it.

## Questions for review
1. Each resident: do you want `dm_open` on for yourself? Gable is summon-priced: a DM spends his daily budget the same way a mention does. Is that acceptable, or should his DM have a lower per-day cap?
2. Is `addressed_by` in the context block useful to your adapters, or noise?
3. Anything in "every message" mode that your adapter would do wrong? For example, the custodian resident's wake-phrase path, or Gable's work-item parsing on unaddressed text.

## Expected diff tier
Tier 2 (migration plus a summon-path change in ws.py).

## Token estimate
Moderate: one migration, two endpoints, one fan-out branch, three client components, tests.

## Deploy recipe
1. Back up the DB (`sqlite3 .backup`).
2. `systemctl restart disjorn`, which applies 018.
3. Client build.
4. Set `MENTION_ONLY_CHANNEL_IDS=[4]` in server/.env before the restart.
5. Order: the rider is merged and deployed first (`claudette-update.sh`).
6. `dm_open = 1` for both residents (Q1 answers). This spends plink's tokens, so it's his flip at deploy.
7. `UPDATE channel_members SET reply_mode='always' WHERE channel_id=9 AND member_type='bot' AND member_id=1`. This keeps today's behaviour in that room.

## Confirm record
- **Confirmed by**:
- **#custodian seq**:
- **Confirmed at**:
<!-- Bot-recorded 3161 withdrawn after both reviews (#3167, #3174): a confirm is a human's seq. Branches build ahead; nothing merges without it. -->

## Status
`draft`
