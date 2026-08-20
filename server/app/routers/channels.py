"""Channels, membership, read state (WP3; named text channels added post-v1).

Endpoints:
    GET    /channels                      (user)  — sidebar list: unread counts + last-message snippet
                                                    (private channels you are not in: admins only)
    POST   /channels {name, visibility?}  (user)  — create a named `text` channel;
                                                    409 on duplicate name; publishes channel_create
    DELETE /channels/{id}                 (user)  — delete a `text` channel; OWNER or ADMIN;
                                                    hard delete (content included);
                                                    publishes channel_delete
    POST   /dms {user_id}                 (user)  — idempotent get-or-create of the 1:1 DM channel
    PUT    /channels/{id}/read {seq}      (user)  — monotonic last_read_seq upsert (no event published)
    GET    /channels/{id}/members         (actor) — member listing, membership-gated
    POST   /channels/{id}/invite {user_id}(user)  — private channels; OWNER ONLY; member_add
    POST   /channels/{id}/join            (user)  — private channels are invite-only (403)
    POST   /channels/{id}/leave           (user)  — anyone may leave a private channel; member_remove
    POST   /channels/{id}/kick {user_id}  (user)  — private channels; OWNER ONLY; member_remove
    POST   /channels/{id}/bots {bot_id}   (user)  — add a bot to a channel; member_add
                                                    (DMs: participants only;
                                                     private channels: owner only)
    DELETE /channels/{id}/bots/{bot_id}   (user)  — remove a bot from a channel; member_remove
                                                    (same access rule as adding)

Exported access-rule helpers (consumed by WP4 messages and WP5 privacy/WS):
    is_member(channel_id, member_type, member_id) -> bool
    user_channel_ids(user_id) -> list[int]      # public main_feed/text implicit
    bot_channel_ids(bot_id)  -> list[int]       # explicit rows only

Membership semantics (Architecture §4.1 + SPECS/2026-08-08-per-channel-membership):
- A channel is `public` (the default, and what every pre-existing channel was
  grandfathered to) or `private`.
- PUBLIC main_feed AND public named `text` channels implicitly include ALL
  users (this is a <=5-human server); a channel_members row is created lazily
  only to store last_read_seq (first PUT /channels/{id}/read).
- PRIVATE text channels have no implicit members at all: channel_members is
  the wall, for humans and bots alike (no bot-shaped exception in either
  direction). Non-members get 403 on every read path, and their messages never
  surface in search or in the sidebar's last-message snippet.
- A private channel's EXISTENCE is hidden from ordinary non-members: GET
  /channels omits it for them (RULED by plink, 2026-08-17, superseding the
  spec's original "existence is not hidden for everyone" — a sidebar full of
  rooms you cannot open is not the UX anyone wanted). ADMINS still get the row,
  with `member: false` and no content: they can see that a channel exists, and
  that is ALL — no read access, ever, which is rule 5 (no silent god-view)
  intact. The listing is still derived from membership (user_channel_ids), not
  from a second visibility rule, so there is only ever one wall to reason about.
- DM channels have exactly two user members.
- Bots are explicit-members-only EVERYWHERE — main_feed (cli.py create-bot
  inserts the row), text channels and DMs via POST .../bots.
- Text-channel names: unique, lowercase [a-z0-9-], 1-32 chars (displayed as
  #name); enforced here plus a partial unique index (005_text_channels.sql).
- The owner (channels.created_by, set at creation) is the only one who may
  invite, kick, or hand a bot the keys to a private channel (RULED by plink,
  2026-08-12). There is deliberately no admin override on those verbs: an
  in-product god-view read button would make "private" a lie. plink owns the
  box and can read the DB directly; the app ships no silent back door.
- DELETING a channel is the one verb an admin may do to a channel they do not
  own (RULED by plink, 2026-08-17). It is not an exception to the paragraph
  above, because it hands nobody a way to READ anything: destroying content is
  the opposite of leaking it. Rule 5 (no silent god-view) is untouched —
  invite/kick/bot-adds stay owner-only.
"""

import re
import sqlite3
from typing import Annotated, Any, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from .. import db, events
from ..models import ChannelType, ChannelVisibility, MemberType, User, UserStatus
from .auth import Actor, get_actor, get_current_user

router = APIRouter()

CurrentUser = Annotated[User, Depends(get_current_user)]
CurrentActor = Annotated[Actor, Depends(get_actor)]

SNIPPET_LEN = 80


