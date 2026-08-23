"""Shared pydantic schemas: core entities + WS event payloads.

These mirror the DB schema (migrations/001_init.sql) and Architecture.md §4/§8.
DB rows store privacy_flags / emote_refs / keys_json as JSON text; routers are
responsible for json.loads/dumps at the boundary — these models hold the parsed
Python values.
"""

from typing import Any, Literal, Optional, Union

from pydantic import BaseModel, Field

MemberType = Literal["user", "bot"]
ChannelType = Literal["main_feed", "dm_1to1", "text"]
# 'public': every user in the house is a member (implicit membership).
# 'private': channel_members is the wall — non-members read nothing.
ChannelVisibility = Literal["public", "private"]
UserStatus = Literal["online", "idle", "dnd", "offline"]
# 'duplicate' is the honest word for a row filed twice by UI error — distinct
# from 'rejected', which is a decision about the request rather than about the
# row. Row 3 took 'rejected' on 2026-08-23 for want of it (migration 010).
BacklogStatus = Literal["open", "spec'd", "built", "rejected", "duplicate"]


# ---------------------------------------------------------------------------
# Core entities
# ---------------------------------------------------------------------------

class User(BaseModel):
    """Public user shape — never includes password_hash.

    `avatar_url` mirrors Bot.avatar_url: the versioned serving URL
    (`/avatars/{id}?v={mtime}`) or None when there is no avatar.
    """

    id: int
    username: str
    display_name: str
    avatar_path: Optional[str] = None
    avatar_url: Optional[str] = None
    status: UserStatus = "offline"
    is_admin: bool = False
    created_at: str


class Channel(BaseModel):
    id: int
    type: ChannelType
    name: Optional[str] = None
    created_at: str
    visibility: ChannelVisibility = "public"
    # The owner (creator) of a text channel; only they may invite or kick.
    # NULL for main_feed and DMs, which have no creator.
    created_by: Optional[int] = None


class ChannelMember(BaseModel):
    channel_id: int
    member_type: MemberType
    member_id: int
    last_read_seq: int = 0


class Attachment(BaseModel):
    id: int
    message_id: int
    file_path: str
    original_filename: str
    mime_type: str
    size_bytes: int
    width: Optional[int] = None
    height: Optional[int] = None


class Message(BaseModel):
    id: int
    channel_id: int
    seq: int
    author_type: MemberType
    author_id: int
    content: str
    created_at: str
    edited_at: Optional[str] = None
    deleted_at: Optional[str] = None
    reply_to_id: Optional[int] = None
    privacy_flags: dict[str, Any] = Field(default_factory=dict)
    emote_refs: list[Any] = Field(default_factory=list)
    attachments: list[Attachment] = Field(default_factory=list)


class BacklogItem(BaseModel):
    """A feature request filed via `/backlog <text>` (WP-L2).

    `text` is stored verbatim; `author` is the poster's label (username or bot
    name). Triage happens through `/backlog reject|duplicate|spec'd|built <id>`
    or the Plan Room's reject button, which are the same write reached two ways
    (services/backlog.py).

    `spec_ref` is the spec SLUG, not a path — the path is derivable from the
    slug, and a stored path is wrong the day `SPECS/` is reorganised.

    `status_by_type` / `status_by_id` / `status_at` are who changed the status
    and when, TYPED — the same shape `messages` uses for an author, not a prose
    label. `author` above is already prose and one prose channel is enough. All
    three are None on a row nobody has triaged, and on every row filed before
    migration 010, which is the truthful answer rather than an invented one.
    """

    id: int
    text: str
    author: str
    created_at: str
    status: BacklogStatus = "open"
    spec_ref: Optional[str] = None
    status_by_type: Optional[MemberType] = None
    status_by_id: Optional[int] = None
    status_at: Optional[str] = None


class Bot(BaseModel):
    """Public bot shape — never includes api_key_hash.

    `avatar_url` is the versioned serving URL (`/bots/{id}/avatar?v={mtime}`,
    routers/media.py) or None when the bot has no avatar — consumers use the
    None to skip a request that would 404, and the `?v=` to avoid a stale
    cached face after a re-upload.
    """

    id: int
    name: str
    avatar_path: Optional[str] = None
    avatar_url: Optional[str] = None
    chibi_pack: Optional[str] = None
    created_at: str


