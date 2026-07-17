"""Shared pydantic schemas: core entities + WS event payloads.

These mirror the DB schema (migrations/001_init.sql) and Architecture.md §4/§8.
DB rows store privacy_flags / emote_refs / keys_json as JSON text; routers are
responsible for json.loads/dumps at the boundary — these models hold the parsed
Python values.
"""

from typing import Any, Literal, Optional, Union

from pydantic import BaseModel, Field

MemberType = Literal["user", "bot"]
ChannelType = Literal["main_feed", "dm_1to1"]
UserStatus = Literal["online", "idle", "dnd", "offline"]


# ---------------------------------------------------------------------------
# Core entities
# ---------------------------------------------------------------------------

class User(BaseModel):
    """Public user shape — never includes password_hash."""

    id: int
    username: str
    display_name: str
    avatar_path: Optional[str] = None
    status: UserStatus = "offline"
    is_admin: bool = False
    created_at: str


class Channel(BaseModel):
    id: int
    type: ChannelType
    name: Optional[str] = None
    created_at: str


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


class Bot(BaseModel):
    """Public bot shape — never includes api_key_hash."""

    id: int
    name: str
    avatar_path: Optional[str] = None
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


Event = Union[
    MessageCreateEvent,
    MessageEditEvent,
    MessageDeleteEvent,
    TypingStartEvent,
    PresenceEvent,
]