# ---------------------------------------------------------------------------
# Access-rule helpers (exported; other WPs import these)
# ---------------------------------------------------------------------------

async def is_member(channel_id: int, member_type: MemberType, member_id: int) -> bool:
    """True if the actor may access the channel.

    Users are implicit members of PUBLIC main_feed and text channels (no row
    required). Everything else — user in a private channel, user in a DM, bot
    anywhere — requires an explicit channel_members row. Unknown channels are
    never accessible.

    This is the single wall: every read path (history, seq/read state, search,
    WS fan-out) asks this question, so a private channel is enforced in one
    place rather than re-derived per endpoint.
    """
    channel = await db.fetch_one(
        "SELECT type, visibility FROM channels WHERE id = ?", (channel_id,)
    )
    if channel is None:
        return False
    if (
        member_type == "user"
        and channel["type"] in ("main_feed", "text")
        and channel["visibility"] == "public"
    ):
        return True
    row = await db.fetch_one(
        """SELECT 1 FROM channel_members
           WHERE channel_id = ? AND member_type = ? AND member_id = ?""",
        (channel_id, member_type, member_id),
    )
    return row is not None


async def user_channel_ids(user_id: int) -> list[int]:
    """All channel ids the user can access: public main_feed + public text
    (implicit) + explicit rows (private channels, DMs; lazy read-state rows
    dedupe via UNION)."""
    rows = await db.fetch_all(
        """SELECT id FROM channels
            WHERE type IN ('main_feed', 'text') AND visibility = 'public'
           UNION
           SELECT channel_id FROM channel_members
           WHERE member_type = 'user' AND member_id = ?""",
        (user_id,),
    )
    return [r["id"] for r in rows]


async def bot_channel_ids(bot_id: int) -> list[int]:
    """Channel ids the bot is an explicit member of. No implicit access, ever."""
    rows = await db.fetch_all(
        """SELECT channel_id FROM channel_members
           WHERE member_type = 'bot' AND member_id = ?""",
        (bot_id,),
    )
    return [r["channel_id"] for r in rows]


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

class LastMessage(BaseModel):
    seq: int
    snippet: str
    author_type: MemberType
    author_id: int
    created_at: str


class ChannelListItem(BaseModel):
    """One sidebar row.

    `member` is false only for a private channel the caller is not in, which
    (RULED 2026-08-17) only an admin is ever shown. Such a row carries NO
    content — `unread` is 0 and `last_message` is None, because the snippet is
    content and content is exactly what the wall is for.

    `created_by` is the channel's owner, so the client can render owner-only
    affordances (invite / kick) without a second round trip. It is None for
    main_feed and DMs, which have no creator.
    """

    id: int
    type: ChannelType
    name: Optional[str] = None          # DMs: the OTHER participant's display name
    dm_user_id: Optional[int] = None    # DMs: the OTHER participant's user id
    unread: int = 0
    last_message: Optional[LastMessage] = None
    visibility: ChannelVisibility = "public"
    member: bool = True
    created_by: Optional[int] = None


class ChannelCreateRequest(BaseModel):
    name: str
    # Omitted -> public, so every existing caller keeps creating exactly the
    # channel it created before this spec.
    visibility: ChannelVisibility = "public"


class MemberRef(BaseModel):
    """The subject of an invite/kick."""

    user_id: int


class DmCreateRequest(BaseModel):
    user_id: int


class DmResponse(BaseModel):
    id: int
    type: ChannelType = "dm_1to1"
    name: str
    dm_user_id: int
    created: bool


class ReadRequest(BaseModel):
    seq: int = Field(ge=0)


class MemberOut(BaseModel):
    """One row of the member panel.

    `avatar_path` is the DB-relative storage path (non-null == this member has
    an avatar) and `avatar_url` the versioned serving URL, both mirroring the
    message-author payload. Without them a member panel had to fire one
    request per member and let most of them 404 — bots almost never have an
    avatar. `avatar_url` also carries the `?v={mtime}` cache key, so a
    repainted avatar shows up on the next members fetch instead of waiting out
    the response cache.
    """

    type: MemberType
    id: int
    name: str
    status: Optional[UserStatus] = None  # users only
    avatar_path: Optional[str] = None
    avatar_url: Optional[str] = None


class BotRef(BaseModel):
    bot_id: int


def _snippet(content: str) -> str:
    if len(content) <= SNIPPET_LEN:
        return content
    return content[: SNIPPET_LEN - 1] + "…"


