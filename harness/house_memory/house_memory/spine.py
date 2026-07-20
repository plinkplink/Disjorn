"""Markdown spine loader (MEMORY-DESIGN layers 1+2, read side).

A spine is a directory of .md files in the resident's own repo. Each file may
open with simple frontmatter:

    ---
    name: identity-core
    kernel: true
    ---
    body markdown...

Recognized keys: `name` (defaults to the filename stem) and `kernel`
(true/false, defaults false). Unknown keys are preserved in `SpineEntry.meta`.
No YAML dependency — key: value lines only.

READ-ONLY by design: git operations (the witnessed-self-edit mechanism) are
WP-H8 consolidation tooling's job, not this library's. Kernel assembly into
CLAUDE.md (WP-H7) consumes `load_kernel()`.
"""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Union

from .retrieval_log import RetrievalLog


@dataclass
class SpineEntry:
    name: str
    kernel: bool
    body: str
    path: Path
    meta: dict = field(default_factory=dict)


class Spine:
    def __init__(self, spine_dir: Union[str, Path],
                 retrieval_log: Optional[RetrievalLog] = None):
        """retrieval_log: when set, serving a non-kernel entry via
        load_entry() appends a retrieval record whose returned_ids is the
        entry's name — spine rent measured in the same unified log episodic
        recalls use (WP-H8 consolidation's reference_counts() keys spine
        entries by name). Kernel loads and list_entries() never log: the
        kernel rides every turn (its rent is capped, not metered) and
        listing metadata is not serving content into context."""
        self.spine_dir = Path(spine_dir)
        self.retrieval_log = retrieval_log
        if not self.spine_dir.is_dir():
            raise FileNotFoundError(f"spine dir not found: {self.spine_dir}")

    def list_entries(self) -> list[SpineEntry]:
        """All entries, deterministic order (sorted by filename)."""
        entries = []
        for path in sorted(self.spine_dir.glob("*.md")):
            entries.append(_parse_entry(path))
        return entries

    def load_entry(self, name: str) -> SpineEntry:
        """Entry by frontmatter name (falling back to filename stem)."""
        for entry in self.list_entries():
            if entry.name == name:
                if self.retrieval_log is not None and not entry.kernel:
                    self.retrieval_log.log(
                        query=f"spine:{name}",
                        subject_filter=None,
                        raw_ids=[entry.name],
                        distances=[None],
                        returned_ids=[entry.name],
                    )
                return entry
        raise KeyError(f"no spine entry named {name!r} in {self.spine_dir}")

    def load_kernel(self) -> str:
        """Concatenated bodies of all kernel entries, in filename order.
        This is what rides along on every turn — hardest rent in the house."""
        bodies = [e.body for e in self.list_entries() if e.kernel]
        return "\n\n".join(b.strip() for b in bodies if b.strip())


def _parse_entry(path: Path) -> SpineEntry:
    text = path.read_text(encoding="utf-8")
    meta, body = _split_frontmatter(text)
    name = str(meta.get("name") or path.stem)
    kernel = _as_bool(meta.get("kernel", False))
    return SpineEntry(name=name, kernel=kernel, body=body, path=path, meta=meta)


def _split_frontmatter(text: str) -> tuple[dict, str]:
    """Parse a leading `--- ... ---` block of `key: value` lines. Files
    without frontmatter are all body."""
    lines = text.split("\n")
    if not lines or lines[0].strip() != "---":
        return {}, text
    meta: dict = {}
    for i in range(1, len(lines)):
        stripped = lines[i].strip()
        if stripped == "---":
            body = "\n".join(lines[i + 1 :])
            return meta, body
        if not stripped or stripped.startswith("#"):
            continue
        if ":" not in stripped:
            # not simple frontmatter after all — treat whole file as body
            return {}, text
        key, _, value = stripped.partition(":")
        meta[key.strip()] = value.strip()
    # opening --- never closed: treat whole file as body
    return {}, text


def _as_bool(value) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("true", "yes", "1")
