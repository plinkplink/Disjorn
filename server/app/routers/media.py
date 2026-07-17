"""Media module (WP6): uploads, web conversion, signed URLs, avatars, picker.

UPLOAD -> LINK FLOW (authoritative — WP10 client and WP13 SDK follow this):

  1. Upload FIRST, then create the message, then claim:
       a. POST /upload  (multipart, field name `files`, multiple files OK)
          -> creates STAGED attachment rows (message_id NULL) owned by the
             uploader; returns [{id, url, thumb_url, orig_url, ...}].
       b. POST /channels/{id}/messages  (WP4, unchanged — no attachment_ids
          in its body).
       c. POST /attachments/claim  {"attachment_ids": [...], "message_id": N}
          -> links the staged rows to the message. Only the message author may
             claim, and only attachments they themselves uploaded. Idempotent
             for already-claimed-to-the-same-message ids. On success the server
             publishes a `message_edit` bus event carrying the REFRESHED full
             message payload (attachments now included), so every client/bot
             sees the attachments appear without any special casing.

  2. Shortcut when the message already exists (e.g. adding files to your own
     message): POST /upload with a `message_id` form field (or ?message_id=
     query param). Validates the message exists and the caller is its author,
     links immediately, and publishes the same `message_edit` event.

  Staged rows that are never claimed are harmless orphans (message_id NULL —
  never joined into any payload); a GC sweep can use attachments.created_at.

ENDPOINTS
  POST /upload                      (actor)  multipart upload, optional message_id
  POST /attachments/claim           (actor)  link staged attachments to own message
  GET  /media/{attachment_id}       (signed) ?v=orig|display|thumb&exp=&sig=
  POST /me/avatar                   (user)   -> 256px WebP, users.avatar_path
  GET  /avatars/{user_id}           (open)   unsigned — avatars are not sensitive
  GET  /picker?tab=gif|image        (user)   list picker assets
  GET  /picker/file/{tab}/{name}    (open)   static picker asset (traversal-safe)

SIGNED URLS
  sign_media_url(attachment_id, variant="display", expires_in=None) -> str  (sync)
    Returns "/media/{id}?v={variant}&exp={unix}&sig={hex}" where
    sig = HMAC-SHA256(SECRET_KEY, "{id}:{variant}:{exp}"). WP4's
    message_payload() imports and calls this for every attachment payload.
  Variant fallback on GET: thumb -> display -> original; display -> original.
  Bad/expired signature -> 403. The signature IS the auth (no cookie/API key
  needed) so <img> tags and bots can fetch alike; DM attachments stay
  unguessable.

STORAGE (relative paths under DATA_DIR are what's stored in the DB)
  uploads/originals/{attachment_id}_{slug}{ext}   original, metadata intact
  uploads/web/{attachment_id}_{slug}_display.webp (absent if original is served)
  uploads/web/{attachment_id}_{slug}_thumb.webp
  avatars/user_{user_id}.webp
  assets/picker/{gifs,images}/*                   seeded with placeholders on
                                                  first use if missing
"""

import hashlib
import hmac
import mimetypes
import re
import time
import uuid
from pathlib import Path
from typing import Annotated, Any, Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from .. import db, events
from ..config import get_settings
from ..models import User
from ..services import media_convert
from ..services.media_convert import UnsupportedFormat
from .auth import Actor, get_actor, get_current_user
from .messages import message_payload

router = APIRouter()

CurrentUser = Annotated[User, Depends(get_current_user)]
CurrentActor = Annotated[Actor, Depends(get_actor)]

VARIANTS = ("orig", "display", "thumb")
PICKER_TABS = {"gif": "gifs", "image": "images"}
_CHUNK = 256 * 1024


# ---------------------------------------------------------------------------
# Signing
# ---------------------------------------------------------------------------

def _signature(attachment_id: int, variant: str, exp: int) -> str:
    key = get_settings().SECRET_KEY.encode("utf-8")
    msg = f"{attachment_id}:{variant}:{exp}".encode("utf-8")
    return hmac.new(key, msg, hashlib.sha256).hexdigest()


def sign_media_url(
    attachment_id: int, variant: str = "display", expires_in: Optional[int] = None
) -> str:
    """Signed relative URL for an attachment variant (sync — WP4 calls this)."""
    if variant not in VARIANTS:
        raise ValueError(f"unknown variant {variant!r}")
    ttl = expires_in if expires_in is not None else get_settings().MEDIA_URL_TTL
    exp = int(time.time()) + ttl
    sig = _signature(attachment_id, variant, exp)
    return f"/media/{attachment_id}?v={variant}&exp={exp}&sig={sig}"


