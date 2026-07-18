"""Typed event dataclasses yielded by :meth:`DisjornClient.events`.

Frame shapes mirror the server WS protocol (server/app/ws.py, Architecture
§8.2). Persisted events carry ``seq`` (per channel); ephemeral events
(:class:`TypingStart`, :class:`Presence`) do not and cannot be backfilled.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional, Union

__all__ = [
    "Ready",
    "MessageCreate",
    "MessageEdit",
    "MessageDelete",
    "TypingStart",
    "Presence",
    "ChannelCreate",
    "Event",
]


@dataclass(slots=True)
class Ready:
    """Connection established and authenticated.

    Yielded once per successful (re)connect. ``reconnected`` is False for the
    first connection of a client's lifetime, True afterwards — a True value
    means a REST backfill of every known channel follows immediately.
    """

    bot_id: int
    reconnected: bool = False


@dataclass(slots=True)
class MessageCreate:
    """A new message in a channel the bot is a member of.

    ``message`` is the server's full materialized payload dict:
    ``{id, channel_id, seq, author_type, author_id, author: {...}, content,
    created_at, edited_at, deleted_at, reply_to_id, privacy_flags,
    emote_refs, attachments}``.

    ``context`` is the structured context-injection block (Architecture §8.3:
    ``{awake_users, channel_state, privacy_flags_on_current_message}``) — set
    only on the copy delivered to a bot that was @mentioned (or name-matched)
    by the message, None otherwise. Backfilled events never carry context.

    ``backfilled`` is True for synthetic events produced by the reconnect
    backfill; such messages are in *current* state (an edit that happened
    while disconnected is already applied — no separate MessageEdit follows).
    """

    channel_id: int
    seq: int
    message: dict[str, Any]
    context: Optional[dict[str, Any]] = None
    backfilled: bool = False


@dataclass(slots=True)
class MessageEdit:
    """An existing message was edited; ``message`` is the full updated payload
    (``edited_at`` set). ``seq`` is the original message's seq."""

    channel_id: int
    seq: int
    message: dict[str, Any]


@dataclass(slots=True)
class MessageDelete:
    """A message was (soft-)deleted. Only ids travel — no content."""

    channel_id: int
    id: int
    seq: int


@dataclass(slots=True)
class TypingStart:
    """Someone started typing (ephemeral, no seq, rate-limited server-side)."""

    channel_id: int
    author_type: str  # "user" | "bot"
    author_id: int


@dataclass(slots=True)
class Presence:
    """A user's presence changed (ephemeral, no seq)."""

    user_id: int
    status: str  # "online" | "idle" | "dnd" | "offline"


@dataclass(slots=True)
class ChannelCreate:
    """A named text channel was created (broadcast to everyone; ephemeral,
    no seq).

    ``channel`` is ``{id, type, name}`` — for this event ``type`` is always
    ``"text"``. Receiving this does NOT make the bot a member: bots must be
    added explicitly (POST /channels/{id}/bots) before any of the channel's
    messages reach them.
    """

    channel: dict[str, Any]


Event = Union[
    Ready, MessageCreate, MessageEdit, MessageDelete, TypingStart, Presence,
    ChannelCreate,
]
