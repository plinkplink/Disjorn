"""Event dataclasses yielded by DisjornClient.events().

Ephemeral events carry no seq and cannot be backfilled.
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
    """Connected and authenticated; ``reconnected`` True means a REST backfill follows."""

    bot_id: int
    reconnected: bool = False


@dataclass(slots=True)
class MessageCreate:
    """Message in a channel the bot is in. ``context`` is set only for an @mentioned bot.

    ``backfilled`` messages are already current; no MessageEdit follows.
    """

    channel_id: int
    seq: int
    message: dict[str, Any]
    context: Optional[dict[str, Any]] = None
    backfilled: bool = False


@dataclass(slots=True)
class MessageEdit:
    """Edit; ``message`` is the full updated payload, ``seq`` the original's."""

    channel_id: int
    seq: int
    message: dict[str, Any]


@dataclass(slots=True)
class MessageDelete:
    """Soft delete; ids only, no content."""

    channel_id: int
    id: int
    seq: int


@dataclass(slots=True)
class TypingStart:
    """Someone is typing; rate-limited server-side."""

    channel_id: int
    author_type: str  # "user" | "bot"
    author_id: int


@dataclass(slots=True)
class Presence:
    """A user's presence changed."""

    user_id: int
    status: str  # "online" | "idle" | "dnd" | "offline"


@dataclass(slots=True)
class ChannelCreate:
    """Text channel created. Bots are not members until added (POST /channels/{id}/bots)."""

    channel: dict[str, Any]


@dataclass(slots=True)
class ChannelDelete:
    """Text channel deleted with its contents.

    HARD delete: no tombstones, and a backfill 404s. Drop state kept for ``channel_id``.
    """

    channel_id: int
    by_user_id: Optional[int]
    channel: dict[str, Any]


@dataclass(slots=True)
class MemberAdd:
    """User or bot added to a channel. ``by_user_id`` is None if nothing acted."""

    channel_id: int
    member_type: str  # "user" | "bot"
    member_id: int
    by_user_id: Optional[int]
    channel: dict[str, Any]


@dataclass(slots=True)
class MemberRemove:
    """User or bot left or was removed. For a bot, the last frame for that channel."""

    channel_id: int
    member_type: str  # "user" | "bot"
    member_id: int
    by_user_id: Optional[int]
    channel: dict[str, Any]


Event = Union[
    Ready, MessageCreate, MessageEdit, MessageDelete, TypingStart, Presence,
    ChannelCreate, ChannelDelete, MemberAdd, MemberRemove,
]