async def _get_channel(channel_id: int) -> dict[str, Any]:
    channel = await db.fetch_one("SELECT * FROM channels WHERE id = ?", (channel_id,))
    if channel is None:
        raise HTTPException(status_code=404, detail="Channel not found")
    return channel


# ---------------------------------------------------------------------------
# GET /channels — sidebar list
# ---------------------------------------------------------------------------

@router.get("/channels")
async def list_channels(user: CurrentUser) -> list[ChannelListItem]:
    """Sidebar list: the channels the caller can actually open, plus (for an
    admin only) the bare existence of the private ones they are not in.

    RULED by plink, 2026-08-17, superseding the merged spec: an ordinary user's
    sidebar does not list private channels they are not a member of. An admin
    still sees every row so the house has someone who can tell what exists —
    and sees it exactly as before, `member: false` with unread 0 and no
    last-message snippet. Seeing the row is not reading the room: every read
    path still refuses them (test_no_silent_admin_god_view).
    """
    main = await db.fetch_one("SELECT * FROM channels WHERE type = 'main_feed'")
    texts = await db.fetch_all(
        "SELECT * FROM channels WHERE type = 'text' ORDER BY name"
    )
    dms = await db.fetch_all(
        """SELECT c.* FROM channels c
           JOIN channel_members cm ON cm.channel_id = c.id
           WHERE c.type = 'dm_1to1' AND cm.member_type = 'user' AND cm.member_id = ?""",
        (user.id,),
    )

    # ONE question, asked once: which channels is this user a member of?
    # user_channel_ids already answers "public main_feed/text implicitly, plus
    # my explicit rows", so both the visibility filter below and the content
    # math further down hang off it rather than re-deriving the wall.
    member_ids = set(await user_channel_ids(user.id))
    if not user.is_admin:
        texts = [c for c in texts if c["id"] in member_ids]

    chans = ([main] if main is not None else []) + texts + dms
    if not chans:
        return []

    # Content (unread math + snippet) is computed ONLY over the channels the
    # caller is a member of; the rest (an admin's view of a private channel
    # they are not in) are listed as bare rows.
    ids = [c["id"] for c in chans if c["id"] in member_ids]

    # One aggregate query each — no per-channel N+1.
    max_seqs: dict[int, int] = {}
    last_msgs: dict[int, dict[str, Any]] = {}
    reads: dict[int, int] = {}
    if ids:
        ph = ",".join("?" * len(ids))
        max_seqs = {
            r["channel_id"]: r["max_seq"]
            for r in await db.fetch_all(
                f"""SELECT channel_id, MAX(seq) AS max_seq FROM messages
                    WHERE channel_id IN ({ph}) GROUP BY channel_id""",
                ids,
            )
        }
        last_msgs = {
            r["channel_id"]: r
            for r in await db.fetch_all(
                f"""SELECT m.channel_id, m.seq, m.author_type, m.author_id,
                           m.content, m.created_at
                    FROM messages m
                    JOIN (SELECT channel_id, MAX(seq) AS s FROM messages
                          WHERE deleted_at IS NULL AND channel_id IN ({ph})
                          GROUP BY channel_id) latest
                      ON latest.channel_id = m.channel_id AND latest.s = m.seq""",
                ids,
            )
        }
        reads = {
            r["channel_id"]: r["last_read_seq"]
            for r in await db.fetch_all(
                f"""SELECT channel_id, last_read_seq FROM channel_members
                    WHERE member_type = 'user' AND member_id = ? AND channel_id IN ({ph})""",
                [user.id, *ids],
            )
        }
    partners: dict[int, dict[str, Any]] = {}
    dm_ids = [c["id"] for c in dms]
    if dm_ids:
        dph = ",".join("?" * len(dm_ids))
        partners = {
            r["channel_id"]: r
            for r in await db.fetch_all(
                f"""SELECT cm.channel_id, u.id AS user_id, u.display_name
                    FROM channel_members cm JOIN users u ON u.id = cm.member_id
                    WHERE cm.member_type = 'user' AND cm.member_id != ?
                      AND cm.channel_id IN ({dph})""",
                [user.id, *dm_ids],
            )
        }

    def build(c: dict[str, Any]) -> ChannelListItem:
        lm = last_msgs.get(c["id"])
        partner = partners.get(c["id"])
        return ChannelListItem(
            id=c["id"],
            type=c["type"],
            name=partner["display_name"] if partner is not None else c["name"],
            dm_user_id=partner["user_id"] if partner is not None else None,
            visibility=c["visibility"],
            member=c["id"] in member_ids,
            created_by=c["created_by"],
            unread=max(0, (max_seqs.get(c["id"]) or 0) - reads.get(c["id"], 0)),
            last_message=LastMessage(
                seq=lm["seq"],
                snippet=_snippet(lm["content"]),
                author_type=lm["author_type"],
                author_id=lm["author_id"],
                created_at=lm["created_at"],
            )
            if lm is not None
            else None,
        )

    def activity_ts(c: dict[str, Any]) -> str:
        lm = last_msgs.get(c["id"])
        return lm["created_at"] if lm is not None else c["created_at"]

    # main_feed pinned first; text channels alphabetically; DMs by most
    # recent activity (ISO strings sort).
    dms_sorted = sorted(dms, key=activity_ts, reverse=True)
    ordered = ([main] if main is not None else []) + texts + dms_sorted
    return [build(c) for c in ordered]


