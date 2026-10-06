"""Admin chibi picker: list a pack's faces, set or remove a tag's alias, re-point one bot message's chibi.

Every alias change is announced in #custodian by the system bot, in a line that can summon nobody.
"""

import json
import logging
import re
from pathlib import Path
from typing import Annotated, Any, Iterable, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from .. import db, events
from ..config import get_settings
from ..models import User
from ..services import chibi, chibi_aliases as aliases
from ..services.emotion_match import normalize
from .auth import get_admin_user
from .messages import _require_message, deliver_message, message_payload
from .slash import SYSTEM_BOT_NAME

logger = logging.getLogger(__name__)

router = APIRouter()

AdminUser = Annotated[User, Depends(get_admin_user)]


class AliasBody(BaseModel):
    target: str = Field(min_length=1, max_length=200)


class EmoteBody(BaseModel):
    tag: str = Field(min_length=1, max_length=200)
    target: str = Field(min_length=1, max_length=200)
    index: Optional[int] = Field(default=None, ge=0)


def _pack(pack: str) -> Path:
    try:
        return aliases.pack_dir(pack)
    except LookupError:
        raise HTTPException(status_code=404, detail="Chibi pack not found") from None


def _bad(err: aliases.AliasError) -> HTTPException:
    return HTTPException(status_code=400, detail=str(err))


def _shown(state: aliases.Current) -> str:
    if state.source != "ladder":
        return state.face.name if state.face else "no face"
    return f"ladder ({state.face.name if state.face else 'no face'})"


def _defang(text: str, residents: dict[str, int]) -> str:
    if not residents:
        return text
    names = "|".join(re.escape(n) for n in sorted(residents, key=len, reverse=True))
    pattern = re.compile(rf"(?<!\w)@?({names})(?!\w)", re.IGNORECASE)
    return pattern.sub(lambda m: f"bot {residents[m.group(1).lower()]}", text)


def custodian_line(
    pack: str, tag: str, old: str, new: str, admin: str,
    bots: Iterable[tuple[int, str]],
) -> str:
    """A resident wakes on her name or an @, so the line carries neither: names become bot ids."""
    residents = {name.lower(): bot_id for bot_id, name in bots
                 if name and name != SYSTEM_BOT_NAME}
    label = _defang(pack, residents)
    if label != pack:
        label += " pack"
    fields = [_defang(part, residents) for part in (tag, old, new, admin)]
    line = 'chibi alias, {}: "{}" {} → {} by {}'.format(label, *fields)
    line = "".join(" " if ch < " " or ch == "\x7f" else ch for ch in line)
    return line.replace("@", "")


async def _announce(pack: str, tag: str, old: str, new: str, admin: User) -> None:
    channel_id = get_settings().CUSTODIAN_CHANNEL_ID
    try:
        if await db.fetch_one("SELECT id FROM channels WHERE id = ?", (channel_id,)) is None:
            logger.warning("chibi alias: no channel %s to announce in", channel_id)
            return
        rows = await db.fetch_all("SELECT id, name FROM bots")
        system = next(r["id"] for r in rows if r["name"] == SYSTEM_BOT_NAME)
        line = custodian_line(pack, tag, old, new, admin.username,
                              [(r["id"], r["name"]) for r in rows])
        await deliver_message(channel_id, "bot", system, line)
    except Exception:
        logger.exception("chibi alias: announcing in channel %s failed", channel_id)


@router.get("/chibi/{pack}/faces")
async def list_faces(pack: str, admin: AdminUser) -> list[dict[str, str]]:
    name = _pack(pack).name
    return [
        {"category": f.category, "name": f.name,
         "url": f"/chibi/{name}/{f.category}/{f.file}"}
        for f in aliases.faces(pack)
    ]


@router.get("/chibi/{pack}/aliases")
async def get_alias(
    pack: str, admin: AdminUser, tag: str = Query(min_length=1, max_length=200)
) -> dict[str, Any]:
    _pack(pack)
    state = aliases.current(pack, tag)
    return {
        "tag": tag,
        "face": state.face.name if state.face else None,
        "source": state.source,
        "alias": list(state.targets) if state.targets else None,
    }


