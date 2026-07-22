"""Slash-command framework + /backlog (WP-L2).

When a posted message's content starts with a registered ``/command``, the
server handles it and posts its own server-rendered reply into the same channel
(so absent users and bots see it async — the loop's intake half). Unknown
``/commands`` (people type ``/shrug``) pass through untouched as plain text: the
user's message is already persisted as normal chat by the messages create path,
and dispatch simply does nothing for them.

The registry is deliberately tiny — later commands register with @command("name")
and slot in. Command handlers receive a :class:`Ctx` and may return reply text
to post; returning None posts nothing.

/backlog:
    /backlog             -> lists the backlog (server-rendered reply, no LLM,
                            no bot summon), compact: id, short text, author, status.
                            Bounded to the most recent _LIST_MAX_ITEMS.
    /backlog <text>      -> files <text> VERBATIM as a new 'open' item, author =
                            poster. Resident triage happens later (not this WP).

Filing is refused (never echoing the text) when the request is not fit for a
public, bot-readable table:
    - the carrying message is privacy-flagged (secret / off_the_record) — the
      HIGH fix; the wall, see privacy.hidden_from_bots;
    - the command was posted from a DM (BL-D5) — DM-filed items would surface
      verbatim, with their author, in the next public `/backlog` listing;
    - the text exceeds MAX_BACKLOG_CHARS (BL-D6).

Replies are authored by the seeded 'system' bot (migration 006) and posted via
messages.deliver_message, so they flow through the normal message path (seq
allocation, bus publish, privacy inheritance) — they are ordinary public chat
messages, visible to everyone in the channel including bots.

GET /backlog: paginated JSON read of the table (``from_id`` cursor + ``limit``,
mirroring the messages endpoints' ``from_seq``+``limit`` idiom) so residents can
triage via the SDK without scraping chat.

Dispatch is rate limited per actor (SLASH_RATE_MAX per SLASH_RATE_WINDOW
seconds), in-process — this is a 5-user house, not a public service.
"""

import logging
import time
from typing import Annotated, Any, Awaitable, Callable, Optional

from fastapi import APIRouter, Depends, Query

from .. import db, privacy
from ..models import BacklogItem
from .auth import Actor, get_actor
from .messages import deliver_message

logger = logging.getLogger(__name__)

router = APIRouter()

CurrentActor = Annotated[Actor, Depends(get_actor)]

SYSTEM_BOT_NAME = "system"

# Longest text shown inline in the `/backlog` listing before it is ellipsised.
_LIST_TEXT_MAX = 80

# Most items rendered into one in-channel `/backlog` listing. Keeps the
# server-authored reply bounded: server-authored messages bypass
# MessageCreate.max_length, so an unbounded listing over a large backlog would
# be the one way to manufacture a giant message. 25 * (~80 chars + framing)
# stays around 3KB — well inside messages.MAX_MESSAGE_CHARS. Residents wanting
# the whole table use GET /backlog (paginated).
_LIST_MAX_ITEMS = 25

# Hard cap on a single backlog item's text, in characters (BL-D6). Tighter than
# messages.MAX_MESSAGE_CHARS on purpose: a backlog row is a one-line feature
# request that gets rendered into chat listings and read by residents, not a
# document. 2000 is Discord's whole-message limit and is roughly a dense
# paragraph — anything longer belongs in a spec, not the intake table.
MAX_BACKLOG_CHARS = 2000

# Per-actor slash-command rate limit (BL-D6). In-process and deliberately
# crude: a fixed window, a dict keyed by (actor_type, actor_id), no storage.
# 10 commands per 60s is far above any human's chat cadence and above a
# resident's, but low enough that a runaway loop can't fill the backlog table
# or the channel. Each dispatched command costs a persisted system reply, so
# this is the throttle on server-authored write amplification too.
SLASH_RATE_MAX = 10
SLASH_RATE_WINDOW = 60.0