# ---------------------------------------------------------------------------
# POST /channels — create a named text channel
# ---------------------------------------------------------------------------

CHANNEL_NAME_RE = re.compile(r"^[a-z0-9-]{1,32}$")


@router.post("/channels")
async def create_channel(body: ChannelCreateRequest, user: CurrentUser) -> ChannelListItem:
    """Create a `text` channel. 409 on duplicate name.

    `visibility` defaults to "public" — flat access, any user, the pre-spec
    behaviour. A "private" channel starts with exactly one member (its
    creator, who is also its owner) and grows only by invite.

    Publishes a channel_create event on the bus; the WS hub fans it out to all
    connected users and bots as {type: "channel_create", channel: {id, type,
    name, visibility}} — or, for a private channel, only to its members.
    Clients otherwise pick new channels up via GET /channels.
    """
    name = body.name
    if not CHANNEL_NAME_RE.fullmatch(name):
        raise HTTPException(
            status_code=400,
            detail="Channel name must be 1-32 characters: lowercase a-z, 0-9, or -",
        )
    private = body.visibility == "private"
    try:
        async with db.transaction() as conn:
            cur = await conn.execute(
                """INSERT INTO channels (type, name, visibility, created_by)
                   VALUES ('text', ?, ?, ?)""",
                (name, body.visibility, user.id),
            )
            channel_id = cur.lastrowid
            if private:
                # The wall is channel_members, so the owner needs a real row —
                # nothing about a private channel is implicit.
                await conn.execute(
                    """INSERT INTO channel_members (channel_id, member_type, member_id)
                       VALUES (?, 'user', ?)""",
                    (channel_id, user.id),
                )
    except sqlite3.IntegrityError:
        # Partial unique index on (name) WHERE type='text' — duplicate name.
        raise HTTPException(
            status_code=409, detail=f"Channel #{name} already exists"
        ) from None
    await events.publish(
        {
            "type": "channel_create",
            "channel_id": channel_id,
            "channel": {
                "id": channel_id,
                "type": "text",
                "name": name,
                "visibility": body.visibility,
            },
        }
    )
    return ChannelListItem(
        id=channel_id,
        type="text",
        name=name,
        visibility=body.visibility,
        created_by=user.id,
    )


# ---------------------------------------------------------------------------
# DELETE /channels/{id} — destroy a text channel and everything in it
# ---------------------------------------------------------------------------

# Channels the house cannot lose: deletion is refused outright, no matter who
# asks (plink, 2026-08-19). "main" is doubly safe — the real #main is the
# main_feed row, which the type check below already refuses — but the name is
# listed anyway so a text channel christened `main` can never be torn down
# either. #custodian is an ordinary text channel only by accident of birth;
# this set is what stands between it and one fat-fingered owner/admin delete.
# Mirrored client-side (AppShell.tsx) so the menu item is never even offered.
PROTECTED_CHANNEL_NAMES = frozenset({"main", "custodian"})


def _require_owner_or_admin(channel: dict[str, Any], user: User) -> None:
    """Deletion is the owner's call — or an admin's (RULED by plink, 2026-08-17).

    Every OTHER channel verb that admins are kept out of (invite, kick,
    bot-adds) grants read access to content; this one destroys content and
    grants nothing, so letting an admin clear out a room they were never in
    leaks exactly nothing. `_require_owner` stays as-is for those verbs.
    """
    if channel["created_by"] != user.id and not user.is_admin:
        raise HTTPException(
            status_code=403,
            detail="Only the channel's owner or an admin may delete it",
        )


