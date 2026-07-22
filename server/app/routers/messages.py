"""Messages module (WP4): create/edit/delete, history & backfill, search.

Endpoints:
    POST   /channels/{id}/messages  (actor) — create; per-channel seq in a transaction
    PATCH  /messages/{id}           (actor) — edit, author only; flags merged, never removed
    DELETE /messages/{id}           (actor) — soft delete, author only
    GET    /channels/{id}/messages  (actor) — scrollback (`before_seq`+`limit`, newest-first)
                                              or backfill (`from_seq`, ascending,
                                              current-state: edits applied, deleted
                                              messages as tombstones {id, seq, deleted})
    GET    /search?q=               (actor) — FTS5, membership-scoped, excludes deleted;
                                              optional after=/before= ISO-8601 bounds;
                                              bot results additionally exclude
                                              privacy-hidden messages

Exported helper (reused by WP5 WS backfill / WP6 media):
    message_payload(row) -> dict — full materialized payload for a messages-table
    row: author display info, parsed privacy_flags/emote_refs, attachments joined
    (with signed URLs once WP6's media.sign_media_url exists).

Bus events (published AFTER commit, full materialized payloads):
    {"type": "message_create"|"message_edit", "channel_id": int, "message": payload}
    {"type": "message_delete", "channel_id": int, "id": int, "seq": int}

Privacy integration (WP5 not landed yet — ImportError-guarded):
    - privacy.detect_flags(content) merged into user-authored messages on
      create/edit; bots set their own flags explicitly. Flags are only ever
      ADDED, never removed.
    - Bot reads exclude secret/off_the_record messages entirely (no tombstone);
      see _hidden_from_bots for the WP5 handoff TODO.
"""

import inspect
import json
from typing import Annotated, Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field, model_validator

from .. import db, events, privacy
from ..models import User
from . import channels
from .auth import Actor, get_actor, get_current_user

router = APIRouter()

CurrentUser = Annotated[User, Depends(get_current_user)]
CurrentActor = Annotated[Actor, Depends(get_actor)]

SEARCH_LIMIT = 50

# Hard cap on message content, in characters (BL-D6).
#
# Why 16000: Discord ships 2000 (4000 for Nitro), but this house is explicitly
# friendlier to long bot output — a resident posts whole build reports, and the
# largest cap anywhere else in the stack is the harness's 4000-char file-proposal
# contract (harness/consolidation/poster.py PROPOSAL_TEXT_CAP = 3900). 16000
# leaves ~4x headroom over that so no legitimate bot post is broken, while still
# turning "a 2MB /backlog stored verbatim" into a 422 instead of a row. It is
# also comfortably under SQLite's default 1e9-byte string limit and small enough
# that a full 200-message history page stays a few MB.
#
# Server-authored messages (slash replies via deliver_message) do not pass
# through this pydantic model, so their own rendering must stay bounded — see
# slash._render_list.
MAX_MESSAGE_CHARS = 16000

# Same idea for the free-form JSON fields on a message create. Without this,
# `content` is capped but `emote_refs` / `privacy_flags` are an uncapped side
# channel into the same row (both are json.dumps'd straight into the DB).
# 4000 chars is generous: a realistic emote_refs is one or two
# "chibi:pack/Category/File.png" strings, and privacy_flags is a handful of
# booleans.
MAX_METADATA_CHARS = 4000


# ---------------------------------------------------------------------------
# Privacy / chibi / media integration seams (guarded — later WPs fill them in)
# ---------------------------------------------------------------------------

def _detect_flags(content: str) -> dict[str, Any]:
    """Server-side NL trigger detection via WP5's privacy module.

    privacy.py doesn't exist until WP5 lands; until then detection yields {}
    and caller-supplied flags pass through unchanged.
    """
    try:
        from .. import privacy  # WP5
    except ImportError:
        return {}
    return privacy.detect_flags(content) or {}


def _hidden_from_bots(flags: dict[str, Any]) -> bool:
    """True if a message with these privacy flags must never reach a bot.

    Delegates to app/privacy.py (WP5) — the single source of truth for bot
    visibility.
    """
    return privacy.hidden_from_bots(flags)