# GET /backlog pagination (matches the messages endpoints' shape).
BACKLOG_PAGE_DEFAULT = 50
BACKLOG_PAGE_MAX = 200


# ---------------------------------------------------------------------------
# Command registry
# ---------------------------------------------------------------------------

class Ctx:
    """Everything a slash-command handler needs to run.

    ``args`` is the verbatim remainder after the command word (leading
    separator whitespace stripped); empty string when the command was posted
    with no argument.
    """

    def __init__(
        self,
        channel_id: int,
        args: str,
        actor: Actor,
        flags: Optional[dict[str, Any]] = None,
        channel_type: Optional[str] = None,
    ) -> None:
        self.channel_id = channel_id
        self.args = args
        self.actor = actor
        # Effective privacy flags of the message that carried this command
        # (caller-supplied + server NL detection, already merged by the create
        # path). Handlers that persist command text to bot-readable surfaces
        # must honor these — see /backlog.
        self.flags = flags or {}
        # channels.type of the channel the command was posted in ('main_feed',
        # 'text', 'dm_1to1'). Resolved once by dispatch. None only if the
        # channel vanished between insert and dispatch — treated as private
        # (fail closed), see is_private_channel.
        self.channel_type = channel_type

    @property
    def poster(self) -> str:
        """Human-readable label for the actor who posted the command."""
        if self.actor.type == "user" and self.actor.user is not None:
            return self.actor.user.username
        if self.actor.bot is not None:
            return self.actor.bot.name
        return f"{self.actor.type}:{self.actor.id}"

    @property
    def is_private_channel(self) -> bool:
        """True unless the channel is one everyone in the house can read.

        main_feed and named text channels are house-public (Architecture §4.1:
        every user is an implicit member). Everything else — DMs today, any
        future restricted channel type, and an unresolvable channel — counts as
        private. Fail closed: a new channel type is private until someone
        deliberately adds it here.
        """
        return self.channel_type not in ("main_feed", "text")


Handler = Callable[[Ctx], Awaitable[Optional[str]]]

_COMMANDS: dict[str, Handler] = {}


def command(name: str) -> Callable[[Handler], Handler]:
    """Register a slash-command handler under ``name`` (without the leading /)."""

    def register(fn: Handler) -> Handler:
        _COMMANDS[name] = fn
        return fn

    return register


# ---------------------------------------------------------------------------
# Rate limiting (in-process, per actor)
# ---------------------------------------------------------------------------

# (actor_type, actor_id) -> (window_start_monotonic, count_in_window)
_rate_state: dict[tuple[str, int], tuple[float, int]] = {}


def init() -> None:
    """Reset per-process dispatch state. Called from the app lifespan."""
    _rate_state.clear()


def _rate_check(actor: Actor) -> str:
    """Fixed-window per-actor limiter.

    Returns "allow", "deny_notify" (first refusal in this window — tell the
    actor once) or "deny_silent" (keep refusing without adding chat noise; a
    refusal reply is itself a persisted message, so it must not be a free
    amplifier).
    """
    key = (actor.type, actor.id)
    now = time.monotonic()
    start, count = _rate_state.get(key, (now, 0))
    if now - start >= SLASH_RATE_WINDOW:
        start, count = now, 0
    count += 1
    _rate_state[key] = (start, count)
    if count <= SLASH_RATE_MAX:
        return "allow"
    return "deny_notify" if count == SLASH_RATE_MAX + 1 else "deny_silent"


def _parse(content: str) -> Optional[tuple[str, str]]:
    """Split a message into (command_name, args), or None if it is not a command.

    A command must start at the very first character (``/name ...``). ``args`` is
    the remainder after the first run of whitespace, kept verbatim otherwise.
    """
    if not content.startswith("/"):
        return None
    parts = content.split(None, 1)
    if not parts:
        return None
    name = parts[0][1:]  # drop the leading '/'
    if not name:
        return None
    args = parts[1] if len(parts) > 1 else ""
    return name, args