@router.delete("/channels/{channel_id}")
async def delete_channel(channel_id: int, user: CurrentUser) -> dict[str, bool]:
    """Delete a `text` channel, its membership rows and all of its messages.

    Owner or admin only. main_feed and DMs are refused (400): main_feed is the
    house's one permanent room, and a DM belongs to two people rather than to a
    creator who could delete it out from under the other one. Channels named in
    PROTECTED_CHANNEL_NAMES (#custodian) are refused the same way — 400, the
    target's problem rather than the caller's rights — before the permission
    check, so nobody, owner and admin included, can delete them.

    HARD delete, in one transaction: `DELETE FROM channels` cascades to
    channel_members, to messages (ON DELETE CASCADE, and foreign_keys is ON for
    the shared connection — see db.connect), and on through messages to
    attachments. The messages_fts AFTER DELETE trigger fires on the cascaded
    row deletions too, so a deleted channel's content stops being searchable
    rather than lingering in the index (test_channel_delete asserts this, plus
    an FTS integrity-check).

    ORPHANS: attachment FILES under DATA_DIR are deliberately left on disk. The
    rows that name them are gone, so nothing serves them; reclaiming the bytes
    is a housekeeping job for whoever owns the box, not something this request
    should be doing inline with a user waiting on it.

    Publishes `channel_delete` on the bus. The recipient list is computed HERE,
    before the row disappears — after the delete `is_member` answers False for
    everyone, so the WS hub could not work out who used to be able to see the
    channel. A public channel carries `recipients: None` (= everyone, the same
    audience channel_create had). A private one carries its explicit member
    list, plus the acting user, plus EVERY admin: an admin's sidebar shows a
    private channel they are not in as a bare row (RULED 2026-08-17), so an
    admin needs this frame to drop a ghost row for a room that no longer
    exists. It tells them nothing they could not already see — the row was
    already on their screen, and the frame carries no content.
    """
    channel = await _get_channel(channel_id)
    if channel["type"] != "text":
        raise HTTPException(
            status_code=400,
            detail=(
                "Only named text channels can be deleted: main_feed is "
                "permanent, and a DM belongs to both of its participants"
            ),
        )
    if channel["name"] in PROTECTED_CHANNEL_NAMES:
        raise HTTPException(
            status_code=400,
            detail=f"#{channel['name']} is protected and cannot be deleted",
        )
    _require_owner_or_admin(channel, user)

    recipients: Optional[list[list[Any]]] = None
    if channel["visibility"] == "private":
        rows = await db.fetch_all(
            "SELECT member_type, member_id FROM channel_members WHERE channel_id = ?",
            (channel_id,),
        )
        pairs: set[tuple[str, int]] = {
            (r["member_type"], r["member_id"]) for r in rows
        }
        pairs.add(("user", user.id))
        # Admins had the channel in their sidebar as a bare, contentless row;
        # without this frame it would sit there pointing at nothing.
        pairs.update(
            ("user", r["id"])
            for r in await db.fetch_all("SELECT id FROM users WHERE is_admin = 1")
        )
        recipients = [[t, i] for t, i in sorted(pairs)]

    async with db.transaction() as conn:
        await conn.execute("DELETE FROM channels WHERE id = ?", (channel_id,))

    await events.publish(
        {
            "type": "channel_delete",
            "channel_id": channel_id,
            "by_user_id": user.id,
            "channel": {
                "id": channel_id,
                "type": channel["type"],
                "name": channel["name"],
                "visibility": channel["visibility"],
            },
            # Internal to the bus: the WS frame never carries it (knowing who
            # else was in a room you just lost is not the client's business).
            "recipients": recipients,
        }
    )
    return {"ok": True}


# ---------------------------------------------------------------------------
# POST /dms — idempotent get-or-create 1:1 DM
# ---------------------------------------------------------------------------