# ---------------------------------------------------------------------------
# Path / filename helpers
# ---------------------------------------------------------------------------

def _data_dir() -> Path:
    return get_settings().data_dir


def _originals_dir() -> Path:
    return _data_dir() / "uploads" / "originals"


def _web_dir() -> Path:
    return _data_dir() / "uploads" / "web"


def _rel(path: Path) -> str:
    """Path relative to DATA_DIR, as stored in the DB (posix separators)."""
    return path.relative_to(_data_dir()).as_posix()


def _slugify(filename: Optional[str]) -> tuple[str, str]:
    """(slug, ext) from an untrusted client filename."""
    name = Path(filename or "file").name  # strips any client-supplied directories
    stem, ext = Path(name).stem, Path(name).suffix.lower()
    slug = re.sub(r"[^A-Za-z0-9_-]+", "-", stem).strip("-")[:40] or "file"
    if not re.fullmatch(r"\.[a-z0-9]{1,9}", ext):
        ext = ""
    return slug, ext


async def _save_upload(upload: UploadFile, dest: Path, max_bytes: int) -> int:
    """Stream an UploadFile to dest, enforcing max_bytes. Returns size."""
    size = 0
    try:
        with dest.open("wb") as out:
            while chunk := await upload.read(_CHUNK):
                size += len(chunk)
                if size > max_bytes:
                    raise HTTPException(
                        status_code=413,
                        detail=f"File exceeds maximum upload size ({max_bytes} bytes)",
                    )
                out.write(chunk)
    except BaseException:
        dest.unlink(missing_ok=True)
        raise
    return size


def _guess_mime(upload: UploadFile) -> str:
    if upload.content_type and upload.content_type != "application/octet-stream":
        return upload.content_type
    guessed, _ = mimetypes.guess_type(upload.filename or "")
    return guessed or "application/octet-stream"


# ---------------------------------------------------------------------------
# Attachment helpers
# ---------------------------------------------------------------------------

async def _require_own_message(message_id: int, actor: Actor) -> dict[str, Any]:
    row = await db.fetch_one("SELECT * FROM messages WHERE id = ?", (message_id,))
    if row is None:
        raise HTTPException(status_code=404, detail="Message not found")
    if row["author_type"] != actor.type or row["author_id"] != actor.id:
        raise HTTPException(status_code=403, detail="Not the author of this message")
    if row["deleted_at"] is not None:
        raise HTTPException(status_code=409, detail="Message is deleted")
    return row


async def _publish_refreshed(message_id: int) -> dict[str, Any]:
    """Re-materialize the message payload and publish message_edit with it."""
    row = await db.fetch_one("SELECT * FROM messages WHERE id = ?", (message_id,))
    assert row is not None
    payload = await message_payload(row)
    await events.publish(
        {"type": "message_edit", "channel_id": row["channel_id"], "message": payload}
    )
    return payload


def _attachment_out(att: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": att["id"],
        "message_id": att["message_id"],
        "original_filename": att["original_filename"],
        "mime_type": att["mime_type"],
        "size_bytes": att["size_bytes"],
        "width": att["width"],
        "height": att["height"],
        "has_preview": att["width"] is not None,
        "url": sign_media_url(att["id"], "display"),
        "thumb_url": sign_media_url(att["id"], "thumb"),
        "orig_url": sign_media_url(att["id"], "orig"),
    }


# ---------------------------------------------------------------------------
# POST /upload
# ---------------------------------------------------------------------------