async def dispatch(
    channel_id: int,
    content: str,
    actor: Actor,
    flags: Optional[dict[str, Any]] = None,
) -> None:
    """Handle a posted message if its content is a registered slash command.

    No-op for plain text and for unknown /commands. Called from the messages
    create path AFTER the user's message is persisted. ``flags`` are the
    persisted message's effective privacy flags, passed through so handlers can
    refuse to copy bot-hidden content onto public surfaces.

    Rate limited per actor before the handler runs, so a flood costs one dict
    update rather than a DB write plus a system reply.
    """
    parsed = _parse(content)
    if parsed is None:
        return
    name, args = parsed
    handler = _COMMANDS.get(name)
    if handler is None:
        return  # unknown command: leave the user's message as plain text

    decision = _rate_check(actor)
    if decision != "allow":
        logger.warning(
            "slash rate limit hit: %s %s /%s (%s)", actor.type, actor.id, name, decision
        )
        if decision == "deny_notify":
            await _post_system_reply(
                channel_id,
                f"Slow down — slash commands are limited to {SLASH_RATE_MAX} per "
                f"{int(SLASH_RATE_WINDOW)}s per person. Nothing was filed; try again shortly.",
            )
        return

    channel = await db.fetch_one("SELECT type FROM channels WHERE id = ?", (channel_id,))
    channel_type = channel["type"] if channel is not None else None
    reply = await handler(Ctx(channel_id, args, actor, flags, channel_type))
    if reply:
        await _post_system_reply(channel_id, reply)


async def _post_system_reply(channel_id: int, text: str) -> None:
    """Post a server-rendered reply as the seeded 'system' bot."""
    await deliver_message(channel_id, "bot", await _system_bot_id(), text)


async def _system_bot_id() -> int:
    row = await db.fetch_one("SELECT id FROM bots WHERE name = ?", (SYSTEM_BOT_NAME,))
    if row is None:  # migration 006 seeds it; never expected to be missing
        raise RuntimeError("system bot not found (migration 006 not applied?)")
    return row["id"]


# ---------------------------------------------------------------------------
# /backlog
# ---------------------------------------------------------------------------

async def _items_page(from_id: int = 0, limit: int = BACKLOG_PAGE_MAX) -> list[dict[str, Any]]:
    """Backlog rows with id >= from_id, oldest first, at most `limit` of them."""
    return await db.fetch_all(
        """SELECT id, text, author, created_at, status, spec_ref FROM backlog
           WHERE id >= ? ORDER BY id LIMIT ?""",
        (from_id, limit),
    )


async def _tail_items(limit: int) -> tuple[list[dict[str, Any]], int, int]:
    """(most recent `limit` items oldest-first, total count, open count)."""
    row = await db.fetch_one(
        "SELECT COUNT(*) AS total, COALESCE(SUM(status = 'open'), 0) AS open FROM backlog"
    )
    total = row["total"] if row is not None else 0
    open_count = row["open"] if row is not None else 0
    rows = await db.fetch_all(
        "SELECT id, text, author, created_at, status, spec_ref FROM backlog "
        "ORDER BY id DESC LIMIT ?",
        (limit,),
    )
    return list(reversed(rows)), total, open_count


def _short(text: str) -> str:
    one_line = " ".join(text.split())
    if len(one_line) > _LIST_TEXT_MAX:
        return one_line[: _LIST_TEXT_MAX - 1] + "…"
    return one_line


def _render_list(items: list[dict[str, Any]], total: int, open_count: int) -> str:
    """Render at most _LIST_MAX_ITEMS rows; `total`/`open_count` describe the
    whole table so the header stays honest when the listing is truncated."""
    if not items:
        return "Backlog is empty. File a request with `/backlog <text>`."
    header = f"Backlog ({total} item{'s' if total != 1 else ''}, {open_count} open):"
    lines = [
        f"#{it['id']} [{it['status']}] {_short(it['text'])} — {it['author']}"
        for it in items
    ]
    if total > len(items):
        lines.append(
            f"…showing the {len(items)} most recent of {total}; "
            "the full table is at GET /backlog."
        )
    return "\n".join([header, *lines])


