"""What a bot may see of an uploaded file, the shapes one read returns, and a seat's export."""

import asyncio
import base64
import hashlib
import json
import logging
import mimetypes
import os
import re
import shutil
from pathlib import Path
from typing import Any, Optional

from .. import db, privacy
from ..config import get_settings
from ..routers.approval import RESIDENT_PRINCIPAL_RE

logger = logging.getLogger(__name__)

MAX_SEAT_LABEL_CHARS = 64
MANIFEST = "manifest.json"
EXPORT_FILE_MODE = 0o640
EXPORT_DIR_MODE = 0o750
_CHUNK = 1024 * 1024
# A browser runs these when it opens one from the house origin.
_UNPOSTABLE_MIMES = frozenset({"text/html", "text/xml", "application/xml", "image/svg+xml"})
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


async def seat_bot(seat: str) -> Optional[int]:
    """`res-<name>` is the one bot whose lowercased name is `<name>`; any other label is nobody."""
    if len(seat) > MAX_SEAT_LABEL_CHARS or not RESIDENT_PRINCIPAL_RE.fullmatch(seat):
        return None
    rows = await db.fetch_all(
        "SELECT id FROM bots WHERE lower(name) = ?", (seat.removeprefix("res-"),)
    )
    return rows[0]["id"] if len(rows) == 1 else None


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        while chunk := fh.read(_CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def export_name(att: dict[str, Any]) -> str:
    """`{id}_{slug}{ext}`: one path component, whatever the uploader named the file."""
    name = str(att["original_filename"] or "")
    stem, dot, ext = name.rpartition(".")
    if not (dot and stem and re.fullmatch(r"[A-Za-z0-9]{1,9}", ext)):
        stem, ext = name, ""
    slug = re.sub(r"[^A-Za-z0-9_-]+", "-", stem).strip("-")[:40] or "file"
    return f"{int(att['id'])}_{slug}{'.' + ext.lower() if ext else ''}"


def _remove(path: str) -> None:
    """A removal that fails is logged and left to the next sweep."""
    try:
        if os.path.isdir(path) and not os.path.islink(path):
            shutil.rmtree(path)
        else:
            os.unlink(path)
    except FileNotFoundError:
        pass
    except OSError:
        logger.exception("bot file export: could not remove %s", path)


def _empty(seat_dir: Path) -> None:
    try:
        for entry in os.scandir(seat_dir):
            _remove(entry.path)
    except OSError:
        logger.exception("bot file export: could not empty %s", seat_dir)


def _write_atomic(dest: Path, chunks) -> None:
    """A temp file renamed within the directory, so a reader never sees part of a file."""
    tmp = dest.with_name(f".tmp-{dest.name}")
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, EXPORT_FILE_MODE)
        with os.fdopen(fd, "wb") as out:
            os.fchmod(fd, EXPORT_FILE_MODE)
            for chunk in chunks:
                out.write(chunk)
        os.replace(tmp, dest)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def _copy(src: Path, dest: Path) -> str:
    digest = hashlib.sha256()

    def chunks():
        with src.open("rb") as fh:
            while chunk := fh.read(_CHUNK):
                digest.update(chunk)
                yield chunk

    _write_atomic(dest, chunks())
    return digest.hexdigest()


def _write_manifest(folder: Path, entries: list[dict[str, Any]]) -> None:
    """A manifest that cannot be written is removed, so it never lists a file that is gone."""
    manifest = folder / MANIFEST
    rows = sorted(entries, key=lambda e: e["attachment_id"])
    body = json.dumps(rows, indent=1).encode("utf-8")
    try:
        if not (manifest.is_file() and manifest.read_bytes() == body):
            _write_atomic(manifest, [body])
    except OSError:
        logger.exception("bot file export: could not write %s", manifest)
        _remove(str(manifest))