def _resolve_emotion(bot_chibi_pack: Optional[str], emotion: str) -> Any:
    """Resolve a bot's `emotion` tag to an emote_ref via WP8's chibi service.

    Returns None (ignore silently) if the service isn't built yet or the
    emotion doesn't resolve.
    """
    try:
        from ..services import chibi  # WP8
    except ImportError:
        return None
    try:
        return chibi.resolve(bot_chibi_pack, emotion)
    except Exception:  # unresolvable emotion — spec: ignore silently
        return None


async def _attachment_url(attachment_id: int, variant: str = "display") -> Optional[str]:
    """Signed media URL for one variant via WP6's media module; None until it lands."""
    try:
        from .media import sign_media_url  # WP6
    except ImportError:
        return None
    result = sign_media_url(attachment_id, variant)
    if inspect.isawaitable(result):
        result = await result
    return result


# ---------------------------------------------------------------------------
# Payload building (exported — WS backfill and other WPs reuse this)
# ---------------------------------------------------------------------------

def _avatar_url(author_type: str, author_id: int, avatar_path: Optional[str]) -> Optional[str]:
    """Versioned avatar URL for an author, or None when they have none.

    Local import for the same reason as _attachment_url: media imports this
    module. See media.avatar_version for why the `?v=` matters.
    """
    try:
        from .media import bot_avatar_url, user_avatar_url  # WP6
    except ImportError:
        return None
    return (
        user_avatar_url(author_id, avatar_path)
        if author_type == "user"
        else bot_avatar_url(author_id, avatar_path)
    )


async def _author_info(author_type: str, author_id: int) -> dict[str, Any]:
    if author_type == "user":
        row = await db.fetch_one(
            "SELECT id, username, display_name, avatar_path FROM users WHERE id = ?",
            (author_id,),
        )
        if row is not None:
            return {
                "type": "user",
                "id": row["id"],
                "name": row["display_name"],
                "username": row["username"],
                "avatar_path": row["avatar_path"],
                "avatar_url": _avatar_url("user", row["id"], row["avatar_path"]),
            }
    else:
        row = await db.fetch_one(
            "SELECT id, name, avatar_path FROM bots WHERE id = ?", (author_id,)
        )
        if row is not None:
            return {
                "type": "bot",
                "id": row["id"],
                "name": row["name"],
                "avatar_path": row["avatar_path"],
                "avatar_url": _avatar_url("bot", row["id"], row["avatar_path"]),
            }
    return {
        "type": author_type,
        "id": author_id,
        "name": "unknown",
        "avatar_path": None,
        "avatar_url": None,
    }


async def message_payload(row: dict[str, Any]) -> dict[str, Any]:
    """Full materialized payload for a messages-table row (dict from db helpers).

    Shape:
        {id, channel_id, seq, author_type, author_id,
         author: {type, id, name, username?, avatar_path, avatar_url},
         content, created_at, edited_at, deleted_at, reply_to_id,
         privacy_flags: {}, emote_refs: [], attachments: [
             {id, original_filename, mime_type, size_bytes, width, height,
              url, thumb_url, orig_url}]}

    `url`/`thumb_url`/`orig_url` are signed media URLs (display / thumb /
    preserved original) once WP6's media.sign_media_url exists, else None. The
    three-variant shape matches POST /upload's `_attachment_out`, so a client
    that got an attachment from an upload response and one that got it from
    history see the same keys — that's what lets the image modal offer "view
    original" (the display variant is a re-encoded WebP; the original keeps the
    source format and metadata).

    Attachment rows may be pre-linked by the upload flow; the join always runs
    (empty list when none exist).
    """
    attachments = []
    for att in await db.fetch_all(
        "SELECT * FROM attachments WHERE message_id = ? ORDER BY id", (row["id"],)
    ):
        attachments.append(
            {
                "id": att["id"],
                "original_filename": att["original_filename"],
                "mime_type": att["mime_type"],
                "size_bytes": att["size_bytes"],
                "width": att["width"],
                "height": att["height"],
                "url": await _attachment_url(att["id"], "display"),
                "thumb_url": await _attachment_url(att["id"], "thumb"),
                "orig_url": await _attachment_url(att["id"], "orig"),
            }
        )
    return {
        "id": row["id"],
        "channel_id": row["channel_id"],
        "seq": row["seq"],
        "author_type": row["author_type"],
        "author_id": row["author_id"],
        "author": await _author_info(row["author_type"], row["author_id"]),
        "content": row["content"],
        "created_at": row["created_at"],
        "edited_at": row["edited_at"],
        "deleted_at": row["deleted_at"],
        "reply_to_id": row["reply_to_id"],
        "privacy_flags": json.loads(row["privacy_flags"] or "{}"),
        "emote_refs": json.loads(row["emote_refs"] or "[]"),
        "attachments": attachments,
    }


