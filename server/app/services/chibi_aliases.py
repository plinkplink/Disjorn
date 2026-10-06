"""Picker edits to a pack: its face list, one `Aliases.txt` line per tag, and the tags a message carries."""

import os
import re
import stat
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from . import chibi
from .emotion_match import normalize

TAG_RE = re.compile(r"\[emotion:\s*([^\]\n]+)\]", re.IGNORECASE)
# The client renderer reads code spans literally, so a tag inside one is no tag.
_CODE_RE = re.compile(r"```[\s\S]*?```|`[^`\n]+`")
_UNWRITABLE = re.compile(r"[^A-Za-z0-9 '_-]+")
MAX_TAG_CHARS = 100

_write_lock = threading.Lock()


class AliasError(ValueError):
    pass


@dataclass(frozen=True)
class Face:
    category: str
    file: str

    @property
    def name(self) -> str:
        return Path(self.file).stem

    def ref(self, pack_name: str) -> str:
        return f"chibi:{pack_name}/{self.category}/{self.file}"


@dataclass(frozen=True)
class Current:
    face: Optional[Face]
    source: str  # "name", "alias" or "ladder"
    targets: Optional[tuple[str, ...]]


def pack_dir(pack: str) -> Path:
    found = chibi.pack_path(pack)
    if found is None:
        raise LookupError(pack)
    return found


def faces(pack: str) -> list[Face]:
    index, _ = chibi.pack_tables(pack_dir(pack))
    return sorted(
        (Face(*hit) for hit in index.values()),
        key=lambda f: (f.category.lower(), f.name.lower()),
    )


def face_for(pack: str, target: str) -> Face:
    index, _ = chibi.pack_tables(pack_dir(pack))
    hit = index.get(normalize(target))
    if hit is None:
        raise AliasError(f"{target!r} is not a face in this pack")
    return Face(*hit)


def current(pack: str, tag: str) -> Current:
    directory = pack_dir(pack)
    index, aliases = chibi.pack_tables(directory)
    norm = normalize(tag)
    if norm in index:
        return Current(Face(*index[norm]), "name", None)
    ref = chibi.resolve(str(directory), tag)
    face = Face(*ref.split("/", 2)[1:]) if ref else None
    targets = aliases.get(norm)
    return Current(face, "alias" if targets else "ladder", targets)


def writable_tag(tag: str) -> str:
    written = " ".join(_UNWRITABLE.sub(" ", tag).split()).lower()
    if not normalize(written) or len(written) > MAX_TAG_CHARS:
        raise AliasError("tag must have letters or digits and be under 100 characters")
    return written


def _line_tag(line: str) -> Optional[str]:
    body = line.split("#", 1)[0]
    if "->" not in body:
        return None
    return normalize(body.split("->", 1)[0])


def _ending(line: str) -> str:
    return line[len(line.rstrip("\r\n")):]


def _rewrite(path: Path, tag: str, new_line: Optional[str]) -> bool:
    try:
        text = path.read_bytes().decode("utf-8", "surrogateescape")
    except FileNotFoundError:
        text = ""
    lines = text.splitlines(keepends=True)
    norm = normalize(tag)
    out: list[str] = []
    placed = False
    for line in lines:
        if _line_tag(line) != norm:
            out.append(line)
            continue
        if new_line is not None and not placed:
            out.append(new_line + _ending(line))
        placed = True
    if new_line is not None and not placed:
        newline = "\r\n" if lines and lines[0].endswith("\r\n") else "\n"
        if out and not _ending(out[-1]):
            out[-1] += newline
        out.append(new_line + newline)
    changed = "".join(out)
    if changed == text:
        return False
    _atomic_write(path, changed.encode("utf-8", "surrogateescape"))
    return True


def _atomic_write(path: Path, data: bytes) -> None:
    try:
        mode = stat.S_IMODE(path.stat().st_mode)
    except FileNotFoundError:
        mode = 0o644
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def set_alias(pack: str, tag: str, target: str) -> tuple[str, Face, bool]:
    written = writable_tag(tag)
    face = face_for(pack, target)
    line = f"{written.ljust(15)} -> {face.name}"
    with _write_lock:
        changed = _rewrite(pack_dir(pack) / chibi.ALIASES_FILENAME, written, line)
    chibi.clear_cache()
    return written, face, changed


def remove_alias(pack: str, tag: str) -> bool:
    written = writable_tag(tag)
    path = pack_dir(pack) / chibi.ALIASES_FILENAME
    with _write_lock:
        changed = path.is_file() and _rewrite(path, written, None)
    chibi.clear_cache()
    return changed


def tags_in(content: str) -> list[str]:
    readable = _CODE_RE.sub(" ", content)
    return [m.strip() for m in TAG_RE.findall(readable)]


def repoint(refs: list[Any], index: int, ref: str) -> Optional[list[Any]]:
    """The nth tag shows the nth chibi ref, so a tag past every ref can only take the next free slot."""
    slots = [i for i, r in enumerate(refs) if isinstance(r, str) and r.startswith("chibi:")]
    out = list(refs)
    if index < len(slots):
        out[slots[index]] = ref
    elif index == len(slots):
        out.insert(slots[-1] + 1 if slots else len(out), ref)
    else:
        return None
    return out
