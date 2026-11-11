"""What a bot may see of an uploaded file, and the shapes one read returns."""

import asyncio
import base64
import json
from pathlib import Path
from typing import Any, Optional

from .. import db, privacy
from ..config import get_settings

PDF_MIME = "application/pdf"
PDF_MAX_PAGES = 200
VARIANT_MIME = "image/webp"
_ALLOWED_PREFIXES = ("text/", "image/")
_ALLOWED_MIMES = frozenset({
    PDF_MIME,
    "application/json",
    "application/xml",
    "application/javascript",
    "application/x-javascript",
    "application/x-sh",
    "application/x-python",
    "application/yaml",
    "application/x-yaml",
    "application/toml",
    "application/sql",
})


def _mime(att: dict[str, Any]) -> str:
    return str(att["mime_type"]).split(";", 1)[0].strip().lower()


async def visible_to_bots(att: dict[str, Any]) -> bool:
    """The one privacy decision for an attachments row; any error deciding it is a no."""
    try:
        row = await db.fetch_one(
            """SELECT m.created_at, m.deleted_at, m.privacy_flags,
                      c.files_safe, c.files_safe_since
                 FROM messages m JOIN channels c ON c.id = m.channel_id
                WHERE m.id = ?""",
            (att["message_id"],),
        )
        if row is None or row["files_safe"] != 1 or row["deleted_at"] is not None:
            return False
        since = row["files_safe_since"]
        if att["bot_history_ok"] != 1 and not (since and row["created_at"] >= since):
            return False
        flags = json.loads(row["privacy_flags"])
        if not isinstance(flags, dict) or privacy.hidden_from_bots(flags):
            return False
        mime = _mime(att)
        if not (mime.startswith(_ALLOWED_PREFIXES) or mime in _ALLOWED_MIMES):
            return False
        return 0 <= att["size_bytes"] <= get_settings().MAX_UPLOAD_BYTES
    except Exception:
        return False


async def channel_of(attachment_id: int) -> Optional[int]:
    """The channel a claimed attachment's message is in; None for an unknown or staged id."""
    row = await db.fetch_one(
        """SELECT m.channel_id FROM attachments a JOIN messages m ON m.id = a.message_id
            WHERE a.id = ?""",
        (attachment_id,),
    )
    return row["channel_id"] if row is not None else None


async def for_bot(payload: dict[str, Any]) -> dict[str, Any]:
    """A message payload as a bot receives it.

    `attachments` is absent unless the channel is files_safe, so a bot cannot
    tell a hidden file from no file.
    """
    out = {k: v for k, v in payload.items() if k != "attachments"}
    try:
        channel = await db.fetch_one(
            "SELECT files_safe FROM channels WHERE id = ?", (payload["channel_id"],)
        )
        if channel is None or channel["files_safe"] != 1:
            return out
        rows = await db.fetch_all(
            "SELECT * FROM attachments WHERE message_id = ? ORDER BY id", (payload["id"],)
        )
        out["attachments"] = [
            {"id": att["id"], "mime_type": att["mime_type"], "size_bytes": att["size_bytes"]}
            for att in rows
            if await visible_to_bots(att)
        ]
    except Exception:
        out.pop("attachments", None)
    return out


async def set_channel_safe(channel_id: int, on: bool, *, include_history: bool = False) -> bool:
    """Flip a channel's flag; True if it changed.

    An off-to-on flip restarts `files_safe_since`, so only a row carrying
    `bot_history_ok` stays readable across it.
    """
    async with db.transaction():
        row = await db.fetch_one("SELECT files_safe FROM channels WHERE id = ?", (channel_id,))
        if row is None:
            raise LookupError(channel_id)
        was_on = row["files_safe"] == 1
        if on and not was_on:
            await db.execute(
                "UPDATE channels SET files_safe = 1, files_safe_since = ? WHERE id = ?",
                (db.utc_now(), channel_id),
                commit=False,
            )
        elif was_on and not on:
            await db.execute(
                "UPDATE channels SET files_safe = 0, files_safe_since = NULL WHERE id = ?",
                (channel_id,),
                commit=False,
            )
        if on and include_history:
            await db.execute(
                """UPDATE attachments SET bot_history_ok = 1
                    WHERE message_id IN (SELECT id FROM messages WHERE channel_id = ?)""",
                (channel_id,),
                commit=False,
            )
    return was_on != on