@router.put("/chibi/{pack}/aliases/{tag}")
async def put_alias(pack: str, tag: str, body: AliasBody, admin: AdminUser) -> dict[str, Any]:
    name = _pack(pack).name
    before = aliases.current(pack, tag)
    if before.source == "name":
        raise HTTPException(
            status_code=400,
            detail=f'"{tag}" is a face\'s own name in this pack; an alias cannot override it',
        )
    try:
        written, face, changed = aliases.set_alias(pack, tag, body.target)
    except aliases.AliasError as err:
        raise _bad(err) from None
    if changed:
        logger.info("chibi alias: %s set %r -> %s in pack %s (was %s)",
                    admin.username, written, face.name, name, _shown(before))
        await _announce(name, written, _shown(before), face.name, admin)
    return {"tag": written, "target": face.name, "previous": _shown(before),
            "changed": changed}


@router.delete("/chibi/{pack}/aliases/{tag}")
async def delete_alias(pack: str, tag: str, admin: AdminUser) -> dict[str, Any]:
    name = _pack(pack).name
    before = aliases.current(pack, tag)
    try:
        written = aliases.writable_tag(tag)
        removed = aliases.remove_alias(pack, tag)
    except aliases.AliasError as err:
        raise _bad(err) from None
    if not removed:
        raise HTTPException(status_code=404, detail=f'No alias for "{written}" in this pack')
    after = aliases.current(pack, tag)
    logger.info("chibi alias: %s removed %r in pack %s (was %s, now %s)",
                admin.username, written, name, _shown(before), _shown(after))
    await _announce(name, written, _shown(before), _shown(after), admin)
    return {"tag": written, "face": after.face.name if after.face else None}


@router.patch("/messages/{message_id}/emote")
async def repoint_emote(message_id: int, body: EmoteBody, admin: AdminUser) -> dict[str, Any]:
    row = await _require_message(message_id)
    if row["deleted_at"] is not None:
        raise HTTPException(status_code=409, detail="Message is deleted")
    if row["author_type"] != "bot":
        raise HTTPException(status_code=400, detail="Only a bot's message carries a chibi")
    bot = await db.fetch_one("SELECT chibi_pack FROM bots WHERE id = ?", (row["author_id"],))
    pack = bot["chibi_pack"] if bot else None
    if not pack or chibi.pack_path(pack) is None:
        raise HTTPException(status_code=400, detail="This bot has no chibi pack")

    tags = [normalize(t) for t in aliases.tags_in(row["content"])]
    wanted = normalize(body.tag)
    if body.index is not None:
        index = body.index if body.index < len(tags) and tags[body.index] == wanted else -1
    else:
        index = tags.index(wanted) if wanted in tags else -1
    if index < 0:
        raise HTTPException(status_code=400, detail="That tag is not in this message")
    try:
        face = aliases.face_for(pack, body.target)
    except aliases.AliasError as err:
        raise _bad(err) from None

    refs = json.loads(row["emote_refs"] or "[]")
    shown = sum(1 for r in refs if isinstance(r, str) and r.startswith("chibi:"))
    if len(tags) > 1 and shown < len(tags):
        raise HTTPException(
            status_code=409,
            detail="Some tags in this message found no face, so which chibi belongs to "
                   "which tag is unknown; the alias still applies to new messages",
        )
    updated = aliases.repoint(refs, index, face.ref(aliases.pack_dir(pack).name))
    if updated is None:
        raise HTTPException(
            status_code=409, detail="An earlier tag in this message has no face; fix that one first"
        )
    if updated != refs:
        await db.execute(
            "UPDATE messages SET emote_refs = ? WHERE id = ?", (json.dumps(updated), message_id)
        )
    payload = await message_payload(await _require_message(message_id))
    if updated != refs:
        await events.publish(
            {"type": "message_edit", "channel_id": row["channel_id"], "message": payload}
        )
    return payload
