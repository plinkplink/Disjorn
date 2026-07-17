"""Channels, membership, read state (WP3).

Endpoints:
    GET    /channels                      (user)  — sidebar list: unread counts + last-message snippet
    POST   /dms {user_id}                 (user)  — idempotent get-or-create of the 1:1 DM channel
    PUT    /channels/{id}/read {seq}      (user)  — monotonic last_read_seq upsert (no event published)
    GET    /channels/{id}/members         (actor) — member listing, membership-gated
    POST   /channels/{id}/bots {bot_id}   (user)  — add a bot to a channel (flat access)
    DELETE /channels/{id}/bots/{bot_id}   (user)  — remove a bot from a channel

Exported access-rule helpers (consumed by WP4 messages and WP5 privacy/WS):
    is_member(channel_id, member_type, member_id) -> bool
    user_channel_ids(user_id) -> list[int]      # main_feed implicit
    bot_channel_ids(bot_id)  -> list[int]       # explicit rows only

Membership semantics (Architecture §4.1):
- main_feed implicitly includes ALL users; a channel_members row is created
  lazily only to store last_read_seq (first PUT /channels/{id}/read).
- DM channels have exactly two user members.
- Bots are members of main_feed explicitly (cli.py create-bot inserts the row)
  and NEVER get implicit DM access — they must be added via POST .../bots.
"""

from typing import Annotated, Any, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from .. import db
from ..models import ChannelType, MemberType, User, UserStatus
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

    Users are implicit members of main_feed (no row required). Everything else
    — user in a DM, bot anywhere — requires an explicit channel_members row.
    Unknown channels are never accessible.
    """
    channel = await db.fetch_one("SELECT type FROM channels WHERE id = ?", (channel_id,))
    if channel is None:
        return False
    if member_type == "user" and channel["type"] == "main_feed":
        return True
    row = await db.fetch_one(
        """SELECT 1 FROM channel_members
           WHERE channel_id = ? AND member_type = ? AND member_id = ?""",
        (channel_id, member_type, member_id),
    )
    return row is not None


async def user_channel_ids(user_id: int) -> list[int]:
    """All channel ids the user can access: main_feed (implicit) + explicit rows."""
    rows = await db.fetch_all(
        """SELECT id FROM channels WHERE type = 'main_feed'
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
    id: int
    type: ChannelType
    name: Optional[str] = None          # DMs: the OTHER participant's display name
    dm_user_id: Optional[int] = None    # DMs: the OTHER participant's user id
    unread: int = 0
    last_message: Optional[LastMessage] = None


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
    type: MemberType
    id: int
    name: str
    status: Optional[UserStatus] = None  # users only


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
    main = await db.fetch_one("SELECT * FROM channels WHERE type = 'main_feed'")
    dms = await db.fetch_all(
        """SELECT c.* FROM channels c
           JOIN channel_members cm ON cm.channel_id = c.id
           WHERE c.type = 'dm_1to1' AND cm.member_type = 'user' AND cm.member_id = ?""",
        (user.id,),
    )
    chans = ([main] if main is not None else []) + dms
    if not chans:
        return []

    ids = [c["id"] for c in chans]
    ph = ",".join("?" * len(ids))

    # One aggregate query each — no per-channel N+1.
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

    # main_feed pinned first; DMs by most recent activity (ISO strings sort).
    dms_sorted = sorted(dms, key=activity_ts, reverse=True)
    ordered = ([main] if main is not None else []) + dms_sorted
    return [build(c) for c in ordered]


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
    channel = await _get_channel(channel_id)
    if channel["type"] != "main_feed":
        # Non-main channels require pre-existing membership; the upsert below
        # must never manufacture DM membership.
        if not await is_member(channel_id, "user", user.id):
            raise HTTPException(status_code=403, detail="Not a member of this channel")
    # main_feed: implicit membership — the row is created lazily here, solely
    # to store last_read_seq. Monotonic: never lowered.
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

    if channel["type"] == "main_feed":
        # All users are implicit members; bots only via their explicit rows.
        users = await db.fetch_all(
            "SELECT id, display_name, status FROM users ORDER BY id"
        )
    else:
        users = await db.fetch_all(
            """SELECT u.id, u.display_name, u.status
               FROM channel_members cm JOIN users u ON u.id = cm.member_id
               WHERE cm.channel_id = ? AND cm.member_type = 'user'
               ORDER BY u.id""",
            (channel_id,),
        )
    bots = await db.fetch_all(
        """SELECT b.id, b.name
           FROM channel_members cm JOIN bots b ON b.id = cm.member_id
           WHERE cm.channel_id = ? AND cm.member_type = 'bot'
           ORDER BY b.id""",
        (channel_id,),
    )
    return [
        MemberOut(type="user", id=u["id"], name=u["display_name"], status=u["status"])
        for u in users
    ] + [MemberOut(type="bot", id=b["id"], name=b["name"]) for b in bots]


# ---------------------------------------------------------------------------
# Bot membership management (flat access: any authenticated user)
# ---------------------------------------------------------------------------

@router.post("/channels/{channel_id}/bots")
async def add_bot_to_channel(
    channel_id: int, body: BotRef, user: CurrentUser
) -> dict[str, bool]:
    await _get_channel(channel_id)
    bot = await db.fetch_one("SELECT id FROM bots WHERE id = ?", (body.bot_id,))
    if bot is None:
        raise HTTPException(status_code=404, detail="Bot not found")
    cur = await db.execute(
        """INSERT OR IGNORE INTO channel_members (channel_id, member_type, member_id)
           VALUES (?, 'bot', ?)""",
        (channel_id, body.bot_id),
    )
    return {"ok": True, "added": cur.rowcount > 0}


@router.delete("/channels/{channel_id}/bots/{bot_id}")
async def remove_bot_from_channel(
    channel_id: int, bot_id: int, user: CurrentUser
) -> dict[str, bool]:
    await _get_channel(channel_id)
    cur = await db.execute(
        """DELETE FROM channel_members
           WHERE channel_id = ? AND member_type = 'bot' AND member_id = ?""",
        (channel_id, bot_id),
    )
    return {"ok": True, "removed": cur.rowcount > 0}