@router.post("/upload")
async def upload_files(
    actor: CurrentActor,
    files: list[UploadFile] = File(...),
    message_id: Optional[int] = Form(default=None),
    message_id_q: Optional[int] = Query(default=None, alias="message_id"),
) -> dict[str, Any]:
    """Upload one or more files. See module docstring for the link flow."""
    link_message_id = message_id if message_id is not None else message_id_q
    if link_message_id is not None:
        await _require_own_message(link_message_id, actor)

    settings = get_settings()
    originals, web = _originals_dir(), _web_dir()
    originals.mkdir(parents=True, exist_ok=True)

    results: list[dict[str, Any]] = []
    for upload in files:
        tmp = originals / f"tmp_{uuid.uuid4().hex}"
        size = await _save_upload(upload, tmp, settings.MAX_UPLOAD_BYTES)
        mime = _guess_mime(upload)
        slug, ext = _slugify(upload.filename)
        original_name = Path(upload.filename or "file").name or "file"

        cur = await db.execute(
            """INSERT INTO attachments
                   (message_id, file_path, original_filename, mime_type, size_bytes,
                    uploader_type, uploader_id, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (link_message_id, "", original_name, mime, size, actor.type, actor.id, db.utc_now()),
        )
        att_id = cur.lastrowid
        final = originals / f"{att_id}_{slug}{ext}"
        tmp.rename(final)

        width = height = None
        display_rel = thumb_rel = None
        try:
            conv = media_convert.convert_for_web(final, web)
        except UnsupportedFormat:
            conv = None  # recognized format, codec unavailable: stored as file, no preview
        except Exception:
            conv = None  # corrupt/undecodable: same degradation
        if conv is not None and conv.is_image:
            width, height = conv.width, conv.height
            display_rel = _rel(conv.display_path) if conv.display_path else None
            thumb_rel = _rel(conv.thumb_path) if conv.thumb_path else None

        await db.execute(
            """UPDATE attachments
               SET file_path = ?, width = ?, height = ?, display_path = ?, thumb_path = ?
               WHERE id = ?""",
            (_rel(final), width, height, display_rel, thumb_rel, att_id),
        )
        att = await db.fetch_one("SELECT * FROM attachments WHERE id = ?", (att_id,))
        assert att is not None
        results.append(_attachment_out(att))

    message_payload_out = None
    if link_message_id is not None:
        message_payload_out = await _publish_refreshed(link_message_id)
    return {"attachments": results, "message": message_payload_out}


# ---------------------------------------------------------------------------
# POST /attachments/claim
# ---------------------------------------------------------------------------

class ClaimRequest(BaseModel):
    attachment_ids: list[int] = Field(min_length=1)
    message_id: int


@router.post("/attachments/claim")
async def claim_attachments(body: ClaimRequest, actor: CurrentActor) -> dict[str, Any]:
    """Link staged uploads to a message you authored; publishes message_edit."""
    await _require_own_message(body.message_id, actor)

    to_link: list[int] = []
    for att_id in body.attachment_ids:
        att = await db.fetch_one("SELECT * FROM attachments WHERE id = ?", (att_id,))
        if att is None:
            raise HTTPException(status_code=404, detail=f"Attachment {att_id} not found")
        if att["message_id"] == body.message_id:
            continue  # idempotent re-claim
        if att["message_id"] is not None:
            raise HTTPException(
                status_code=409, detail=f"Attachment {att_id} already linked to a message"
            )
        if att["uploader_type"] != actor.type or att["uploader_id"] != actor.id:
            raise HTTPException(
                status_code=403, detail=f"Attachment {att_id} was not uploaded by you"
            )
        to_link.append(att_id)

    for att_id in to_link:
        await db.execute(
            "UPDATE attachments SET message_id = ? WHERE id = ?", (body.message_id, att_id)
        )
    payload = await _publish_refreshed(body.message_id)
    return payload


# ---------------------------------------------------------------------------
# GET /media/{attachment_id} — signed access
# ---------------------------------------------------------------------------

@router.get("/media/{attachment_id}")
async def get_media(
    attachment_id: int,
    exp: int,
    sig: str,
    v: str = Query(default="display"),
) -> FileResponse:
    if v not in VARIANTS:
        raise HTTPException(status_code=400, detail="Unknown variant")
    now = int(time.time())
    if exp < now or not hmac.compare_digest(sig, _signature(attachment_id, v, exp)):
        raise HTTPException(status_code=403, detail="Invalid or expired signature")

    att = await db.fetch_one("SELECT * FROM attachments WHERE id = ?", (attachment_id,))
    if att is None:
        raise HTTPException(status_code=404, detail="Attachment not found")

    # Variant fallback: thumb -> display -> orig; display -> orig.
    rel, mime = att["file_path"], att["mime_type"]
    if v == "thumb" and att["thumb_path"]:
        rel, mime = att["thumb_path"], "image/webp"
    elif v in ("display", "thumb") and att["display_path"]:
        rel, mime = att["display_path"], "image/webp"

    path = _data_dir() / rel
    if not path.is_file():
        raise HTTPException(status_code=404, detail="File missing on disk")
    return FileResponse(
        path,
        media_type=mime,
        filename=att["original_filename"] if v == "orig" else None,
        content_disposition_type="inline",
        headers={"Cache-Control": f"private, max-age={max(0, exp - now)}"},
    )


# ---------------------------------------------------------------------------
# Avatars
# ---------------------------------------------------------------------------

@router.post("/me/avatar")
async def upload_avatar(user: CurrentUser, file: UploadFile = File(...)) -> dict[str, Any]:
    avatars = _data_dir() / "avatars"
    avatars.mkdir(parents=True, exist_ok=True)
    tmp = avatars / f"tmp_{uuid.uuid4().hex}"
    await _save_upload(file, tmp, get_settings().MAX_UPLOAD_BYTES)
    dest = avatars / f"user_{user.id}.webp"
    try:
        media_convert.make_avatar(tmp, dest, max_dim=256)
    except Exception:
        raise HTTPException(status_code=400, detail="Not a supported image")
    finally:
        tmp.unlink(missing_ok=True)
    rel = _rel(dest)
    await db.execute("UPDATE users SET avatar_path = ? WHERE id = ?", (rel, user.id))
    return {"avatar_path": rel, "url": f"/avatars/{user.id}"}


@router.get("/avatars/{user_id}")
async def get_avatar(user_id: int) -> FileResponse:
    """Unsigned by design: avatars are not sensitive and unsigned URLs keep
    client rendering trivial (plain <img src="/avatars/3">)."""
    row = await db.fetch_one("SELECT avatar_path FROM users WHERE id = ?", (user_id,))
    if row is None or not row["avatar_path"]:
        raise HTTPException(status_code=404, detail="No avatar")
    path = _data_dir() / row["avatar_path"]
    if not path.is_file():
        raise HTTPException(status_code=404, detail="No avatar")
    return FileResponse(
        path,
        media_type="image/webp",
        # Short max-age: the path is stable across re-uploads, so long caching
        # would show stale avatars.
        headers={"Cache-Control": "public, max-age=300"},
    )


# ---------------------------------------------------------------------------
# Picker
# ---------------------------------------------------------------------------

def _picker_dir(tab: str) -> Path:
    return _data_dir() / "assets" / "picker" / PICKER_TABS[tab]


def _seed_picker() -> None:
    """Create picker dirs with a few tiny generated placeholders (first run only).

    Seeding happens only when a tab directory does not exist yet, so deleting
    individual placeholders later doesn't resurrect them.
    """
    from PIL import Image

    images = _picker_dir("image")
    if not images.exists():
        images.mkdir(parents=True, exist_ok=True)
        for name, color in (("placeholder-red.png", (220, 60, 60)),
                            ("placeholder-blue.png", (60, 90, 220))):
            Image.new("RGB", (64, 64), color).save(images / name, format="PNG")
    gifs = _picker_dir("gif")
    if not gifs.exists():
        gifs.mkdir(parents=True, exist_ok=True)
        frames = [Image.new("RGB", (64, 64), c) for c in ((240, 200, 40), (40, 200, 160))]
        frames[0].save(
            gifs / "placeholder-blink.gif",
            format="GIF", save_all=True, append_images=frames[1:], duration=400, loop=0,
        )


@router.get("/picker")
async def list_picker(user: CurrentUser, tab: str = Query(...)) -> list[dict[str, str]]:
    if tab not in PICKER_TABS:
        raise HTTPException(status_code=400, detail="tab must be 'gif' or 'image'")
    _seed_picker()
    directory = _picker_dir(tab)
    out = []
    for p in sorted(directory.iterdir()):
        if p.is_file() and not p.name.startswith("."):
            out.append({"name": p.name, "url": f"/picker/file/{tab}/{p.name}"})
    return out


@router.get("/picker/file/{tab}/{name}")
async def get_picker_file(tab: str, name: str) -> FileResponse:
    if tab not in PICKER_TABS:
        raise HTTPException(status_code=404, detail="Unknown tab")
    # Traversal safety: the name must be a plain filename component.
    if (
        name != Path(name).name
        or name in (".", "..")
        or name.startswith(".")
        or "/" in name
        or "\\" in name
    ):
        raise HTTPException(status_code=400, detail="Invalid file name")
    _seed_picker()
    base = _picker_dir(tab).resolve()
    path = (base / name).resolve()
    if path.parent != base or not path.is_file():
        raise HTTPException(status_code=404, detail="Not found")
    mime, _ = mimetypes.guess_type(name)
    return FileResponse(
        path,
        media_type=mime or "application/octet-stream",
        headers={"Cache-Control": "public, max-age=3600"},
    )
