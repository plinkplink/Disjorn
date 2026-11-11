"""How stored bytes leave the house origin: a fixed set of inline types, everything else a download."""

from pathlib import Path
from typing import Optional
from urllib.parse import quote

from fastapi.responses import FileResponse

OCTET = "application/octet-stream"
PDF = "application/pdf"

# `sandbox` gives a response opened as a document an opaque origin and no script;
# the style and media sources are for the browser's own viewer page.
CSP_DOWNLOAD = "default-src 'none'; sandbox"
CSP_IMAGE = "default-src 'none'; style-src 'unsafe-inline'; sandbox"
CSP_AV = "default-src 'none'; style-src 'unsafe-inline'; media-src 'self'; sandbox"

_AV_CONTAINER = {
    "video/mp4": "mp4",
    "video/quicktime": "mp4",
    "audio/mp4": "mp4",
    "audio/x-m4a": "mp4",
    "video/webm": "webm",
    "audio/webm": "webm",
    "video/ogg": "ogg",
    "audio/ogg": "ogg",
    "audio/wav": "wav",
    "audio/x-wav": "wav",
    "audio/flac": "flac",
    "audio/mpeg": "mp3",
}


def _raster(head: bytes) -> Optional[str]:
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if head.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if head.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    if head[4:8] == b"ftyp" and head[8:12] in (b"avif", b"avis"):
        return "image/avif"
    return None


def _container(head: bytes) -> Optional[str]:
    if head[4:8] == b"ftyp":
        return "mp4"
    if head.startswith(b"\x1a\x45\xdf\xa3"):
        return "webm"
    if head.startswith(b"OggS"):
        return "ogg"
    if head[:4] == b"RIFF" and head[8:12] == b"WAVE":
        return "wav"
    if head.startswith(b"fLaC"):
        return "flac"
    if head.startswith(b"ID3") or (len(head) > 1 and head[0] == 0xFF and head[1] & 0xE0 == 0xE0):
        return "mp3"
    return None


def served_type(path: Path, declared: str, decoded: bool) -> tuple[str, bool]:
    """(Content-Type, inline) for a stored upload, read from its leading bytes.

    `declared` is the uploader's word: it never becomes the type unless it is a
    listed audio or video type and the bytes are that container. `decoded` is
    the conversion pipeline's verdict that the file is an image.
    """
    with path.open("rb") as fh:
        head = fh.read(16)
    raster = _raster(head) if decoded else None
    if raster is not None:
        return raster, True
    mime = declared.split(";", 1)[0].strip().lower()
    if mime in _AV_CONTAINER and _AV_CONTAINER[mime] == _container(head):
        return mime, True
    return (PDF if head.startswith(b"%PDF-") else OCTET), False


def _respond(path: Path, media_type: str, csp: str, headers: dict[str, str]) -> FileResponse:
    return FileResponse(
        path,
        media_type=media_type,
        headers={**headers, "X-Content-Type-Options": "nosniff", "Content-Security-Policy": csp},
    )


def upload(
    path: Path, filename: str, declared: str, decoded: bool, headers: dict[str, str]
) -> FileResponse:
    """A file an uploader supplied: inline only as a listed image, audio or video type."""
    media_type, inline = served_type(path, declared, decoded)
    kind = "inline" if inline else "attachment"
    csp = CSP_DOWNLOAD if not inline else CSP_IMAGE if media_type.startswith("image/") else CSP_AV
    disposition = f"{kind}; filename*=UTF-8''{quote(filename, safe='')}"
    return _respond(path, media_type, csp, {**headers, "Content-Disposition": disposition})


def image(path: Path, media_type: str, headers: dict[str, str]) -> FileResponse:
    """An image the server encoded or ships itself."""
    return _respond(path, media_type, CSP_IMAGE, headers)
