"""GET /bot/attachments/{id}: one uploaded file, in the shape a bot may read."""

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException

from ..services import bot_files
from .auth import Actor, get_actor

router = APIRouter()


@router.get("/bot/attachments/{attachment_id}")
async def read_attachment(
    attachment_id: int, actor: Annotated[Actor, Depends(get_actor)]
) -> dict[str, Any]:
    """404 for an unknown id and a hidden one alike, so hidden ids cannot be enumerated."""
    out = await bot_files.read(attachment_id) if actor.type == "bot" else None
    if out is None:
        raise HTTPException(status_code=404, detail="Not found")
    return out
