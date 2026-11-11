"""GET /bot/attachments/{id}: one uploaded file, in the shape a bot may read.

A file is never easier to reach than its message: the reading bot must be a
member of the file's channel, and then `visible_to_bots` decides the file.
"""

import logging
from typing import Annotated, Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from .. import db
from ..config import get_settings
from ..services import bot_files
from .approval import RESIDENT_PRINCIPAL_RE
from .auth import Actor, get_actor
from .channels import is_member

logger = logging.getLogger(__name__)

router = APIRouter()

MAX_SEAT_LABEL_CHARS = 64


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
    if len(on_behalf_of) > MAX_SEAT_LABEL_CHARS or not RESIDENT_PRINCIPAL_RE.fullmatch(
        on_behalf_of
    ):
        return None
    rows = await db.fetch_all(
        "SELECT id FROM bots WHERE lower(name) = ?", (on_behalf_of.removeprefix("res-"),)
    )
    return rows[0]["id"] if len(rows) == 1 else None


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