@router.post("/dms")
async def create_or_get_dm(body: DmCreateRequest, user: CurrentUser) -> DmResponse:
    if body.user_id == user.id:
        raise HTTPException(status_code=400, detail="Cannot DM yourself")
    target = await db.fetch_one(
        "SELECT id, display_name FROM users WHERE id = ?", (body.user_id,)
    )
    if target is None:
        raise HTTPException(status_code=404, detail="User not found")

    # Canonical pair lookup: the dm_1to1 channel where BOTH users are members,
    # regardless of who created it (order-independent by construction).
    existing = await db.fetch_one(
        """SELECT c.id FROM channels c
           JOIN channel_members a ON a.channel_id = c.id
                AND a.member_type = 'user' AND a.member_id = ?
           JOIN channel_members b ON b.channel_id = c.id
                AND b.member_type = 'user' AND b.member_id = ?
           WHERE c.type = 'dm_1to1'""",
        (user.id, body.user_id),
    )
    if existing is not None:
        return DmResponse(
            id=existing["id"], name=target["display_name"],
            dm_user_id=target["id"], created=False,
        )

    async with db.transaction() as conn:
        cur = await conn.execute(
            "INSERT INTO channels (type, name) VALUES ('dm_1to1', NULL)"
        )
        channel_id = cur.lastrowid
        for member_id in (user.id, body.user_id):
            await conn.execute(
                """INSERT INTO channel_members (channel_id, member_type, member_id)
                   VALUES (?, 'user', ?)""",
                (channel_id, member_id),
            )
    return DmResponse(
        id=channel_id, name=target["display_name"],
        dm_user_id=target["id"], created=True,
    )


# ---------------------------------------------------------------------------
# PUT /channels/{id}/read — monotonic read-state upsert
# ---------------------------------------------------------------------------

@router.put("/channels/{channel_id}/read")
async def mark_read(channel_id: int, body: ReadRequest, user: CurrentUser) -> dict[str, int]:
    await _get_channel(channel_id)
    # Read state is a read path: a non-member must not learn a private
    # channel's seq numbers, and the upsert below must never manufacture
    # membership of a DM or a private channel. is_member is True without a row
    # only for public main_feed/text, which is exactly where the lazy row is
    # wanted.
    if not await is_member(channel_id, "user", user.id):
        raise HTTPException(status_code=403, detail="Not a member of this channel")
    # public main_feed / text: implicit membership — the row is created lazily
    # here, solely to store last_read_seq. Monotonic: never lowered.
    await db.execute(
        """INSERT INTO channel_members (channel_id, member_type, member_id, last_read_seq)
           VALUES (?, 'user', ?, ?)
           ON CONFLICT (channel_id, member_type, member_id)
           DO UPDATE SET last_read_seq = MAX(last_read_seq, excluded.last_read_seq)""",
        (channel_id, user.id, body.seq),
    )
    row = await db.fetch_one(
        """SELECT last_read_seq FROM channel_members
           WHERE channel_id = ? AND member_type = 'user' AND member_id = ?""",
        (channel_id, user.id),
    )
    assert row is not None
    return {"channel_id": channel_id, "last_read_seq": row["last_read_seq"]}


# ---------------------------------------------------------------------------
# GET /channels/{id}/members
# ---------------------------------------------------------------------------

@router.get("/channels/{channel_id}/members")
async def list_members(channel_id: int, actor: CurrentActor) -> list[MemberOut]:
    channel = await _get_channel(channel_id)
    if channel["type"] != "main_feed" and not await is_member(
        channel_id, actor.type, actor.id
    ):
        raise HTTPException(status_code=403, detail="Not a member of this channel")

    if channel["type"] in ("main_feed", "text") and channel["visibility"] == "public":
        # All users are implicit members; bots only via their explicit rows.
        users = await db.fetch_all(
            "SELECT id, display_name, status, avatar_path FROM users ORDER BY id"
        )
    else:
        users = await db.fetch_all(
            """SELECT u.id, u.display_name, u.status, u.avatar_path
               FROM channel_members cm JOIN users u ON u.id = cm.member_id
               WHERE cm.channel_id = ? AND cm.member_type = 'user'
               ORDER BY u.id""",
            (channel_id,),
        )
    bots = await db.fetch_all(
        """SELECT b.id, b.name, b.avatar_path
           FROM channel_members cm JOIN bots b ON b.id = cm.member_id
           WHERE cm.channel_id = ? AND cm.member_type = 'bot'
           ORDER BY b.id""",
        (channel_id,),
    )
    # Local import: media imports messages imports channels (see messages.py's
    # _attachment_url for the same dodge).
    from .media import bot_avatar_url, user_avatar_url

    return [
        MemberOut(
            type="user",
            id=u["id"],
            name=u["display_name"],
            status=u["status"],
            avatar_path=u["avatar_path"],
            avatar_url=user_avatar_url(u["id"], u["avatar_path"]),
        )
        for u in users
    ] + [
        MemberOut(
            type="bot",
            id=b["id"],
            name=b["name"],
            avatar_path=b["avatar_path"],
            avatar_url=bot_avatar_url(b["id"], b["avatar_path"]),
        )
        for b in bots
    ]


