"""Bot cosmetics admin surface (chibi pack + avatar).

Closes the DEFERRED item "No admin surface for bot cosmetics": assigning a
`bots.chibi_pack` or a bot avatar used to mean hand-editing the DB. The same
knobs are also available offline via `cli.py create-bot --chibi-pack`.

    GET   /bots                     (any actor)  list bots (public Bot shape)
    GET   /bots/{id}                (any actor)  one bot (public Bot shape)
    PATCH /bots/{id}                (ADMIN)      set/clear chibi_pack
    POST  /bots/{id}/avatar         (ADMIN)      upload -> 256px WebP avatar
    DELETE /bots/{id}/avatar        (ADMIN)      clear the avatar
    GET   /bots/{id}/avatar         (open)       serve it, mirrors /avatars/{user_id}

Auth idiom: reads are actor-authenticated (bots discover each other's display
metadata the same way clients do); every mutation goes through auth's
`get_admin_user` — session-cookie only, `is_admin` required, so a leaked bot API
key can never repaint another bot. Avatar serving is unsigned, exactly like
GET /avatars/{user_id}: avatars are not sensitive and unsigned URLs keep client
rendering trivial.

Storage reuses the human avatar path wholesale (media.store_avatar ->
avatars/bot_{id}.webp) rather than inventing a second conversion/serving route.

`chibi_pack` is validated against services.chibi (bare pack name under
DATA_DIR/assets/chibi_packs/, or an absolute path to a pack dir) — an
unresolvable pack is a 400, not a silently dead cosmetic. Passing null clears
it; that is the one way to unset without a DB edit.
"""

from typing import Annotated, Any, Optional

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel

from .. import db
from ..models import Bot, User
from ..services import chibi as chibi_service
from .auth import Actor, get_actor, get_admin_user
from .media import avatar_response, bot_avatar_url, store_avatar

router = APIRouter()

AdminUser = Annotated[User, Depends(get_admin_user)]
CurrentActor = Annotated[Actor, Depends(get_actor)]

# The seeded message-author bot (migration 006). It is not a real participant —
# it can never authenticate — so it is not a thing to give cosmetics to.
SYSTEM_BOT_NAME = "system"


def _bot_out(row: dict[str, Any]) -> Bot:
    """Public Bot shape — never includes api_key_hash."""
    return Bot(
        id=row["id"],
        name=row["name"],
        avatar_path=row["avatar_path"],
        avatar_url=bot_avatar_url(row["id"], row["avatar_path"]),
        chibi_pack=row["chibi_pack"],
        created_at=row["created_at"],
    )


async def _require_bot(bot_id: int) -> dict[str, Any]:
    row = await db.fetch_one("SELECT * FROM bots WHERE id = ?", (bot_id,))
    if row is None:
        raise HTTPException(status_code=404, detail="Bot not found")
    return row


async def _require_editable_bot(bot_id: int) -> dict[str, Any]:
    row = await _require_bot(bot_id)
    if row["name"] == SYSTEM_BOT_NAME:
        raise HTTPException(
            status_code=400, detail="The 'system' author is not a configurable bot"
        )
    return row


class BotUpdate(BaseModel):
    """Cosmetic bot fields an admin may set.

    `chibi_pack` is tri-state: omitted = unchanged, a string = set (validated),
    explicit null = cleared. `_fields_set` distinguishes the first two.
    """

    chibi_pack: Optional[str] = None


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------

@router.get("/bots")
async def list_bots(actor: CurrentActor) -> list[Bot]:
    """All bots, public shape. Includes the seeded 'system' author."""
    return [
        _bot_out(r) for r in await db.fetch_all("SELECT * FROM bots ORDER BY id")
    ]


@router.get("/bots/{bot_id}")
async def get_bot(bot_id: int, actor: CurrentActor) -> Bot:
    return _bot_out(await _require_bot(bot_id))


# ---------------------------------------------------------------------------
# Mutations (admin only)
# ---------------------------------------------------------------------------

@router.patch("/bots/{bot_id}")
async def update_bot(bot_id: int, body: BotUpdate, admin: AdminUser) -> Bot:
    """Set or clear a bot's chibi pack. 400 if the pack does not resolve."""
    await _require_editable_bot(bot_id)
    if "chibi_pack" not in body.model_fields_set:
        return _bot_out(await _require_bot(bot_id))

    pack = body.chibi_pack
    if pack is not None:
        pack = pack.strip()
        if not pack:
            raise HTTPException(
                status_code=400,
                detail="chibi_pack must be a pack name/path, or null to clear it",
            )
        if not chibi_service.pack_exists(pack):
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Chibi pack {pack!r} not found — expected a directory under "
                    "DATA_DIR/assets/chibi_packs/ or an absolute path to a pack"
                ),
            )
    await db.execute("UPDATE bots SET chibi_pack = ? WHERE id = ?", (pack, bot_id))
    return _bot_out(await _require_bot(bot_id))


@router.post("/bots/{bot_id}/avatar")
async def upload_bot_avatar(
    bot_id: int, admin: AdminUser, file: UploadFile = File(...)
) -> dict[str, Any]:
    """Upload a bot avatar (same 256px WebP path as POST /me/avatar).

    `url` carries the `?v={mtime}` cache key (media.bot_avatar_url): the file
    name is stable across re-uploads, so without it a repainted bot kept
    showing its old face for up to the avatar max-age (300s).
    """
    await _require_editable_bot(bot_id)
    rel = await store_avatar(file, f"bot_{bot_id}")
    await db.execute("UPDATE bots SET avatar_path = ? WHERE id = ?", (rel, bot_id))
    return {"avatar_path": rel, "url": bot_avatar_url(bot_id, rel)}


@router.delete("/bots/{bot_id}/avatar")
async def clear_bot_avatar(bot_id: int, admin: AdminUser) -> dict[str, bool]:
    """Clear the avatar column (the file on disk is left as a harmless orphan,
    and is overwritten by the next upload — same stem)."""
    await _require_editable_bot(bot_id)
    await db.execute("UPDATE bots SET avatar_path = NULL WHERE id = ?", (bot_id,))
    return {"ok": True}


# ---------------------------------------------------------------------------
# Serving
# ---------------------------------------------------------------------------

@router.get("/bots/{bot_id}/avatar")
async def get_bot_avatar(bot_id: int) -> FileResponse:
    """Unsigned by design — mirrors GET /avatars/{user_id}."""
    row = await db.fetch_one("SELECT avatar_path FROM bots WHERE id = ?", (bot_id,))
    if row is None or not row["avatar_path"]:
        raise HTTPException(status_code=404, detail="No avatar")
    return avatar_response(row["avatar_path"])
