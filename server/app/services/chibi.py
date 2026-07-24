"""Chibi emote pack loader + resolver (WP8).

A chibi pack follows Claudette's layout (see /home/plink/bots/claudette/chibis/):

    <pack_dir>/
      Emotions.txt                 # taxonomy: "# Category" headers, one emotion per line
      Happy_and_Confident/*.png    # category subdirs of PNGs named after emotions
      Curiosity_and_Cunning/*.png
      ...

Resolution (used by routers/messages.py on bot message create — call signature
`resolve(pack, emotion)` is load-bearing, keep it):

    resolve("claudette", "heart eyes") -> "chibi:claudette/Calm_and_Content/Heart-Eyes.png"

- `pack` is either a filesystem path (the bots.chibi_pack column) or a bare
  pack name looked up under DATA_DIR/assets/chibi_packs/<name>/.
- Emotions match case-insensitively against PNG filenames across all category
  subdirs; spaces/hyphens/underscores are ignored ("Heart-Eyes" == "heart eyes").
- A non-exact tag falls through to services/emotion_match.py: a deterministic
  ladder of pack-level Aliases.txt overrides, a built-in synonym lexicon,
  stemming, and high-cutoff fuzzy matching ("Wry" -> Sly.png). Misses are
  logged and still resolve to None.
- Returns an emote_ref string "chibi:{pack_name}/{Category}/{File.png}" or None.
  The client resolves emote_refs to GET /chibi/{pack}/{category}/{filename}
  (served by routers/summarize.py, the content-services router).

Pack indexes are cached in-memory and invalidated via an mtime signature over
the pack dir + its category subdirs.

Seeding: `ensure_default_pack()` copies repo-level seed packs from
app/assets_seed/chibi_packs/ into DATA_DIR/assets/chibi_packs/ on first use.
It is called lazily from the resolver/serving paths — never at import time.
"""

import logging
import re
import shutil
from pathlib import Path
from typing import Optional

from ..config import get_settings
from . import emotion_match

logger = logging.getLogger(__name__)

SEED_DIR = Path(__file__).resolve().parent.parent / "assets_seed" / "chibi_packs"

ALIASES_FILENAME = "Aliases.txt"

# str(pack_dir) -> (mtime_signature,
#                   {normalized_emotion: (category, filename)},
#                   parsed Aliases.txt)
_index_cache: dict[
    str, tuple[tuple, dict[str, tuple[str, str]], dict[str, tuple[str, ...]]]
] = {}

_SAFE_COMPONENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._-]*$")


def _normalize(name: str) -> str:
    """Case-insensitive emotion key: letters and digits only (quotes and
    punctuation in tags must not sink them). Must stay byte-compatible with
    emotion_match.normalize."""
    return re.sub(r"[^a-z0-9]+", "", name.lower())


def packs_root() -> Path:
    return get_settings().data_dir / "assets" / "chibi_packs"


def ensure_default_pack() -> None:
    """Copy seed packs (app/assets_seed/chibi_packs/*) into DATA_DIR once.

    Idempotent and cheap when the destination already exists. Lazily invoked
    from resolve()/safe_file() so main.py needs no startup hook.
    """
    if not SEED_DIR.is_dir():
        return
    root = packs_root()
    for seed_pack in sorted(SEED_DIR.iterdir()):
        if not seed_pack.is_dir():
            continue
        dest = root / seed_pack.name
        if not dest.exists():
            root.mkdir(parents=True, exist_ok=True)
            shutil.copytree(seed_pack, dest)


def _pack_dir(pack: str) -> Optional[Path]:
    """Resolve a pack reference (path or bare name) to a directory, or None."""
    if "/" in pack or "\\" in pack:
        # bots.chibi_pack stores a filesystem path
        path = Path(pack)
        return path if path.is_dir() else None
    if not _SAFE_COMPONENT.match(pack):
        return None
    ensure_default_pack()
    path = packs_root() / pack
    return path if path.is_dir() else None


def pack_exists(pack: str) -> bool:
    """True if `pack` (bare name or filesystem path) resolves to a pack directory.

    Public wrapper over _pack_dir for admin surfaces that want to validate a
    `bots.chibi_pack` value before storing it (routers/bots_admin.py, cli.py).
    """
    return _pack_dir(pack) is not None