# ---------------------------------------------------------------------------
# Membership verbs: invite / join / leave / kick
#
# These operate on PRIVATE channels. A public channel has no membership to
# manage — every user is already a member of it by construction — so the verbs
# answer truthfully (400) instead of pretending to do something.
# ---------------------------------------------------------------------------

def _require_owner(channel: dict[str, Any], user: User) -> None:
    """RULED by plink, 2026-08-12: only the channel owner (creator) may invite.

    Applied to kick and to bot-adds on a private channel for the same reason —
    all three hand out (or take away) read access to the channel's content.
    Deletion is deliberately NOT one of these: see _require_owner_or_admin.
    """
    if channel["created_by"] != user.id:
        raise HTTPException(
            status_code=403,
            detail="Only the channel's owner may change who is in it",
        )


def _require_private(channel: dict[str, Any]) -> None:
    """Membership is only a thing you can change on a private channel."""
    if channel["type"] != "text" or channel["visibility"] != "private":
        raise HTTPException(
            status_code=400,
            detail=(
                "This channel has no membership list to change: DMs are fixed "
                "pairs, and everyone in the house is already a member of a "
                "public channel"
            ),
        )


async def _publish_member_event(
    kind: str,
    channel: dict[str, Any],
    member_type: MemberType,
    member_id: int,
    by_user_id: Optional[int],
) -> None:
    """member_add / member_remove on the bus (WS fans it out to the channel's
    members plus the affected member — see ws.handle_bus_event).

    Used for humans (invite / leave / kick) and for bots (POST/DELETE
    .../bots) alike, so a client has one frame shape to handle rather than two.
    Note `channel["name"]` is None for a DM, which only the bot verbs can
    reach — a human membership verb requires a private text channel.

    `by_user_id` is WHO did it, which the subject and the room both need in
    order to say "alice added bob" rather than "bob appeared". On /leave the
    actor and the subject are the same person — that is the honest answer, not
    a missing one, so it is filled in. It is Optional only because a future
    system-initiated membership change would genuinely have no acting user.
    """
    await events.publish(
        {
            "type": kind,
            "channel_id": channel["id"],
            "member_type": member_type,
            "member_id": member_id,
            "by_user_id": by_user_id,
            "channel": {
                "id": channel["id"],
                "type": channel["type"],
                "name": channel["name"],
                "visibility": channel["visibility"],
            },
        }
    )


@router.post("/channels/{channel_id}/invite")
async def invite_to_channel(
    channel_id: int, body: MemberRef, user: CurrentUser
) -> dict[str, bool]:
    """Add a user to a private channel. Owner only; idempotent."""
    channel = await _get_channel(channel_id)
    _require_private(channel)
    _require_owner(channel, user)
    target = await db.fetch_one("SELECT id FROM users WHERE id = ?", (body.user_id,))
    if target is None:
        raise HTTPException(status_code=404, detail="User not found")
    cur = await db.execute(
        """INSERT OR IGNORE INTO channel_members (channel_id, member_type, member_id)
           VALUES (?, 'user', ?)""",
        (channel_id, body.user_id),
    )
    added = cur.rowcount > 0
    if added:
        await _publish_member_event("member_add", channel, "user", body.user_id, user.id)
    return {"ok": True, "added": added}


@router.post("/channels/{channel_id}/join")
async def join_channel(channel_id: int, user: CurrentUser) -> dict[str, bool]:
    """Join a channel — which, in this house, you almost always already have.

    Public main_feed/text channels include every user implicitly, so joining
    one is a no-op that reports `joined: false`. Private channels are
    invite-only (RULED: only the owner adds members), so a non-member asking
    to join gets a truthful 403 rather than a way in.
    """
    channel = await _get_channel(channel_id)
    if await is_member(channel_id, "user", user.id):
        return {"ok": True, "joined": False}  # already in — nothing to do
    if channel["visibility"] == "private":
        raise HTTPException(
            status_code=403,
            detail=f"#{channel['name']} is invite-only — ask its owner",
        )
    raise HTTPException(status_code=403, detail="Not a member of this channel")


