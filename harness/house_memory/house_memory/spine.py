"""Markdown spine loader (MEMORY-DESIGN layers 1+2, read side).

A spine is a directory of .md files in the resident's own repo. Each file may
open with simple frontmatter:

    ---
    name: identity-core
    kernel: true
    seats: [resident, build]
    ---
    body markdown...

Recognized keys:
  `name`   defaults to the filename stem.
  `kernel` true/false, defaults false.
  `seats`  which SEATS load this entry (see below); defaults to ALL seats.
Unknown keys are preserved in `SpineEntry.meta`. No YAML dependency — key:
value lines only.

SEATS (Gable's spine RO-cutover / seat-split, spec 2026-07-22).
A single spine of record serves two kinds of session, and they must not load
the same thing:
  * the RESIDENT seat  — a summon; loads the whole spine, biography included.
  * the BUILD seat     — a detached Claude Code build session under the same
                         key; loads the OPERATIONAL entries only. "House
                         knowledge travels, biography doesn't": a build seat
                         needs the guardrails and the map of the house, not
                         the autobiography.
`seats:` on an entry declares which seats load it. Absent => BOTH seats (the
documented default; house knowledge travels by default, only biography is
marked down to `[resident]`). An UNKNOWN seat name — in frontmatter or passed
to a query — is an ERROR, never a silent load-nothing/load-everything: this
surface fails closed and loud.

READ-ONLY by design: git operations (the witnessed-self-edit mechanism) are
WP-H8 consolidation tooling's job, not this library's. Kernel assembly into
CLAUDE.md (WP-H7) consumes `assemble_for_seat()` (or the seat-agnostic
`load_kernel()`, which it reduces to for the resident seat).
"""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Union

try:
    from .retrieval_log import RetrievalLog
except ImportError:  # direct-file import (bootstrap.py on a bare python)
    from retrieval_log import RetrievalLog  # type: ignore[no-redef]

# The only valid seat names. Fail-closed: anything else is an error, whether it
# appears in an entry's `seats:` frontmatter or is passed to a seat query.
RESIDENT_SEAT = "resident"
BUILD_SEAT = "build"
SEATS: frozenset[str] = frozenset({RESIDENT_SEAT, BUILD_SEAT})

# A `seats:` key that is ABSENT means the entry loads in every seat. This is the
# deliberate default — operational/house knowledge travels; only biography is
# explicitly narrowed to `[resident]`. (An entry that is PRESENT-but-empty is a
# footgun, not this default, and _parse_seats rejects it.)
DEFAULT_SEATS: tuple[str, ...] = (RESIDENT_SEAT, BUILD_SEAT)