def _tombstone(row: dict[str, Any]) -> dict[str, Any]:
    return {"id": row["id"], "seq": row["seq"], "deleted": True}


# ---------------------------------------------------------------------------
# Small shared helpers
# ---------------------------------------------------------------------------

def _merge_flags(*flag_dicts: dict[str, Any]) -> dict[str, Any]:
    """Union of truthy flags. Falsy values are dropped, so a caller can never
    UNSET a flag by sending e.g. {"secret": false} — flags only accumulate."""
    merged: dict[str, Any] = {}
    for flags in flag_dicts:
        for key, value in (flags or {}).items():
            if value:
                merged[key] = value
    return merged


async def _require_channel(channel_id: int) -> dict[str, Any]:
    channel = await db.fetch_one("SELECT * FROM channels WHERE id = ?", (channel_id,))
    if channel is None:
        raise HTTPException(status_code=404, detail="Channel not found")
    return channel


async def _require_message(message_id: int) -> dict[str, Any]:
    row = await db.fetch_one("SELECT * FROM messages WHERE id = ?", (message_id,))
    if row is None:
        raise HTTPException(status_code=404, detail="Message not found")
    return row


async def deliver_message(
    channel_id: int,
    author_type: str,
    author_id: int,
    content: str,
    *,
    flags: Optional[dict[str, Any]] = None,
    emote_refs: Optional[list[Any]] = None,
    reply_to_id: Optional[int] = None,
) -> dict[str, Any]:
    """Allocate a per-channel seq, insert the message, publish message_create.

    The shared insert path used by the HTTP create endpoint AND by server-side
    features that author messages directly (WP-L2's slash framework posts its
    server-rendered replies through here, as the seeded 'system' bot). It does
    NO membership/authorization checks — callers own that. Returns the full
    materialized payload. Does NOT run slash dispatch, so it never recurses.
    """
    async with db.transaction():
        seq_row = await db.fetch_one(
            "SELECT COALESCE(MAX(seq), 0) + 1 AS next_seq FROM messages WHERE channel_id = ?",
            (channel_id,),
        )
        assert seq_row is not None
        cur = await db.execute(
            """INSERT INTO messages
                   (channel_id, seq, author_type, author_id, content, created_at,
                    reply_to_id, privacy_flags, emote_refs)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                channel_id,
                seq_row["next_seq"],
                author_type,
                author_id,
                content,
                db.utc_now(),
                reply_to_id,
                json.dumps(flags or {}),
                json.dumps(emote_refs or []),
            ),
            commit=False,
        )
        message_id = cur.lastrowid

    row = await _require_message(message_id)
    payload = await message_payload(row)
    # Published AFTER commit so subscribers (WS hub, push) only ever see
    # durable messages.
    await events.publish(
        {"type": "message_create", "channel_id": channel_id, "message": payload}
    )
    return payload


def _require_author(row: dict[str, Any], actor: Actor) -> None:
    if row["author_type"] != actor.type or row["author_id"] != actor.id:
        raise HTTPException(status_code=403, detail="Not the author of this message")


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

class MessageCreate(BaseModel):
    # max_length is the intake wall for oversized content (BL-D6): pydantic
    # rejects with a 422 before anything is persisted, indexed by FTS5, or
    # fanned out on the bus.
    content: str = Field(max_length=MAX_MESSAGE_CHARS)
    reply_to_id: Optional[int] = None
    privacy_flags: dict[str, Any] = Field(default_factory=dict)
    # bot authors only; resolved via chibi (WP8)
    emotion: Optional[str] = Field(default=None, max_length=200)
    emote_refs: Optional[list[Any]] = None  # bot authors only; stored as-is

    @model_validator(mode="after")
    def _bound_metadata(self) -> "MessageCreate":
        """Keep the free-form JSON fields from becoming an uncapped side channel."""
        for name, value in (
            ("privacy_flags", self.privacy_flags),
            ("emote_refs", self.emote_refs),
        ):
            if value is None:
                continue
            try:
                encoded = json.dumps(value)
            except (TypeError, ValueError):
                raise ValueError(f"{name} must be JSON-serializable") from None
            if len(encoded) > MAX_METADATA_CHARS:
                raise ValueError(
                    f"{name} exceeds {MAX_METADATA_CHARS} serialized characters"
                )
        return self


class MessageEdit(BaseModel):
    # Same cap as create — an edit must not be a way around it.
    content: str = Field(max_length=MAX_MESSAGE_CHARS)


# ---------------------------------------------------------------------------
# POST /channels/{id}/messages
# ---------------------------------------------------------------------------

@router.post("/channels/{channel_id}/messages")
async def create_message(
    channel_id: int, body: MessageCreate, actor: CurrentActor
) -> dict[str, Any]:
    await _require_channel(channel_id)
    if not await channels.is_member(channel_id, actor.type, actor.id):
        raise HTTPException(status_code=403, detail="Not a member of this channel")

    if body.reply_to_id is not None:
        target = await db.fetch_one(
            "SELECT channel_id FROM messages WHERE id = ?", (body.reply_to_id,)
        )
        if target is None or target["channel_id"] != channel_id:
            raise HTTPException(
                status_code=400, detail="reply_to message not found in this channel"
            )

    # Caller-supplied flags merged with server-side NL detection. Detection
    # runs on user messages (bots set their own flags explicitly). Merge only
    # ever adds flags.
    flags = _merge_flags(
        body.privacy_flags,
        _detect_flags(body.content) if actor.type == "user" else {},
    )

    # emote_refs / emotion: bot authors only; silently ignored for users.
    emote_refs: list[Any] = []
    if actor.type == "bot":
        if body.emote_refs:
            emote_refs = list(body.emote_refs)
        if body.emotion:
            assert actor.bot is not None
            ref = _resolve_emotion(actor.bot.chibi_pack, body.emotion)
            if ref is not None:
                emote_refs.append(ref)

    # Per-channel seq allocation, insert, and post-commit publish are shared
    # with server-authored messages — see deliver_message. BEGIN IMMEDIATE on
    # the single shared connection serializes writers, so MAX(seq)+1 is race-free
    # (WP1 note); UNIQUE(channel_id, seq) backstops.
    payload = await deliver_message(
        channel_id,
        actor.type,
        actor.id,
        body.content,
        flags=flags,
        emote_refs=emote_refs,
        reply_to_id=body.reply_to_id,
    )

    # Slash-command dispatch (WP-L2): the user's message is persisted as normal
    # chat above; if its content is a registered /command, the server handles it
    # and posts its own reply. Unknown /commands (e.g. /shrug) pass through
    # untouched. Local import avoids a messages<->slash import cycle.
    from . import slash

    await slash.dispatch(channel_id, body.content, actor, flags)

    return payload


# ---------------------------------------------------------------------------
# PATCH /messages/{id}
# ---------------------------------------------------------------------------

@router.patch("/messages/{message_id}")
async def edit_message(
    message_id: int, body: MessageEdit, actor: CurrentActor
) -> dict[str, Any]:
    row = await _require_message(message_id)
    _require_author(row, actor)
    if row["deleted_at"] is not None:
        raise HTTPException(status_code=409, detail="Message is deleted")

    # Re-run detection on the new content and MERGE — existing flags are never
    # removed by an edit (privacy only ratchets up).
    flags = _merge_flags(
        json.loads(row["privacy_flags"] or "{}"),
        _detect_flags(body.content) if actor.type == "user" else {},
    )
    await db.execute(
        "UPDATE messages SET content = ?, edited_at = ?, privacy_flags = ? WHERE id = ?",
        (body.content, db.utc_now(), json.dumps(flags), message_id),
    )
    updated = await _require_message(message_id)
    payload = await message_payload(updated)
    await events.publish(
        {"type": "message_edit", "channel_id": row["channel_id"], "message": payload}
    )
    return payload


# ---------------------------------------------------------------------------
# DELETE /messages/{id}
# ---------------------------------------------------------------------------

@router.delete("/messages/{message_id}")
async def delete_message(message_id: int, actor: CurrentActor) -> dict[str, bool]:
    row = await _require_message(message_id)
    _require_author(row, actor)
    if row["deleted_at"] is not None:
        return {"ok": True}  # idempotent; no duplicate event
    await db.execute(
        "UPDATE messages SET deleted_at = ? WHERE id = ?", (db.utc_now(), message_id)
    )
    await events.publish(
        {
            "type": "message_delete",
            "channel_id": row["channel_id"],
            "id": row["id"],
            "seq": row["seq"],
        }
    )
    return {"ok": True}


# ---------------------------------------------------------------------------
# GET /channels/{id}/messages — scrollback + backfill
# ---------------------------------------------------------------------------

@router.get("/channels/{channel_id}/messages")
async def list_messages(
    channel_id: int,
    actor: CurrentActor,
    before_seq: Optional[int] = Query(default=None, ge=1),
    from_seq: Optional[int] = Query(default=None, ge=0),
    limit: int = Query(default=50, ge=1, le=200),
) -> list[dict[str, Any]]:
    await _require_channel(channel_id)
    if not await channels.is_member(channel_id, actor.type, actor.id):
        raise HTTPException(status_code=403, detail="Not a member of this channel")
    if before_seq is not None and from_seq is not None:
        raise HTTPException(
            status_code=400, detail="before_seq and from_seq are mutually exclusive"
        )

    if from_seq is not None:
        # Backfill: ascending, current-state semantics (edits applied). Deleted
        # messages appear as tombstones so clients/bots can drop them.
        rows = await db.fetch_all(
            """SELECT * FROM messages WHERE channel_id = ? AND seq >= ?
               ORDER BY seq ASC LIMIT ?""",
            (channel_id, from_seq, limit),
        )
    else:
        # Scrollback: newest-first; soft-deleted hidden from all reads.
        sql = "SELECT * FROM messages WHERE channel_id = ? AND deleted_at IS NULL"
        params: list[Any] = [channel_id]
        if before_seq is not None:
            sql += " AND seq < ?"
            params.append(before_seq)
        sql += " ORDER BY seq DESC LIMIT ?"
        params.append(limit)
        rows = await db.fetch_all(sql, params)

    out: list[dict[str, Any]] = []
    for row in rows:
        if actor.type == "bot" and _hidden_from_bots(
            json.loads(row["privacy_flags"] or "{}")
        ):
            # Flagged messages are entirely absent for bots — not even a
            # tombstone (Architecture §4.2 / §8.2).
            continue
        if row["deleted_at"] is not None:
            out.append(_tombstone(row))  # from_seq mode only; scrollback filters in SQL
        else:
            out.append(await message_payload(row))
    return out


# ---------------------------------------------------------------------------
# GET /search — FTS5, membership-scoped (users and bots), privacy-filtered
# ---------------------------------------------------------------------------

def _fts_match_query(q: str) -> str:
    """Build a safe FTS5 MATCH string from raw user input.

    Every whitespace-separated term is wrapped in double quotes (with embedded
    quotes doubled) so FTS operators/parens in user input are treated as
    literals and can never produce a MATCH syntax error. Terms with no
    alphanumeric characters are dropped (they tokenize to nothing).
    """
    terms = [t for t in q.split() if any(c.isalnum() for c in t)]
    return " ".join('"' + t.replace('"', '""') + '"' for t in terms)


def _validate_iso_date(value: str, param: str) -> str:
    """Loosely validate an ISO-8601 date/datetime query param; 400 on garbage.

    messages.created_at is TEXT like '2026-07-17T12:34:56.789Z', so ISO strings
    compare lexicographically and any valid prefix ('2026-07-17', full
    timestamps, ...) works as a raw string bound — no parsing of stored rows
    needed. datetime.fromisoformat is the loose gate ('Z' accepted on 3.11+).
    """
    from datetime import datetime

    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise HTTPException(
            status_code=400, detail=f"{param} must be an ISO-8601 date/datetime"
        ) from None
    return value


@router.get("/search")
async def search_messages(
    actor: CurrentActor,
    q: str = Query(default=""),
    after: Optional[str] = Query(default=None),
    before: Optional[str] = Query(default=None),
    limit: int = Query(default=SEARCH_LIMIT, ge=1, le=SEARCH_LIMIT),
) -> list[dict[str, Any]]:
    """FTS5 search over the caller's channels.

    Users: all accessible channels (main_feed + text channels implicit, + DMs),
    deleted excluded.
    Bots: explicit-membership channels only, and privacy-hidden messages
    (secret / off_the_record) never appear.
    after/before: optional ISO-8601 bounds on created_at — `after` is inclusive
    (created_at >= after), `before` exclusive (created_at < before). A date-only
    `before=2026-07-17` therefore excludes that whole day.
    limit: result cap, 1..SEARCH_LIMIT (default SEARCH_LIMIT).
    """
    # Validate bounds up front so garbage is a 400 even for empty queries.
    if after is not None:
        after = _validate_iso_date(after, "after")
    if before is not None:
        before = _validate_iso_date(before, "before")
    match = _fts_match_query(q)
    if not match:
        return []
    if actor.type == "user":
        channel_ids = await channels.user_channel_ids(actor.id)
    else:
        # Bots: explicit membership only — no implicit main_feed, no DMs unless
        # the bot was added (Architecture §11).
        channel_ids = await channels.bot_channel_ids(actor.id)
    if not channel_ids:
        return []
    placeholders = ",".join("?" * len(channel_ids))
    sql = f"""SELECT m.*, c.type AS channel_type, c.name AS channel_name
            FROM messages_fts fts
            JOIN messages m ON m.id = fts.rowid
            JOIN channels c ON c.id = m.channel_id
            WHERE messages_fts MATCH ?
              AND m.deleted_at IS NULL
              AND m.channel_id IN ({placeholders})"""
    params: list[Any] = [match, *channel_ids]
    if after is not None:
        sql += " AND m.created_at >= ?"
        params.append(after)
    if before is not None:
        sql += " AND m.created_at < ?"
        params.append(before)
    if actor.type == "bot":
        # Privacy wall (Architecture §8.2/§11): app/privacy.py is the source of
        # truth for bot visibility; this SQL mirrors privacy.hidden_from_bots.
        # The merge logic (_merge_flags) guarantees only TRUTHY flags are ever
        # stored in privacy_flags JSON, so "key absent" (json_extract IS NULL)
        # is exactly "flag not set".
        sql += """
              AND json_extract(m.privacy_flags, '$.secret') IS NULL
              AND json_extract(m.privacy_flags, '$.off_the_record') IS NULL"""
    sql += " ORDER BY m.created_at DESC, m.id DESC LIMIT ?"
    params.append(limit)
    rows = await db.fetch_all(sql, params)
    results: list[dict[str, Any]] = []
    for row in rows:
        # Belt-and-braces: re-check via the canonical policy even though the
        # SQL above already excluded hidden rows for bots.
        if actor.type == "bot" and _hidden_from_bots(
            json.loads(row["privacy_flags"] or "{}")
        ):
            continue
        results.append(
            {
                "message": await message_payload(row),
                "channel": {
                    "id": row["channel_id"],
                    "type": row["channel_type"],
                    "name": row["channel_name"],
                },
            }
        )
    return results