@router.post("/channels/{channel_id}/leave")
async def leave_channel(channel_id: int, user: CurrentUser) -> dict[str, bool]:
    """Leave a private channel. Anyone may leave, including the owner.

    Idempotent: leaving a channel you are not in reports `left: false`.
    Leaving takes your last_read_seq with it — the row IS the membership.
    """
    channel = await _get_channel(channel_id)
    _require_private(channel)
    cur = await db.execute(
        """DELETE FROM channel_members
           WHERE channel_id = ? AND member_type = 'user' AND member_id = ?""",
        (channel_id, user.id),
    )
    left = cur.rowcount > 0
    if left:
        await _publish_member_event("member_remove", channel, "user", user.id, user.id)
    return {"ok": True, "left": left}


@router.post("/channels/{channel_id}/kick")
async def kick_from_channel(
    channel_id: int, body: MemberRef, user: CurrentUser
) -> dict[str, bool]:
    """Remove a user from a private channel. Owner only; idempotent.

    The owner cannot be kicked (their own way out is /leave), so a channel
    never ends up with an owner it has evicted.
    """
    channel = await _get_channel(channel_id)
    _require_private(channel)
    _require_owner(channel, user)
    if body.user_id == channel["created_by"]:
        raise HTTPException(
            status_code=400,
            detail="The channel's owner cannot be kicked (use leave)",
        )
    cur = await db.execute(
        """DELETE FROM channel_members
           WHERE channel_id = ? AND member_type = 'user' AND member_id = ?""",
        (channel_id, body.user_id),
    )
    removed = cur.rowcount > 0
    if removed:
        await _publish_member_event("member_remove", channel, "user", body.user_id, user.id)
    return {"ok": True, "removed": removed}


# ---------------------------------------------------------------------------
# Bot membership management (flat access among members: any user for public
# main_feed and text channels — every user is an implicit member there — DM
# participants only for DMs; a bot in a DM streams that DM, so only its members
# may grant/revoke that access. In a PRIVATE channel, handing a bot the stream
# is an invite by another name, so it is the owner's call alone — the wall is
# the same for bots, with no carve in either direction.)
#
# Both endpoints publish member_add / member_remove, in the same frame shape a
# human invite or kick produces, with member_type "bot". This is the FIRST time
# a bot's arrival in (or departure from) a channel is visible live: before it,
# a bot simply started talking one day and clients only learned it was a member
# by refetching. Fan-out is the ordinary members-only rule, so a DM's bot events
# reach exactly its two participants (plus the bot itself, as the subject).
# ---------------------------------------------------------------------------

async def _require_bot_manage_access(channel_id: int, user: User) -> dict[str, Any]:
    channel = await _get_channel(channel_id)
    if channel["type"] != "main_feed" and not await is_member(
        channel_id, "user", user.id
    ):
        raise HTTPException(status_code=403, detail="Not a member of this channel")
    if channel["visibility"] == "private":
        _require_owner(channel, user)
    return channel


@router.post("/channels/{channel_id}/bots")
async def add_bot_to_channel(
    channel_id: int, body: BotRef, user: CurrentUser
) -> dict[str, bool]:
    channel = await _require_bot_manage_access(channel_id, user)
    bot = await db.fetch_one("SELECT id FROM bots WHERE id = ?", (body.bot_id,))
    if bot is None:
        raise HTTPException(status_code=404, detail="Bot not found")
    cur = await db.execute(
        """INSERT OR IGNORE INTO channel_members (channel_id, member_type, member_id)
           VALUES (?, 'bot', ?)""",
        (channel_id, body.bot_id),
    )
    added = cur.rowcount > 0
    if added:
        # Only on a real insert: INSERT OR IGNORE makes a repeat add a no-op,
        # and a no-op is not news — same silence a repeat invite keeps.
        await _publish_member_event("member_add", channel, "bot", body.bot_id, user.id)
    return {"ok": True, "added": added}


@router.delete("/channels/{channel_id}/bots/{bot_id}")
async def remove_bot_from_channel(
    channel_id: int, bot_id: int, user: CurrentUser
) -> dict[str, bool]:
    channel = await _require_bot_manage_access(channel_id, user)
    cur = await db.execute(
        """DELETE FROM channel_members
           WHERE channel_id = ? AND member_type = 'bot' AND member_id = ?""",
        (channel_id, bot_id),
    )
    removed = cur.rowcount > 0
    if removed:
        # The removed bot is in the `also` recipient set (ws._send_to_members),
        # so it hears its own eviction — the last frame it gets for this
        # channel, and its cue to stop expecting traffic.
        await _publish_member_event("member_remove", channel, "bot", bot_id, user.id)
    return {"ok": True, "removed": removed}