@dataclass
class SpineEntry:
    name: str
    kernel: bool
    body: str
    path: Path
    meta: dict = field(default_factory=dict)
    # Which seats load this entry. Parsed from `seats:` frontmatter; defaults
    # to all seats when the key is absent. Order-preserving list, not a set, so
    # a diff of the spine reads the way the author wrote it.
    seats: list[str] = field(default_factory=lambda: list(DEFAULT_SEATS))

    def loads_in(self, seat: str) -> bool:
        """True if `seat` loads this entry. Caller is responsible for having
        validated `seat` (Spine._require_seat does that once per query)."""
        return seat in self.seats


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
        This is what rides along on every turn — hardest rent in the house.

        Seat-agnostic and UNCHANGED: it selects on `kernel`, never on `seats`.
        `assemble_for_seat("resident")` reduces to exactly this (every kernel
        entry is resident-visible), which is why the seat split is zero-
        regression for a summon — see assemble_for_seat."""
        bodies = [e.body for e in self.list_entries() if e.kernel]
        return "\n\n".join(b.strip() for b in bodies if b.strip())

    # ── seat-aware loading (spec 2026-07-22 seat-split) ──────────────────
    @staticmethod
    def _require_seat(seat: str) -> str:
        """Validate a seat name once, at the top of a query. Fail closed and
        loud: an unknown seat is a ValueError, never a silent load-nothing (an
        empty result would look like an intact-but-empty spine) or load-
        everything (which would leak biography into a build seat)."""
        if seat not in SEATS:
            raise ValueError(
                f"unknown seat {seat!r}; valid seats: {sorted(SEATS)}")
        return seat

    def entries_for_seat(self, seat: str) -> list[SpineEntry]:
        """All entries THIS seat loads, in filename order.

        resident => every entry (resident sees the whole spine).
        build    => only entries whose `seats:` includes `build` (the
                    operational set; biography, marked `[resident]`, is absent).
        This gates BOTH the CLAUDE.md bake (via assemble_for_seat) AND the
        MEMORY.md index (bootstrap.py), so a build seat is never even told a
        resident-only entry exists."""
        self._require_seat(seat)
        return [e for e in self.list_entries() if e.loads_in(seat)]

    def assemble_for_seat(self, seat: str) -> str:
        """The CLAUDE.md body baked for `seat`, in filename order.

        THE BAKE POLICY DIFFERS BY SEAT, and this is the design fork the spec
        asked to be surfaced rather than silently resolved:

          resident — bake KERNEL entries only; everything else is served on
                     demand by the WP-H7 retrieval loop. This is today's
                     behaviour byte-for-byte (all kernel entries are resident-
                     visible, so this equals load_kernel()). ZERO REGRESSION.

          build    — bake EVERY entry this seat loads, kernel flag ignored. A
                     detached build session has NO retrieval loop to serve a
                     non-kernel entry on demand, so an operational entry
                     (10-people, 20/30/40) that is not baked is simply ABSENT
                     from the session. Gable's spec says the build seat LOADS
                     10/20/30/40; the only way to honour that in a session that
                     cannot retrieve is to bake them. Resident-only entries are
                     excluded upstream by entries_for_seat, so biography never
                     rides a build seat regardless.

        The two policies are genuinely different (retrieve-the-rest vs bake-
        everything), so the branch is explicit, not disguised. Do NOT read this
        as a redesign of WP-H7 retrieval — it is the minimum needed to make the
        build seat's declared set actually present. Left for review: whether a
        build seat should instead gain a retrieval loop of its own (then it
        could bake kernel-only like the resident). That is a bigger change than
        this spec authorises; flagged, not taken."""
        self._require_seat(seat)
        entries = self.entries_for_seat(seat)
        if seat == BUILD_SEAT:
            bodies = [e.body for e in entries]                 # bake all visible
        else:  # RESIDENT_SEAT: kernel-only, retrieve the rest (today's model)
            bodies = [e.body for e in entries if e.kernel]
        return "\n\n".join(b.strip() for b in bodies if b.strip())


def _parse_entry(path: Path) -> SpineEntry:
    text = path.read_text(encoding="utf-8")
    meta, body = _split_frontmatter(text)
    name = str(meta.get("name") or path.stem)
    kernel = _as_bool(meta.get("kernel", False))
    try:
        seats = _parse_seats(meta.get("seats"))
    except ValueError as e:  # name the offending file — a bad seats key here
        raise ValueError(f"{path}: {e}") from e  # would silently mis-load a seat
    return SpineEntry(name=name, kernel=kernel, body=body, path=path,
                      meta=meta, seats=seats)


def _parse_seats(raw) -> list[str]:
    """Parse a `seats:` frontmatter value into a validated seat list.

    Accepts the natural spellings, since frontmatter values are bare strings
    (no YAML): `[resident, build]`, `resident, build`, or a single `resident`.
      absent (None)     -> DEFAULT_SEATS  (both; house knowledge travels)
      present but empty  -> ValueError     (an entry loading in no seat is a
                                            footgun, not the default)
      any unknown name   -> ValueError     (fail closed: never silently drop a
                                            seat or leak biography to a build)
    Order is preserved so the spine diff reads as authored."""
    if raw is None:
        return list(DEFAULT_SEATS)
    text = str(raw).strip()
    if text.startswith("[") and text.endswith("]"):
        text = text[1:-1]
    parts = [p.strip() for p in text.split(",") if p.strip()]
    if not parts:
        raise ValueError(f"seats: present but declares no seat: {raw!r}")
    unknown = [p for p in parts if p not in SEATS]
    if unknown:
        raise ValueError(
            f"seats: unknown seat(s) {unknown}; valid seats: {sorted(SEATS)}")
    return parts


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