def _reconcile(seat_dir: Path, wanted: dict[int, dict[str, tuple[Path, dict[str, Any]]]]) -> None:
    """Removals, then pruned manifests, then copies: a failure leaves too little, never too much."""
    folders = {str(channel_id): files for channel_id, files in wanted.items()}
    for entry in os.scandir(seat_dir):
        keep = folders.get(entry.name) if entry.is_dir(follow_symlinks=False) else None
        if keep is None:
            _remove(entry.path)
            continue
        for item in os.scandir(entry.path):
            listed = item.name in keep or item.name == MANIFEST
            if not (listed and item.is_file(follow_symlinks=False)):
                _remove(item.path)

    copies = []
    for name, files in folders.items():
        folder = seat_dir / name
        try:
            folder.mkdir(mode=EXPORT_DIR_MODE, exist_ok=True)
        except OSError:
            logger.exception("bot file export: could not create %s", folder)
            continue
        try:
            known = {e["file"]: e["sha256"] for e in json.loads((folder / MANIFEST).read_bytes())}
        except (OSError, ValueError, TypeError, KeyError):
            known = {}
        entries, missing = [], []
        for file_name, (src, meta) in files.items():
            if file_name in known and (folder / file_name).is_file():
                entries.append({**meta, "file": file_name, "sha256": known[file_name]})
            else:
                missing.append((file_name, src, meta))
        _write_manifest(folder, entries)
        if missing:
            copies.append((folder, entries, missing))

    for folder, entries, missing in copies:
        for file_name, src, meta in missing:
            dest = folder / file_name
            try:
                sha = _copy(src, dest)
            except OSError:
                logger.exception("bot file export: could not write %s", dest)
                _remove(str(dest))
                continue
            entries.append({**meta, "file": file_name, "sha256": sha})
        _write_manifest(folder, entries)


async def _sweep_seat(seat: str, seat_dir: Path) -> None:
    # Imported here because routers.channels imports this module.
    from ..routers.channels import is_member

    wanted: dict[int, dict[str, tuple[Path, dict[str, Any]]]] = {}
    bot_id = await seat_bot(seat)
    rows = [] if bot_id is None else await db.fetch_all(
        """SELECT a.*, m.channel_id,
                  CASE m.author_type WHEN 'user' THEN u.display_name ELSE b.name END AS author
             FROM attachments a
             JOIN messages m ON m.id = a.message_id
             JOIN channels c ON c.id = m.channel_id
             LEFT JOIN users u ON m.author_type = 'user' AND u.id = m.author_id
             LEFT JOIN bots b ON m.author_type = 'bot' AND b.id = m.author_id
            WHERE c.files_safe = 1
            ORDER BY a.id"""
    )
    member: dict[int, bool] = {}
    for att in rows:
        channel_id = att["channel_id"]
        if channel_id not in member:
            member[channel_id] = await is_member(channel_id, "bot", bot_id)
        if not (member[channel_id] and await visible_to_bots(att)):
            continue
        try:
            src = _stored(att["file_path"], "originals")
        except LookupError:
            continue
        wanted.setdefault(channel_id, {})[export_name(att)] = (src, {
            "attachment_id": att["id"],
            "message_id": att["message_id"],
            "author": att["author"],
            "created_at": att["created_at"],
            "mime_type": att["mime_type"],
        })
    await asyncio.to_thread(_reconcile, seat_dir, wanted)


_sweep_lock = asyncio.Lock()


def init() -> None:
    """A lock belongs to the event loop it first waited on, so each app start takes a new one."""
    global _sweep_lock
    _sweep_lock = asyncio.Lock()


async def export_sweep() -> None:
    """Make each seat's export hold exactly the files `visible_to_bots` passes for its bot now.

    Never raises: a seat whose sweep cannot finish has its export emptied. A
    seat directory that does not exist is a seat with no export.
    """
    try:
        settings = get_settings()
        root = Path(settings.BOT_FILES_EXPORT_ROOT)
        seats = [s for s in settings.BOT_FILES_EXPORT_SEATS if RESIDENT_PRINCIPAL_RE.fullmatch(s)]
        async with _sweep_lock:
            for seat in seats:
                seat_dir = root / seat
                if seat_dir.is_symlink() or not seat_dir.is_dir():
                    continue
                try:
                    await _sweep_seat(seat, seat_dir)
                except Exception:
                    logger.exception("bot file export: sweep failed for %s; emptying it", seat)
                    _empty(seat_dir)
    except Exception:
        logger.exception("bot file export: sweep could not run")