def _stored(rel: Optional[str], folder: str) -> Path:
    """A stored file's path, refused unless it sits directly in uploads/<folder>."""
    root = (get_settings().data_dir / "uploads" / folder).resolve()
    path = (get_settings().data_dir / (rel or "")).resolve()
    if path.parent != root or not path.is_file():
        raise LookupError(rel)
    return path


def _text_shape(path: Path, cap: int) -> dict[str, Any]:
    total = path.stat().st_size
    with path.open("rb") as fh:
        data = fh.read(cap)
    text = data.decode("utf-8", errors="replace")
    if total > len(data):
        text += f"\n[truncated: {len(data)} of {total} bytes]"
    return {"kind": "text", "text": text}


def _image_shape(att: dict[str, Any], cap: int) -> dict[str, Any]:
    """The largest stored webp variant under the cap; the original is never a candidate."""
    fitting = []
    for rel in (att["display_path"], att["thumb_path"]):
        if rel:
            path = _stored(rel, "web")
            if path.stat().st_size <= cap:
                fitting.append(path)
    if not fitting:
        raise LookupError(att["id"])
    best = max(fitting, key=lambda p: p.stat().st_size)
    return {
        "kind": "image",
        "media_type": VARIANT_MIME,
        "data": base64.b64encode(best.read_bytes()).decode("ascii"),
    }


def _pdf_shape(path: Path, cap: int) -> dict[str, Any]:
    """Extraction stops at the byte cap or PDF_MAX_PAGES, whichever comes first.

    A worker thread cannot be cancelled, so the bound is on the work asked for.
    """
    # Imported here so a host without pypdf loses PDF reads, not the server.
    from pypdf import PdfReader

    reader = PdfReader(str(path))
    total = len(reader.pages)
    out = b""
    has_text = False
    for number in range(1, min(total, PDF_MAX_PAGES) + 1):
        body = (reader.pages[number - 1].extract_text() or "").strip()
        has_text = has_text or bool(body)
        out += f"[page {number}]\n{body}\n".encode("utf-8", errors="replace")
        if len(out) > cap or (number == PDF_MAX_PAGES and total > number):
            text = out[:cap].decode("utf-8", errors="ignore").rstrip("\n")
            return {"kind": "text",
                    "text": f"{text}\n[truncated: page {number} of {total}]"}
    if not has_text:
        return {"kind": "text", "text": f"[no text layer: {total} pages]"}
    return {"kind": "text", "text": out.decode("utf-8")}


async def read(attachment_id: int) -> Optional[dict[str, Any]]:
    """One file in its bot shape, or None for an id that is unknown, hidden or unreadable.

    No stored byte is opened before `visible_to_bots` passes for the row. The
    caller owns the reading bot's channel membership.
    """
    try:
        att = await db.fetch_one("SELECT * FROM attachments WHERE id = ?", (attachment_id,))
        if att is None or not await visible_to_bots(att):
            return None
        cap = get_settings().BOT_READ_BYTES
        mime = _mime(att)
        if mime.startswith("image/"):
            shape = _image_shape(att, cap)
        elif mime == PDF_MIME:
            shape = await asyncio.to_thread(
                _pdf_shape, _stored(att["file_path"], "originals"), cap
            )
        else:
            shape = _text_shape(_stored(att["file_path"], "originals"), cap)
    except Exception:
        return None
    return {"id": att["id"], "mime_type": att["mime_type"],
            "size_bytes": att["size_bytes"], **shape}
