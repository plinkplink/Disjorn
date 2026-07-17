"""Media conversion service (WP6): originals -> web-friendly WebP variants.

Public API:
    convert_for_web(src_path, dest_dir) -> ConvertResult
        Loads HEIC/HEIF via pillow-heif, RAW/DNG via rawpy (optional extra —
        raises UnsupportedFormat with a clear message when not installed),
        everything else via Pillow. Non-image files pass through untouched
        (is_image=False, no display/thumb).

        Produces in dest_dir (named after src's stem):
          {stem}_display.webp — max dimension DISPLAY_MAX (~2048), quality 85.
              Skipped when the original is already web-friendly (JPEG/PNG/WebP/GIF),
              within size limits AND carries no EXIF GPS — then the original is
              served as the display variant (display_path=None,
              display_is_original=True).
          {stem}_thumb.webp   — max dimension THUMB_MAX (~400). Always produced
              for images.

        EXIF orientation is applied before encoding. Re-encoded variants carry
        NO EXIF at all (Pillow only writes EXIF when asked), so GPS and other
        metadata are stripped from display copies (privacy). Originals keep
        their metadata on disk and are only reachable via the signed `orig`
        variant.

    make_avatar(src_path, dest_path, max_dim=256) -> (width, height)
        Small square-ish WebP for avatars; same loaders, EXIF stripped.

    UnsupportedFormat — raised when a format is recognized but the codec is
        unavailable (e.g. RAW without rawpy). The upload route maps this to
        "stored as file, no preview".
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from PIL import ExifTags, Image, ImageOps, UnidentifiedImageError

DISPLAY_MAX = 2048
THUMB_MAX = 400
WEBP_QUALITY = 85

# Formats browsers render natively — candidates for the "serve original" shortcut.
WEB_FRIENDLY_FORMATS = {"JPEG", "PNG", "WEBP", "GIF"}

HEIF_EXTS = {".heic", ".heif", ".hif"}
RAW_EXTS = {
    ".dng", ".nef", ".cr2", ".cr3", ".arw", ".orf",
    ".rw2", ".raf", ".srw", ".pef", ".nrw", ".x3f",
}


class UnsupportedFormat(Exception):
    """Recognized image format but no codec available to decode it."""


@dataclass
class ConvertResult:
    is_image: bool
    width: Optional[int] = None            # original dimensions (after EXIF orientation)
    height: Optional[int] = None
    display_path: Optional[Path] = None    # None => no separate display copy
    thumb_path: Optional[Path] = None
    display_is_original: bool = False      # original is web-friendly; serve it as display


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------

def _load_image(src: Path) -> Image.Image:
    """Open src as a PIL image. UnsupportedFormat for codec gaps,
    UnidentifiedImageError for non-images."""
    ext = src.suffix.lower()
    if ext in HEIF_EXTS:
        try:
            from pillow_heif import register_heif_opener
        except ImportError as e:  # pragma: no cover — pillow-heif is in requirements
            raise UnsupportedFormat(
                "HEIC/HEIF decoding requires pillow-heif, which is not available"
            ) from e
        register_heif_opener()  # idempotent
        return Image.open(src)
    if ext in RAW_EXTS:
        try:
            import rawpy
        except ImportError as e:
            raise UnsupportedFormat(
                "RAW/DNG decoding requires rawpy (install requirements-ml.txt)"
            ) from e
        with rawpy.imread(str(src)) as raw:
            rgb = raw.postprocess()
        return Image.fromarray(rgb)
    return Image.open(src)


def _flatten(img: Image.Image) -> Image.Image:
    """Convert to a mode WebP encodes well (RGB, or RGBA when transparent)."""
    if img.mode in ("RGB", "RGBA"):
        return img
    if img.mode in ("P", "PA", "LA") or img.info.get("transparency") is not None:
        return img.convert("RGBA")
    return img.convert("RGB")


def _has_gps(img: Image.Image) -> bool:
    """True if the image carries an EXIF GPS IFD. Conservative on errors."""
    try:
        return bool(img.getexif().get_ifd(ExifTags.IFD.GPSInfo))
    except Exception:
        return True  # can't prove it's clean -> force re-encode (which strips EXIF)


def _save_webp(img: Image.Image, dest: Path, max_dim: int) -> None:
    out = _flatten(img).copy()
    out.thumbnail((max_dim, max_dim), Image.LANCZOS)
    out.save(dest, format="WEBP", quality=WEBP_QUALITY)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def convert_for_web(src_path: str | Path, dest_dir: str | Path) -> ConvertResult:
    """Produce display + thumbnail WebP variants for an uploaded file.

    See module docstring. Sync (CPU-bound Pillow work); callers on the event
    loop may push it to a thread via anyio.to_thread / run_in_executor.
    """
    src = Path(src_path)
    dest_dir = Path(dest_dir)
    try:
        img = _load_image(src)
    except UnidentifiedImageError:
        return ConvertResult(is_image=False)

    with img:
        try:
            img.load()
        except OSError:
            return ConvertResult(is_image=False)  # corrupt/truncated — store, no preview

        has_gps = _has_gps(img)
        src_format = img.format
        oriented = ImageOps.exif_transpose(img)

    width, height = oriented.size
    dest_dir.mkdir(parents=True, exist_ok=True)
    stem = src.stem

    serve_original = (
        src_format in WEB_FRIENDLY_FORMATS
        and max(width, height) <= DISPLAY_MAX
        and not has_gps  # GPS present -> must re-encode so display copy is stripped
    )

    display_path: Optional[Path] = None
    if not serve_original:
        display_path = dest_dir / f"{stem}_display.webp"
        _save_webp(oriented, display_path, DISPLAY_MAX)

    thumb_path = dest_dir / f"{stem}_thumb.webp"
    _save_webp(oriented, thumb_path, THUMB_MAX)

    return ConvertResult(
        is_image=True,
        width=width,
        height=height,
        display_path=display_path,
        thumb_path=thumb_path,
        display_is_original=serve_original,
    )


def make_avatar(src_path: str | Path, dest_path: str | Path, max_dim: int = 256) -> tuple[int, int]:
    """Convert any supported image to a small avatar WebP (EXIF stripped).

    Returns the saved (width, height). Raises UnsupportedFormat /
    UnidentifiedImageError / OSError for undecodable input.
    """
    dest = Path(dest_path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    with _load_image(Path(src_path)) as img:
        img.load()
        oriented = ImageOps.exif_transpose(img)
    out = _flatten(oriented)
    out.thumbnail((max_dim, max_dim), Image.LANCZOS)
    out.save(dest, format="WEBP", quality=WEBP_QUALITY)
    return out.size