async def sweep_forever(seconds: float) -> None:
    while True:
        await asyncio.sleep(seconds)
        await export_sweep()


class Refused(Exception):
    """Why a bot's file was not posted, in words the seat may read."""


def _postable(mime: str) -> bool:
    return mime not in _UNPOSTABLE_MIMES and (
        mime.startswith(_ALLOWED_PREFIXES) or mime in _ALLOWED_MIMES
    )


def postable_mime(filename: str) -> Optional[str]:
    """The type a posted file is stored as, from its name alone; None for a type no bot may post."""
    mime = (mimetypes.guess_type(filename)[0] or "").lower()
    return mime if _postable(mime) else None


async def check_channel(bot_id: int, channel_id: int, who: str = "bot") -> None:
    """Refused unless the channel is files-safe and the bot is a member of it."""
    # Imported here because routers.channels imports this module.
    from ..routers.channels import is_member

    channel = await db.fetch_one("SELECT files_safe FROM channels WHERE id = ?", (channel_id,))
    if (
        channel is None
        or channel["files_safe"] != 1
        or not await is_member(channel_id, "bot", bot_id)
    ):
        raise Refused(f"channel {channel_id} is not a files-safe channel this {who} is a member of")


async def check_quota(bot_id: int, size: int, quota: int) -> None:
    """Refused if `size` more bytes would take the bot past `quota` for the UTC day."""
    used = await uploaded_today(bot_id)
    if used + size > quota:
        raise Refused(
            f"daily upload quota is {quota} bytes; {used} are used today and this file is {size}"
        )


async def check_staged(bot_id: int, att: dict[str, Any]) -> None:
    """Refused unless the bot may attach this stored upload of its own.

    The type the uploader declared is checked beside the name, because the
    bot's own key chose both.
    """
    if postable_mime(str(att["original_filename"])) is None or not _postable(_mime(att)):
        raise Refused("a bot may not post a file of this type")
    try:
        data = await asyncio.to_thread(_stored(att["file_path"], "originals").read_bytes)
    except (LookupError, OSError):
        raise Refused("this upload cannot be read") from None
    if await matches_hidden(data, bot_id):
        raise Refused("this file matches an upload that bots may not read")


async def uploaded_today(bot_id: int) -> int:
    """Bytes this bot has uploaded since 00:00 UTC, counted from its attachment rows."""
    row = await db.fetch_one(
        """SELECT COALESCE(SUM(size_bytes), 0) AS used FROM attachments
            WHERE uploader_type = 'bot' AND uploader_id = ? AND created_at >= ?""",
        (bot_id, db.utc_now()[:10]),
    )
    return row["used"]


async def matches_hidden(data: bytes, poster: Optional[int] = None) -> bool:
    """True if these bytes are a stored upload that `visible_to_bots` does not pass now.

    A row of another size cannot hold the same bytes, and a same-size row that
    cannot be hashed counts as a match. An upload `poster` staged itself and
    linked to no message is that bot's own bytes, so it is not a match.
    """
    sha = hashlib.sha256(data).hexdigest()
    rows = await db.fetch_all("SELECT * FROM attachments WHERE size_bytes = ?", (len(data),))
    for att in rows:
        own = (att["uploader_type"], att["uploader_id"]) == ("bot", poster)
        if (own and att["message_id"] is None) or await visible_to_bots(att):
            continue
        try:
            stored = await asyncio.to_thread(_sha256, _stored(att["file_path"], "originals"))
        except Exception:
            return True
        if stored == sha:
            return True
    return False


async def discard_staged(attachment_id: int) -> None:
    """Drop an upload that reached no message, so it neither counts nor matches afterwards."""
    att = await db.fetch_one(
        "SELECT * FROM attachments WHERE id = ? AND message_id IS NULL", (attachment_id,)
    )
    if att is None:
        return
    await db.execute(
        "DELETE FROM attachments WHERE id = ? AND message_id IS NULL", (attachment_id,)
    )
    for rel, folder in (
        (att["file_path"], "originals"), (att["display_path"], "web"), (att["thumb_path"], "web")
    ):
        try:
            _stored(rel, folder).unlink()
        except (LookupError, OSError):
            pass
