# disjorn_sdk

Python SDK for writing Disjorn bots. WebSocket event stream + REST posting,
with automatic reconnect and seq-based backfill. Depends only on `httpx` and
`websockets` (no discord.py).

## Install

```bash
pip install -e sdk            # from the repo root, into your bot's venv
```

Python 3.11+.

## Quickstart

```python
import asyncio
from disjorn_sdk import DisjornClient, MessageCreate

client = DisjornClient("http://localhost:8000", api_key="YOUR_BOT_KEY")

async def handle(event):
    if isinstance(event, MessageCreate) and event.context is not None:
        # context is only attached when THIS bot was mentioned
        await client.send(
            event.channel_id,
            f"you rang, {event.message['author']['name']}?",
            reply_to=event.message["id"],
            emotion="happy",
        )

asyncio.run(client.run(handle))
```

A complete runnable example lives in `examples/echo_bot.py`:

```bash
python examples/echo_bot.py --url http://localhost:8000 --api-key KEY
```

## Auth model

- **REST**: every request carries `X-Api-Key: <key>` (the SDK does this for you).
- **WS**: the SDK connects to `/ws`, sends `{"op": "auth", "api_key": ...}` as
  its first frame (server allows 5s), and waits for
  `{"type": "ready", "bot_id": N}`. A rejected key (close code 4401 / HTTP 401)
  raises `DisjornAuthError` — fatal, not retried.

## Events

`client.events()` is an async iterator of typed dataclasses
(`disjorn_sdk.events`):

| Event | Fields | When |
|---|---|---|
| `Ready` | `bot_id`, `reconnected` | once per successful (re)connect |
| `MessageCreate` | `channel_id`, `seq`, `message` (full payload dict), `context` (dict or None), `backfilled` (bool) | new message in a member channel |
| `MessageEdit` | `channel_id`, `seq`, `message` (full updated payload) | message edited |
| `MessageDelete` | `channel_id`, `id`, `seq` | message soft-deleted (ids only) |
| `TypingStart` | `channel_id`, `author_type`, `author_id` | ephemeral, no seq |
| `Presence` | `user_id`, `status` | ephemeral, no seq |

`message` is the server's full materialized payload:
`{id, channel_id, seq, author_type, author_id, author: {type, id, name, ...},
content, created_at, edited_at, deleted_at, reply_to_id, privacy_flags,
emote_refs, attachments: [{id, original_filename, mime_type, size_bytes,
width, height, url}]}`.

`context` (Architecture §8.3) is attached **only** to the copy delivered to a
bot the message mentioned (`@name` or the bot's name as a word,
case-insensitive):
`{awake_users: [{id, name, status}], channel_state: {name},
privacy_flags_on_current_message: {}}`.

## Methods

| Method | Notes |
|---|---|
| `await client.send(channel_id, content, *, reply_to=None, emotion=None, emote_refs=None, privacy_flags=None)` | POST a message; returns the full message dict. `reply_to` → `reply_to_id` (same channel only). `emotion` is resolved server-side against the bot's chibi pack into `emote_refs`; unknown emotions are silently dropped. `emote_refs` may also be passed explicitly. |
| `await client.get_messages(channel_id, *, from_seq=None, before_seq=None, limit=None)` | `from_seq`: ascending current-state backfill (tombstones `{id, seq, deleted: true}` for deleted). `before_seq`: newest-first scrollback (deleted omitted). Mutually exclusive. Limit max 200. |
| `await client.members(channel_id)` | `[{type, id, name, status?}]`. **This is the bot's channel-discovery primitive** — `GET /channels` is user-only; bots learn channel ids from events and membership. |
| `await client.typing(channel_id)` | WS op (needs a live `events()` loop). Server rate-limits to 1 per 3s per channel. |
| `client.seed_seq(channel_id, seq)` | Pre-register a cursor (see resuming, below). |
| `await client.run(handler)` | Loop calling `await handler(event)`; a handler exception is logged and the loop continues. |
| `await client.aclose()` | Graceful shutdown of WS + HTTP. |
| `client.bot_id` / `client.ws` / `client.last_seen_seq` | Set after ready / live connection / per-channel high-water marks. |

Logging goes to the `disjorn_sdk` logger (stdlib `logging`).

## Reconnect & backfill semantics

- The `events()` loop reconnects forever with exponential backoff
  (`backoff_initial=1.0` doubling to `backoff_max=30.0`, ±25% jitter),
  yielding a fresh `Ready(reconnected=True)` each time. Only `aclose()` or
  `DisjornAuthError` stops it.
- The client tracks `last_seen_seq` per channel from every persisted event it
  sees. After a **re**connect, each known channel is REST-backfilled with
  `GET /channels/{id}/messages?from_seq=last+1` (paged) and missed messages
  are yielded as **synthetic `MessageCreate(backfilled=True)`** events, in seq
  order per channel, before live frames resume.
- **Backfill is current state, not event replay** (Architecture §8.2):
  - a message *edited* while you were away arrives as a single backfilled
    `MessageCreate` with the edited content and `edited_at` set — no separate
    `MessageEdit` event;
  - a message *deleted* while away appears server-side as a tombstone, which
    the SDK skips silently (its seq still advances the cursor) — you will
    never hear about it;
  - `typing_start`/`presence` carry no seq and are never backfilled;
  - backfilled events never carry `context`.
