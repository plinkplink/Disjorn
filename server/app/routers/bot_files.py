"""GET /bot/attachments/{id}: one uploaded file, in the shape a bot may read.
POST /bot/attachments: one file a resident seat posts as its own bot.

A file is never easier to reach than its message: the reading bot must be a
member of the file's channel, and then `visible_to_bots` decides the file.
"""

import base64
import binascii
import io
import logging
from typing import Annotated, Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, UploadFile
from pydantic import BaseModel, Field
from starlette.datastructures import Headers

from ..config import get_settings
from ..services import bot_files
from . import media
from .auth import Actor, get_actor
from .channels import is_member
from .messages import MAX_MESSAGE_CHARS, Attribution, deliver_message

logger = logging.getLogger(__name__)

router = APIRouter()


async def _reader(actor: Actor, on_behalf_of: Optional[str]) -> Optional[int]:
    """The bot whose channel membership gates this read, or None for nobody.

    Only a relay bot may name a resident seat, and `res-<name>` is the bot
    called `<name>`; any other caller sending the field reads nothing.
    """
    if actor.type != "bot" or actor.bot is None:
        return None
    if on_behalf_of is None:
        return actor.id
    if actor.bot.name not in get_settings().FILES_RELAY_BOT_NAMES:
        return None
    return await bot_files.seat_bot(on_behalf_of)


@router.get("/bot/attachments/{attachment_id}")
async def read_attachment(
    attachment_id: int,
    actor: Annotated[Actor, Depends(get_actor)],
    on_behalf_of: Optional[str] = Query(default=None),
) -> dict[str, Any]:
    """404 for every refusal, so an id a bot may not read looks like one that does not exist."""
    reader, out = None, None
    try:
        reader = await _reader(actor, on_behalf_of)
        channel_id = await bot_files.channel_of(attachment_id)
        if (
            reader is not None
            and channel_id is not None
            and await is_member(channel_id, "bot", reader)
        ):
            out = await bot_files.read(attachment_id)
    except Exception:
        out = None
    logger.info(
        "bot attachment read: caller=%s:%s reader_bot=%s attachment=%s %s",
        actor.type,
        actor.id,
        reader,
        attachment_id,
        "allowed" if out is not None else "refused",
    )
    if out is None:
        raise HTTPException(status_code=404, detail="Not found")
    return out


class FilePost(BaseModel):
    on_behalf_of: str
    channel_id: int
    filename: str = Field(min_length=1, max_length=255)
    data: str
    text: str = Field(default="", max_length=MAX_MESSAGE_CHARS)
    daily_quota_bytes: int = Field(ge=0)
    idempotency_key: Optional[str] = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    attribution: Optional[Attribution] = None


def _attribution(body: FilePost) -> dict[str, Any]:
    """What the seat says produced the file, never verified: the relay cannot check it."""
    said = body.attribution
    if said is None or (said.model is None and said.summoner is None):
        return {}
    return {"model": said.model, "verified": False, "summoner": said.summoner}


async def _post(poster: Optional[int], body: FilePost) -> dict[str, Any]:
    """Every check, in the spec's order, then the write; nothing is stored before the last check.

    A key this bot already posted under is answered with that post, before any check.
    """
    if poster is None:
        raise bot_files.Refused("no seat to post as")
    if body.idempotency_key is not None:
        earlier = await bot_files.posted_before(poster, body.idempotency_key)
        if earlier is not None:
            return earlier
    await bot_files.check_channel(poster, body.channel_id, "seat")
    mime = bot_files.postable_mime(body.filename)
    if mime is None:
        raise bot_files.Refused("a bot may not post a file of this type")
    try:
        data = base64.b64decode(body.data, validate=True)
    except (binascii.Error, ValueError):
        raise bot_files.Refused("file data is not base64") from None
    size, cap = len(data), get_settings().MAX_UPLOAD_BYTES
    if not 0 < size <= cap:
        raise bot_files.Refused(f"a posted file must be 1 to {cap} bytes")
    await bot_files.check_quota(poster, size, body.daily_quota_bytes)
    if await bot_files.matches_hidden(data, poster):
        raise bot_files.Refused("this file matches an upload that bots may not read")

    bot = Actor(type="bot", id=poster)
    upload = UploadFile(
        io.BytesIO(data), filename=body.filename, headers=Headers({"content-type": mime})
    )
    staged = await media.upload_files(bot, files=[upload], message_id=None, message_id_q=None)
    attachment_id = staged["attachments"][0]["id"]
    try:
        message = await deliver_message(
            body.channel_id, "bot", poster, body.text, attribution=_attribution(body)
        )
        await media.claim_attachments(
            media.ClaimRequest(attachment_ids=[attachment_id], message_id=message["id"]), bot
        )
    except BaseException:
        await bot_files.discard_staged(attachment_id)
        raise
    if body.idempotency_key is not None:
        try:
            await bot_files.remember_post(
                poster, body.idempotency_key, attachment_id, message["id"]
            )
        except Exception:
            logger.exception("bot attachment post: no key kept for attachment %s", attachment_id)
    return {
        "attachment_id": attachment_id,
        "message_id": message["id"],
        "channel_id": body.channel_id,
        "seq": message["seq"],
        "size_bytes": size,
    }


@router.post("/bot/attachments")
async def post_attachment(
    body: FilePost, actor: Annotated[Actor, Depends(get_actor)]
) -> dict[str, Any]:
    """A relay bot posts a file as the bot of the seat it names; nobody else posts here.

    `on_behalf_of` is a required string, so `_reader` never answers with the caller itself.
    One post at a time, so a retry that arrives during its first attempt waits for that answer.
    """
    poster, out, why = None, None, "error"
    try:
        poster = await _reader(actor, body.on_behalf_of)
        async with bot_files.post_lock:
            out = await _post(poster, body)
    except bot_files.Refused as exc:
        why = str(exc)
        raise HTTPException(status_code=403, detail=why) from None
    finally:
        logger.info(
            "bot attachment post: caller=%s:%s poster_bot=%s channel=%s attachment=%s %s",
            actor.type,
            actor.id,
            poster,
            body.channel_id,
            out and out["attachment_id"],
            "allowed" if out is not None else f"refused ({why})",
        )
    return out
