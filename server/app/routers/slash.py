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
    /backlog <text>      -> files <text> VERBATIM as a new 'open' item, author =
                            poster. Resident triage happens later (not this WP).

Replies are authored by the seeded 'system' bot (migration 006) and posted via
messages.deliver_message, so they flow through the normal message path (seq
allocation, bus publish, privacy inheritance) — they are ordinary public chat
messages, visible to everyone in the channel including bots.

GET /backlog: JSON read of the table so residents can triage via the SDK without
scraping chat.
"""

from typing import Annotated, Any, Awaitable, Callable, Optional

from fastapi import APIRouter, Depends

from .. import db, privacy
from ..models import BacklogItem
from .auth import Actor, get_actor
from .messages import deliver_message

router = APIRouter()

CurrentActor = Annotated[Actor, Depends(get_actor)]

SYSTEM_BOT_NAME = "system"

# Longest text shown inline in the `/backlog` listing before it is ellipsised.
_LIST_TEXT_MAX = 80


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
    ) -> None:
        self.channel_id = channel_id
        self.args = args
        self.actor = actor
        # Effective privacy flags of the message that carried this command
        # (caller-supplied + server NL detection, already merged by the create
        # path). Handlers that persist command text to bot-readable surfaces
        # must honor these — see /backlog.
        self.flags = flags or {}

    @property
    def poster(self) -> str:
        """Human-readable label for the actor who posted the command."""
        if self.actor.type == "user" and self.actor.user is not None:
            return self.actor.user.username
        if self.actor.bot is not None:
            return self.actor.bot.name
        return f"{self.actor.type}:{self.actor.id}"


Handler = Callable[[Ctx], Awaitable[Optional[str]]]

_COMMANDS: dict[str, Handler] = {}


def command(name: str) -> Callable[[Handler], Handler]:
    """Register a slash-command handler under ``name`` (without the leading /)."""

    def register(fn: Handler) -> Handler:
        _COMMANDS[name] = fn
        return fn

    return register


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
    """
    parsed = _parse(content)
    if parsed is None:
        return
    name, args = parsed
    handler = _COMMANDS.get(name)
    if handler is None:
        return  # unknown command: leave the user's message as plain text
    reply = await handler(Ctx(channel_id, args, actor, flags))
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

async def _all_items() -> list[dict[str, Any]]:
    return await db.fetch_all(
        "SELECT id, text, author, created_at, status, spec_ref FROM backlog ORDER BY id"
    )


def _short(text: str) -> str:
    one_line = " ".join(text.split())
    if len(one_line) > _LIST_TEXT_MAX:
        return one_line[: _LIST_TEXT_MAX - 1] + "…"
    return one_line


def _render_list(items: list[dict[str, Any]]) -> str:
    if not items:
        return "Backlog is empty. File a request with `/backlog <text>`."
    open_count = sum(1 for it in items if it["status"] == "open")
    header = f"Backlog ({len(items)} item{'s' if len(items) != 1 else ''}, {open_count} open):"
    lines = [
        f"#{it['id']} [{it['status']}] {_short(it['text'])} — {it['author']}"
        for it in items
    ]
    return "\n".join([header, *lines])


@command("backlog")
async def _backlog(ctx: Ctx) -> str:
    if not ctx.args.strip():
        return _render_list(await _all_items())
    # The backlog table and GET /backlog are bot-readable. If the carrying
    # message was flagged bot-hidden (secret / off_the_record — by NL detection
    # or explicit privacy_flags), refuse at intake rather than copy the text
    # onto a public surface. The wall is "never in any form": filtering the
    # read side is not enough while the row persists as bot-readable data, so
    # the row must never be written. The refusal text does not echo the args.
    if privacy.hidden_from_bots(ctx.flags):
        return (
            "Can't file that: the message is marked private (secret / "
            "off-the-record) and the backlog is readable by bots. Rephrase "
            "the request without the private content and file it again."
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
async def list_backlog(actor: CurrentActor) -> list[BacklogItem]:
    """Backlog table as JSON, oldest first. Any authenticated actor may read it.

    Lets residents triage via the SDK without scraping the server-rendered chat
    listing. Backlog items are public feature requests by construction: the
    /backlog filing path refuses bot-hidden content at intake, so no row here
    can carry secret / off-the-record text. No read-side filtering is needed.
    """
    return [BacklogItem(**it) for it in await _all_items()]