- Live `message_create` frames at-or-below the cursor are deduplicated (a
  message can land in both the backfill page and the live queue during
  reconnect; you see it once).
- Seq gaps are normal: messages hidden from bots by privacy flags consume a
  seq but are never delivered, in any form.
- **Resuming across restarts**: persist `client.last_seen_seq` on shutdown and
  call `client.seed_seq(channel_id, seq)` before starting. Note the *first*
  connect of a fresh client does not backfill — do a boot-time
  `get_messages(from_seq=...)` sweep yourself if you need catch-up before the
  first reconnect.

## Privacy guarantees (server-enforced)

Messages flagged `secret`/`off_the_record` never reach a bot — not in the live
stream, not in backfill, not in context, not even as tombstones. DM channels
are invisible to bots unless the bot was explicitly added
(`POST /channels/{id}/bots`). There is nothing to filter client-side.

## Porting guide for Claudette (from discord.py)

The shape changes from callback registration to an event loop, but every
discord.py concept has a direct mapping:

| discord.py | disjorn_sdk |
|---|---|
| `commands.Bot(...)` / `discord.Client(...)` | `DisjornClient(base_url, api_key)` |
| `bot.run(TOKEN)` | `asyncio.run(client.run(handler))` |
| `async def on_ready()` | `isinstance(event, Ready)` (fires on every reconnect too — check `event.reconnected`) |
| `async def on_message(message)` | `isinstance(event, MessageCreate)` → `event.message` dict |
| `message.content` / `message.author.display_name` / `message.id` | `event.message["content"]` / `["author"]["name"]` / `["id"]` |
| `message.author.bot` (self-echo guard) | `event.message["author_type"] == "bot"` (also guards against other bots) |
| `bot.user.mentioned_in(message)` | `event.context is not None` — the server only attaches `context` to a mentioned bot's copy; no name parsing needed |
| `message.channel.send(...)` | `await client.send(event.channel_id, ...)` |
| `message.reply(...)` | `await client.send(ch, ..., reply_to=event.message["id"])` |
| `async with channel.typing():` | `await client.typing(channel_id)` (fire once; server rate-limits) |
| `async def on_message_edit/delete` | `MessageEdit` / `MessageDelete` events |
| `async def on_typing` / `on_presence_update` | `TypingStart` / `Presence` events |
| `channel.history(...)` | `await client.get_messages(ch, before_seq=..., limit=...)` |
| `guild.members` / presence cache | `await client.members(ch)` + `Presence` events + `context["awake_users"]` |
| gateway resume / missed events | automatic: reconnect + seq backfill (see above) |

Concretely:

```python
# discord.py                              # disjorn_sdk
@bot.event                                async def handle(event):
async def on_message(message):                if not isinstance(event, MessageCreate):
    if message.author.bot:                        return
        return                                msg = event.message
    remember(message.content, ...)            if msg["author_type"] == "bot":
    if bot.user.mentioned_in(message):            return
        await message.reply(think(...))       remember(msg["content"], ...)
                                              if event.context is not None:
                                                  await client.send(
                                                      event.channel_id, think(...),
                                                      reply_to=msg["id"])
```

**Memory stays yours.** The ChromaDB + Voyage stack in
`bots/claudette/memory/` is untouched — Disjorn deliberately keeps memory
bot-side (Architecture §8.5). Wherever `on_message` fed `message.content` /
author / timestamp into Chroma, feed `event.message["content"]`,
`event.message["author"]["name"]`, `event.message["created_at"]` instead. Two
upgrades for free: backfilled `MessageCreate` events let you ingest what was
said while you were offline (check `event.backfilled` if you want to tag those
memories), and the server's privacy wall means anything flagged secret/off-
the-record physically never reaches your ingest path — no "please don't
remember this" prompt engineering.

**Context block replaces ad-hoc presence tracking.** Instead of maintaining
your own who's-online map from raw presence events, every mention hands you
`event.context["awake_users"]` (id, name, status) plus
`channel_state` — prebuilt server-side from already-filtered data. Keep
listening to `Presence` events only if you need presence *between* mentions.

**Chibis.** Your pack (category dirs + `Emotions.txt`, the existing
`/home/plink/bots/claudette/chibis/` convention) gets configured server-side
on the bot record. Then either pass `emotion="Smug"` on `send()` and the
server resolves it against the pack into `emote_refs` (unknown emotions are
silently dropped — no error handling needed), or pass explicit
`emote_refs=[...]` by name. Rendering (start/end of message body) is the
client's job; you just tag.

## Running the integration tests

The suite (`tests/test_sdk_live.py`) spins up a real server subprocess from
`server/.venv` against a throwaway SQLite DB on a random free port, creates
two users + a bot via `server/cli.py`, and exercises the SDK end-to-end
(live events, mention context, secret exclusion, reconnect backfill, DM
exclusion, typing). The SDK is installed into the **server venv** so one
interpreter has server deps + SDK:

```bash
server/.venv/bin/pip install -e sdk
cd sdk && ../server/.venv/bin/python -m pytest tests -v
```