def _mtime_signature(pack_dir: Path) -> tuple:
    sig: list = [pack_dir.stat().st_mtime_ns]
    # Editing a file doesn't touch its directory's mtime, so Aliases.txt
    # joins the signature explicitly (0 = absent).
    try:
        sig.append((pack_dir / ALIASES_FILENAME).stat().st_mtime_ns)
    except OSError:
        sig.append(0)
    for sub in sorted(p for p in pack_dir.iterdir() if p.is_dir()):
        sig.append((sub.name, sub.stat().st_mtime_ns))
    return tuple(sig)


def _build_index(pack_dir: Path) -> dict[str, tuple[str, str]]:
    """{normalized emotion: (Category, File.png)} from category subdirs."""
    index: dict[str, tuple[str, str]] = {}
    for category in sorted(p for p in pack_dir.iterdir() if p.is_dir()):
        for png in sorted(category.glob("*.png")):
            index.setdefault(_normalize(png.stem), (category.name, png.name))
    return index


def _get_pack(
    pack_dir: Path,
) -> tuple[dict[str, tuple[str, str]], dict[str, tuple[str, ...]]]:
    """(emotion index, parsed Aliases.txt) for a pack, mtime-cached together."""
    key = str(pack_dir)
    try:
        sig = _mtime_signature(pack_dir)
    except OSError:
        _index_cache.pop(key, None)
        return {}, {}
    cached = _index_cache.get(key)
    if cached is not None and cached[0] == sig:
        return cached[1], cached[2]
    index = _build_index(pack_dir)
    aliases = emotion_match.parse_aliases(pack_dir / ALIASES_FILENAME)
    _index_cache[key] = (sig, index, aliases)
    return index, aliases


def _get_index(pack_dir: Path) -> dict[str, tuple[str, str]]:
    return _get_pack(pack_dir)[0]


def load_taxonomy(pack_dir: Path) -> dict[str, list[str]]:
    """Parse Emotions.txt: '# Category' headers + emotion names, one per line.

    Documentation/introspection helper — resolution itself is filename-driven.
    """
    taxonomy: dict[str, list[str]] = {}
    path = pack_dir / "Emotions.txt"
    if not path.is_file():
        return taxonomy
    category = "Uncategorized"
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("#"):
            category = line.lstrip("#").strip()
            taxonomy.setdefault(category, [])
        else:
            taxonomy.setdefault(category, []).append(line)
    return taxonomy


def resolve(pack: Optional[str], emotion: str) -> Optional[str]:
    """Resolve a bot's emotion tag to an emote_ref string, or None.

    Exact (normalized) filename match first; on a miss the tag goes through
    emotion_match's deterministic ladder (pack Aliases.txt -> built-in
    lexicon -> stemming -> fuzzy), so bots tag freely without being boxed
    into the pack's exact vocabulary. Total misses are logged — tune the
    lexicon/aliases from those lines, not from imagination.

    Never raises on bad input — unknown pack/emotion simply yields None
    (messages.py ignores unresolvable emotions silently).
    """
    if not pack or not emotion:
        return None
    pack_dir = _pack_dir(pack)
    if pack_dir is None:
        return None
    index, aliases = _get_pack(pack_dir)
    hit = index.get(_normalize(emotion))
    if hit is None:
        matched = emotion_match.match(emotion, index.keys(), aliases)
        if matched is None:
            logger.info(
                "chibi: unresolved emotion %r for pack %s", emotion, pack_dir.name
            )
            return None
        hit = index[matched]
        logger.debug(
            "chibi: mapped emotion %r -> %r for pack %s",
            emotion, hit[1], pack_dir.name,
        )
    category, filename = hit
    return f"chibi:{pack_dir.name}/{category}/{filename}"


def safe_file(pack: str, category: str, filename: str) -> Optional[Path]:
    """Path for GET /chibi/{pack}/{category}/{filename}; None if invalid.

    Path-traversal safe: every component must match a conservative charset
    (no separators, no leading dots), the file must be a .png, and the
    resolved path must stay inside the pack directory.
    """
    for component in (pack, category, filename):
        if not _SAFE_COMPONENT.match(component) or ".." in component:
            return None
    if not filename.lower().endswith(".png"):
        return None
    ensure_default_pack()
    pack_dir = packs_root() / pack
    if not pack_dir.is_dir():
        return None
    path = (pack_dir / category / filename).resolve()
    if not str(path).startswith(str(pack_dir.resolve()) + "/"):
        return None
    return path if path.is_file() else None


def clear_cache() -> None:
    """Drop all cached pack indexes — used by tests."""
    _index_cache.clear()
