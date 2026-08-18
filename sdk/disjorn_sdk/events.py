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
    "ChannelDelete",
    "MemberAdd",
    "MemberRemove",
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
    """A named text channel was created (ephemeral, no seq).

    ``channel`` is ``{id, type, name, visibility}`` — for this event ``type``
    is always ``"text"``. A ``"public"`` channel's creation is broadcast to
    everyone; a ``"private"`` one reaches only its members, so a bot that is
    not a member never sees this event for it at all. Receiving this does NOT
    make the bot a member: bots must be added explicitly (POST
    /channels/{id}/bots) before any of the channel's messages reach them.
    """

    channel: dict[str, Any]


@dataclass(slots=True)
class ChannelDelete:
    """A text channel was deleted, with everything in it (ephemeral, no seq).

    Reaches the same audience :class:`ChannelCreate` did: everyone for a public
    channel, its members for a private one — so a bot that was in the channel
    hears about it, and one that never was does not. ``channel`` is
    ``{id, type, name, visibility}`` describing what is gone, and
    ``by_user_id`` is the user (its owner, or an admin) who deleted it.

    The delete is HARD on the server: the channel's messages are not coming
    back, and no MessageDelete tombstones are sent for them. Drop any state you
    keep for ``channel_id`` on receipt — a backfill of it will 404.
    """

    channel_id: int
    by_user_id: Optional[int]
    channel: dict[str, Any]


@dataclass(slots=True)
class MemberAdd:
    """A user or bot was added to a channel (ephemeral, no seq).

    Reaches the channel's members plus the member it happened to, so a bot
    hears both "someone joined a room I am in" and "I was just added
    somewhere". ``channel`` is ``{id, type, name, visibility}`` as on
    :class:`ChannelCreate`; ``member_type`` is ``"user"`` or ``"bot"`` and
    ``member_id`` identifies the subject in that namespace. ``by_user_id`` is
    the user who did it (None if nothing acted).
    """

    channel_id: int
    member_type: str  # "user" | "bot"
    member_id: int
    by_user_id: Optional[int]
    channel: dict[str, Any]


@dataclass(slots=True)
class MemberRemove:
    """A user or bot left a channel or was removed from it (ephemeral, no seq).

    Same audience as :class:`MemberAdd`, the subject included — for a bot that
    is itself the subject, this frame is the last thing it hears about that
    channel. ``by_user_id`` is the kicker, or the leaving member themselves.
    """

    channel_id: int
    member_type: str  # "user" | "bot"
    member_id: int
    by_user_id: Optional[int]
    channel: dict[str, Any]


Event = Union[
    Ready, MessageCreate, MessageEdit, MessageDelete, TypingStart, Presence,
    ChannelCreate, ChannelDelete, MemberAdd, MemberRemove,
]
