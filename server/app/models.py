"""Shared pydantic schemas: core entities + WS event payloads.

These mirror the DB schema (migrations/001_init.sql) and Architecture.md §4/§8.
DB rows store privacy_flags / emote_refs / keys_json as JSON text; routers are
responsible for json.loads/dumps at the boundary — these models hold the parsed
Python values.
"""

from typing import Any, Literal, Optional, Union

from pydantic import BaseModel, Field

MemberType = Literal["user", "bot"]
# 'app_build' is the modal build chat for an app (SPECS/2026-08-30-apps-tab-v1).
# It is an ordinary channel in every mechanical sense — messages, seq, history,
# WS fan-out, typing, the privacy filter — and explicit-membership-only, with
# exactly two members: the session's owner and the builder bot.
ChannelType = Literal["main_feed", "dm_1to1", "text", "app_build"]
# 'public': every user in the house is a member (implicit membership).
# 'private': channel_members is the wall — non-members read nothing.
ChannelVisibility = Literal["public", "private"]
UserStatus = Literal["online", "idle", "dnd", "offline"]
# 'duplicate' is the honest word for a row filed twice by UI error — distinct
# from 'rejected', which is a decision about the request rather than about the
# row. Row 3 took 'rejected' on 2026-08-23 for want of it (migration 010).
BacklogStatus = Literal["open", "spec'd", "built", "rejected", "duplicate"]
# The five build stages, FIXED (Amendment A edit 4). There is no percent and
# never will be: a builder's estimate of its own progress is self-report, and
# rendering self-report as a number is how a progress bar starts lying.
AppStage = Literal["scoped", "scaffolded", "files_written", "deployed", "live"]
AppVisibility = Literal["private", "shared", "public"]
AppStatus = Literal["draft", "live", "archived"]


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


class OpenSessionRef(BaseModel):
    """The live build session on an app, if it has one.

    Present only while the session is genuinely open — not ended, and its lock
    not lapsed. A lapsed lock reads as no open session, which is the same
    answer every other reader gives (there is no sweeper to make it true
    later).
    """

    id: int
    channel_id: int
    stage: Optional[AppStage] = None
    locked_until: str


class AppOut(BaseModel):
    """One app as the API and the `app_update` frame carry it.

    The `apps` row plus two facts that are about the CALLER rather than about
    the app: `on_menu` (is it in this user's APPS group) and `open_session`
    (is someone building it right now). Both are recomputed per request; a row
    read straight out of the table cannot answer either.
    """

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


class AppStageEvent(BaseModel):
    """A build session reached one of the five fixed stages.

    AUDIENCE: every socket of the session's OWNER, and nobody else. Not the
    builder bot (a resident must not read the harness's attestation about its
    own build as if it were chat), not other users, not admins. The owner is
    the only person the modal is open for, and the stage bar is the only
    consumer.

    Fan-out needs the owner's user id, which this frame deliberately does not
    carry — the bus event does, alongside it, the way channel_delete carries
    its recipient list. The routing fact stays off the wire.

    `detail` is a bounded JSON object (filenames, a note). There is no percent
    field and adding one would be a spec change, not a feature.

    The publisher in stage 1 is the REST endpoint alone, and nothing in
    production calls it yet: this is a subscription interface with the
    subscriber written first. A second subscriber must be addable without
    touching a publisher.
    """

    type: Literal["app_stage"] = "app_stage"
    channel_id: None = None
    session_id: int
    app_id: str
    stage: AppStage
    detail: dict[str, Any] = Field(default_factory=dict)
    created_at: str


class AppUpdateEvent(BaseModel):
    """An app's own record changed (renamed, re-described, gone live).

    AUDIENCE: every socket of the app's OWNER, and nobody else — the same wall
    as app_stage, for the same reason. Members who merely have the app on their
    menu learn about a rename on their next GET /apps; a push to them is a
    later stage's problem, and inventing one now would fan an owner's private
    draft name out to people the owner has not shared with.
    """

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
    AppStageEvent,
    AppUpdateEvent,
]
