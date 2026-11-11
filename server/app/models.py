"""Pydantic schemas for entities and WS event payloads.

Routers parse DB JSON text at the boundary; these hold Python values.
"""

from typing import Any, Literal, Optional, Union

from pydantic import BaseModel, Field

MemberType = Literal["user", "bot"]
# 'app_build': explicit membership of exactly the owner and the builder bot.
ChannelType = Literal["main_feed", "dm_1to1", "text", "app_build"]
# 'private': channel_members is the wall; non-members read nothing.
ChannelVisibility = Literal["public", "private"]
UserStatus = Literal["online", "idle", "dnd", "offline"]
ReplyMode = Literal["mention", "always"]
# 'duplicate' is a filing error; 'rejected' is a decision about the request.
BacklogStatus = Literal["open", "spec'd", "built", "rejected", "duplicate"]
# Five fixed build stages. No percent: self-report shown as a number lies.
AppStage = Literal["scoped", "scaffolded", "files_written", "deployed", "live"]
AppVisibility = Literal["private", "shared", "public"]
AppStatus = Literal["draft", "live", "archived", "deleting"]


# Core entities

class User(BaseModel):
    """Public user shape; never includes password_hash."""

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
    # Owner only may invite or kick. NULL for main_feed and DMs.
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
    attribution: dict[str, Any] = Field(default_factory=dict)
    trace: Optional[dict[str, Any]] = None
    attachments: list[Attachment] = Field(default_factory=list)


class BacklogItem(BaseModel):
    """A feature request from `/backlog`. spec_ref is a slug, never a path."""

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
    """Public bot shape; never includes api_key_hash."""

    id: int
    name: str
    avatar_path: Optional[str] = None
    avatar_url: Optional[str] = None
    chibi_pack: Optional[str] = None
    created_at: str
    dm_open: bool = False


class OpenSessionRef(BaseModel):
    """A live build session. A lapsed lock reads as no session; nothing sweeps it."""

    id: int
    channel_id: int
    stage: Optional[AppStage] = None
    locked_until: str


class AppOut(BaseModel):
    """One app as the API and app_update carry it, per-caller fields included."""

    id: str
    name: str
    description: str = ""
    visibility: AppVisibility = "private"
    status: AppStatus = "draft"
    owner_user_id: int
    builder_bot_id: int
    parent_app_id: Optional[str] = None
    created_at: str
    updated_at: str
    on_menu: bool = False
    open_session: Optional[OpenSessionRef] = None


class PushSubscription(BaseModel):
    id: int
    user_id: int
    endpoint: str
    keys: dict[str, str] = Field(default_factory=dict)
    created_at: str


# WS and bus events. Persisted events carry seq.

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
    """Ephemeral; no seq."""

    type: Literal["typing_start"] = "typing_start"
    channel_id: int
    author_type: MemberType = "user"
    author_id: int


class PresenceEvent(BaseModel):
    """Ephemeral; no seq, no channel."""

    type: Literal["presence"] = "presence"
    channel_id: None = None
    user_id: int
    status: UserStatus


class ChannelCreateRef(BaseModel):
    """Channel payload for channel and member events. name is None only for a DM."""

    id: int
    type: ChannelType
    name: Optional[str] = None
    visibility: ChannelVisibility = "public"


class ChannelCreateEvent(BaseModel):
    """A named text channel was created. Public: everyone. Private: members only."""

    type: Literal["channel_create"] = "channel_create"
    channel_id: int
    channel: ChannelCreateRef


class ChannelDeleteEvent(BaseModel):
    """A text channel was deleted with its contents.

    Compute the audience before the row is deleted: afterwards is_member is False
    for all. Private: members and admins.
    """

    type: Literal["channel_delete"] = "channel_delete"
    channel_id: int
    by_user_id: Optional[int] = None
    channel: ChannelCreateRef


class MemberAddEvent(BaseModel):
    """A user or bot joined a channel. Fanned out to members and the new member."""

    type: Literal["member_add"] = "member_add"
    channel_id: int
    member_type: MemberType
    member_id: int
    by_user_id: Optional[int] = None
    channel: ChannelCreateRef


class MemberRemoveEvent(BaseModel):
    """A user or bot left or was kicked. by_user_id: the leaver on /leave, the owner on /kick."""

    type: Literal["member_remove"] = "member_remove"
    channel_id: int
    member_type: MemberType
    member_id: int
    by_user_id: Optional[int] = None
    channel: ChannelCreateRef


class MemberUpdateEvent(BaseModel):
    """A bot's reply mode changed; same audience as member_add."""

    type: Literal["member_update"] = "member_update"
    channel_id: int
    member_type: MemberType
    member_id: int
    by_user_id: Optional[int] = None
    reply_mode: ReplyMode
    channel: ChannelCreateRef


class AppStageEvent(BaseModel):
    """A build session reached a fixed stage.

    Audience: the owner's sockets only, never the builder bot. The bus event
    carries the owner id; the frame must not.
    """

    type: Literal["app_stage"] = "app_stage"
    channel_id: None = None
    session_id: int
    app_id: str
    stage: AppStage
    detail: dict[str, Any] = Field(default_factory=dict)
    created_at: str


class AppUpdateEvent(BaseModel):
    """An app's record changed. Audience: the owner's sockets only, as for app_stage."""

    type: Literal["app_update"] = "app_update"
    channel_id: None = None
    app: AppOut


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
    MemberUpdateEvent,
    AppStageEvent,
    AppUpdateEvent,
]