class PushSubscription(BaseModel):
    id: int
    user_id: int
    endpoint: str
    keys: dict[str, str] = Field(default_factory=dict)
    created_at: str


# ---------------------------------------------------------------------------
# WS / bus events (full materialized payloads; persisted events carry seq)
# ---------------------------------------------------------------------------

class MessageCreateEvent(BaseModel):
    type: Literal["message_create"] = "message_create"
    channel_id: int
    message: Message


class MessageEditEvent(BaseModel):
    type: Literal["message_edit"] = "message_edit"
    channel_id: int
    message: Message


class MessageDeleteEvent(BaseModel):
    type: Literal["message_delete"] = "message_delete"
    channel_id: int
    id: int
    seq: int


class TypingStartEvent(BaseModel):
    """Ephemeral — no seq."""

    type: Literal["typing_start"] = "typing_start"
    channel_id: int
    author_type: MemberType = "user"
    author_id: int


class PresenceEvent(BaseModel):
    """Ephemeral — no seq, no channel."""

    type: Literal["presence"] = "presence"
    channel_id: None = None
    user_id: int
    status: UserStatus


class ChannelCreateRef(BaseModel):
    """Minimal channel payload carried by channel_create / channel_delete /
    member events.

    `name` is None only for a DM, which a member event can now name (a bot
    added to a DM) even though channel_create never does — channel_create fires
    for named text channels only.
    """

    id: int
    type: ChannelType
    name: Optional[str] = None
    visibility: ChannelVisibility = "public"


class ChannelCreateEvent(BaseModel):
    """A named text channel was created.

    Broadcast to all users and bots for a public channel; for a private one it
    reaches only that channel's members (the channel still exists as far as
    GET /channels is concerned — this is fan-out scoping, not hiding).
    """

    type: Literal["channel_create"] = "channel_create"
    channel_id: int
    channel: ChannelCreateRef


class ChannelDeleteEvent(BaseModel):
    """A text channel was deleted, along with everything in it.

    Fanned out to everyone who could see the channel a moment ago — every
    connected user and bot for a public one; for a private one its members,
    whoever deleted it, and every admin (an admin's sidebar carries a private
    channel they are not in as a bare row, which now has to go). That audience
    has to be computed before the row is deleted, because afterwards
    `is_member` answers False for everybody; the router carries it on the bus
    event and the WS hub delivers to exactly that list. The recipient list itself is internal and
    never reaches the wire.

    `channel` describes the channel that just stopped existing, so a client can
    say "#backroom was deleted" without having kept its own copy of the name.
    """

    type: Literal["channel_delete"] = "channel_delete"
    channel_id: int
    by_user_id: Optional[int] = None
    channel: ChannelCreateRef


class MemberAddEvent(BaseModel):
    """Someone joined a channel (invite accepted for them, or a bot added).

    Fanned out to the channel's members plus the affected member themselves.

    `by_user_id` is the user who performed the action — the difference between
    "alice added you to #backroom" and a room that silently changed shape. None
    only if a membership change ever has no acting user behind it.
    """

    type: Literal["member_add"] = "member_add"
    channel_id: int
    member_type: MemberType
    member_id: int
    by_user_id: Optional[int] = None
    channel: ChannelCreateRef


class MemberRemoveEvent(BaseModel):
    """Someone left a channel or was kicked from it.

    Same fan-out as member_add — including the removed member, whose client
    needs to know its access just ended.

    `by_user_id` distinguishes a kick from a walk-out: on /leave it is the
    leaving member themselves (equal to `member_id`), on /kick it is the owner.
    """

    type: Literal["member_remove"] = "member_remove"
    channel_id: int
    member_type: MemberType
    member_id: int
    by_user_id: Optional[int] = None
    channel: ChannelCreateRef


Event = Union[
    MessageCreateEvent,
    MessageEditEvent,
    MessageDeleteEvent,
    TypingStartEvent,
    PresenceEvent,
    ChannelCreateEvent,
    ChannelDeleteEvent,
    MemberAddEvent,
    MemberRemoveEvent,
]