@command("backlog")
async def _backlog(ctx: Ctx) -> str:
    if not ctx.args.strip():
        items, total, open_count = await _tail_items(_LIST_MAX_ITEMS)
        return _render_list(items, total, open_count)

    # ------------------------------------------------------------------ walls
    # Each refusal below returns WITHOUT echoing ctx.args: the whole point is
    # that the text is not fit for the public/bot-readable surface, and the
    # refusal is itself posted into chat.
    #
    # 1. The wall (HIGH fix). The backlog table and GET /backlog are
    #    bot-readable. If the carrying message was flagged bot-hidden (secret /
    #    off_the_record — by NL detection or explicit privacy_flags), refuse at
    #    intake rather than copy the text onto a public surface. "Never in any
    #    form": filtering the read side is not enough while the row persists as
    #    bot-readable data, so the row must never be written.
    if privacy.hidden_from_bots(ctx.flags):
        return (
            "Can't file that: the message is marked private (secret / "
            "off-the-record) and the backlog is readable by bots. Rephrase "
            "the request without the private content and file it again."
        )
    # 2. The footgun above the wall (BL-D5). The backlog is public feature
    #    requests by design (Architecture §13): one item filed in a DM is
    #    reprinted verbatim, with its author, by the next `/backlog` listing in
    #    #main. Text that is merely sensitive — not flag-worthy — would leak
    #    that way, and the person who typed it in a DM had no reason to expect
    #    it. So filing is refused outside house-public channels; listing still
    #    works everywhere (public data going to a private place is fine).
    if ctx.is_private_channel:
        return (
            "Can't file from a DM: the backlog is a public list — every item is "
            "reprinted verbatim, with its author, in `/backlog` listings and is "
            "readable by bots. Nothing was filed and nothing was copied out of "
            "this conversation. File it in #main or another public channel."
        )
    # 3. Size (BL-D6). Verbatim storage means an unbounded arg becomes an
    #    unbounded row and an unbounded chat listing.
    if len(ctx.args) > MAX_BACKLOG_CHARS:
        return (
            f"Can't file that: backlog items are capped at {MAX_BACKLOG_CHARS} "
            f"characters (that one was {len(ctx.args)}). Nothing was filed — "
            "post a one-paragraph summary and link the detail."
        )

    # File verbatim. Use the raw args (leading separator already stripped by the
    # parser); do not otherwise normalize the text.
    cur = await db.execute(
        "INSERT INTO backlog (text, author, created_at) VALUES (?, ?, ?)",
        (ctx.args, ctx.poster, db.utc_now()),
    )
    return f"Filed backlog #{cur.lastrowid} (open). Residents triage in #custodian."


# ---------------------------------------------------------------------------
# GET /backlog — SDK read surface for resident triage
# ---------------------------------------------------------------------------

@router.get("/backlog")
async def list_backlog(
    actor: CurrentActor,
    from_id: int = Query(default=0, ge=0),
    limit: int = Query(default=BACKLOG_PAGE_DEFAULT, ge=1, le=BACKLOG_PAGE_MAX),
) -> list[BacklogItem]:
    """Backlog table as JSON, oldest first. Any authenticated actor may read it.

    Paginated with the same cursor idiom as GET /channels/{id}/messages:
    ``from_id`` is an inclusive lower bound on the item id, ``limit`` caps the
    page (default BACKLOG_PAGE_DEFAULT, max BACKLOG_PAGE_MAX). Page forward with
    ``from_id = last_id + 1`` until a short page comes back.

    Lets residents triage via the SDK without scraping the server-rendered
    /backlog chat listing. Backlog items are public feature requests by
    construction: the filing path refuses bot-hidden content, DM-filed items and
    oversized text at intake, so no row here can carry secret / off-the-record
    text. No read-side filtering is needed.
    """
    return [BacklogItem(**it) for it in await _items_page(from_id, limit)]
