"""Spine loader: frontmatter parsing, kernel assembly, entry lookup."""

import pytest

from house_memory import Spine


@pytest.fixture
def spine_dir(tmp_path):
    # Every entry declares its seats — redline 1 (2026-07-23) made the
    # declaration mandatory, so even the "legacy" fixture spine carries it.
    d = tmp_path / "spine"
    d.mkdir()
    (d / "10-identity.md").write_text(
        "---\nname: identity-core\nkernel: true\nseats: [resident]\n---\nI am the resident.\n"
    )
    (d / "20-promises.md").write_text(
        "---\nname: promises\nkernel: true\nreviewed: 2026-07-01\nseats: [resident]\n---\nNothing changes in the dark.\n"
    )
    (d / "30-lore.md").write_text(
        "---\nname: lore\nkernel: false\nseats: [resident]\n---\nLong tale of the first NAS crash.\n"
    )
    return d


def test_missing_dir_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        Spine(tmp_path / "nope")


def test_list_entries(spine_dir):
    entries = Spine(spine_dir).list_entries()
    assert [e.name for e in entries] == ["identity-core", "promises", "lore"]
    assert [e.kernel for e in entries] == [True, True, False]
    # unknown frontmatter keys preserved
    assert entries[1].meta["reviewed"] == "2026-07-01"


def test_load_entry_by_name(spine_dir):
    entry = Spine(spine_dir).load_entry("lore")
    assert entry.body.strip() == "Long tale of the first NAS crash."
    assert entry.path.name == "30-lore.md"


def test_load_entry_missing_raises(spine_dir):
    with pytest.raises(KeyError):
        Spine(spine_dir).load_entry("nonexistent")


def test_load_kernel_concatenates_kernel_entries_in_order(spine_dir):
    kernel = Spine(spine_dir).load_kernel()
    assert kernel == "I am the resident.\n\nNothing changes in the dark."
    assert "first NAS crash" not in kernel  # non-kernel excluded


def test_unclosed_frontmatter_is_an_error(tmp_path):
    """An unclosed `---` block used to fall back to all-body; under redline 1
    all-body means NO seats declaration, which is now a loud error — a
    malformed entry must never silently ride (or skip) a seat."""
    d = tmp_path / "spine"
    d.mkdir()
    (d / "odd.md").write_text("---\nname: broken\nnever closed\n")
    with pytest.raises(ValueError) as ei:
        Spine(d).list_entries()
    assert "seats" in str(ei.value)
    assert str(d / "odd.md") in str(ei.value)


def test_no_frontmatter_file_is_an_error(tmp_path):
    """A bare .md with no frontmatter cannot declare seats, so it is refused
    outright rather than defaulted anywhere (redline 1)."""
    d = tmp_path / "spine"
    d.mkdir()
    (d / "plain.md").write_text("No frontmatter here, just body.\n")
    with pytest.raises(ValueError) as ei:
        Spine(d).list_entries()
    assert "seats" in str(ei.value)


def test_load_entry_logs_spine_rent(spine_dir, tmp_path):
    from house_memory import RetrievalLog
    log = RetrievalLog(tmp_path / "retrieval.jsonl", resident="testres")
    spine = Spine(spine_dir, retrieval_log=log)

    spine.load_entry("lore")
    spine.load_entry("lore")
    records = log.read()
    assert len(records) == 2
    assert all(r.returned_ids == ["lore"] for r in records)
    assert all(r.query == "spine:lore" for r in records)
    assert all(r.resident == "testres" for r in records)
    assert log.reference_counts(window_days=7)["lore"] == 2


def test_kernel_entries_and_listing_never_log(spine_dir, tmp_path):
    from house_memory import RetrievalLog
    log = RetrievalLog(tmp_path / "retrieval.jsonl", resident="testres")
    spine = Spine(spine_dir, retrieval_log=log)

    spine.load_entry("identity-core")  # kernel entry: rent capped, not metered
    spine.load_kernel()
    spine.list_entries()
    assert log.read() == []


def test_no_log_configured_is_noop(spine_dir):
    entry = Spine(spine_dir).load_entry("lore")  # no retrieval_log: unchanged
    assert entry.name == "lore"


# ── seat-aware loading (spec 2026-07-22 seat-split) ──────────────────────
#
# One spine of record serves two seats. resident = whole spine (biography
# included); build = operational entries only. `seats:` frontmatter declares
# which, and the declaration is REQUIRED (redline 1: declared, never
# inferred); a missing key, an empty list, or an unknown seat name is a hard
# error, never a silent load-nothing/everything.

from house_memory import BUILD_SEAT, RESIDENT_SEAT, SEATS  # noqa: E402
from house_memory.spine import SpineEntry  # noqa: E402


@pytest.fixture
def split_spine(tmp_path):
    """A spine shaped like Gable's post-split spine of record — every entry
    DECLARES its seats (redline 1):

      00-nonnegotiables  kernel, seats: [resident, build]
      05-bearings        seats: [resident]  (biography)
      10-people          seats: [resident, build]
      20/30/40           seats: [resident, build] — the operational set
      50-genesis         seats: [resident]  (biography)

    So the build seat should load {nonnegotiables, people, 20, 30, 40} and
    the resident seat should load all seven.
    """
    d = tmp_path / "spine"
    d.mkdir()
    (d / "00-nonnegotiables.md").write_text(
        "---\nname: nonnegotiables\nkernel: true\nseats: [resident, build]\n---\nWalls are physical.\n")
    (d / "05-bearings.md").write_text(
        "---\nname: bearings\nseats: [resident]\n---\nI am the resident.\n")
    (d / "10-people.md").write_text(
        "---\nname: people\nseats: [resident, build]\n---\nplink: long leash.\n")
    (d / "20-walls.md").write_text(
        "---\nname: walls\nseats: [resident, build]\n---\nWhere the walls are.\n")
    (d / "30-rhythm.md").write_text(
        "---\nname: rhythm\nseats: [resident, build]\n---\nBuild rhythm.\n")
    (d / "40-cautions.md").write_text(
        "---\nname: cautions\nseats: [resident, build]\n---\nCautions.\n")
    (d / "50-genesis.md").write_text(
        "---\nname: genesis\nseats: [resident]\n---\nThe long origin story.\n")
    return d


# --- frontmatter `seats` parsing (missing declaration = loud error) ------

def test_seats_missing_is_an_error_never_inferred(tmp_path):
    """Redline 1: an absent `seats:` key must FAIL, not default to both.
    The error names the offending file and says what is missing."""
    d = tmp_path / "spine"
    d.mkdir()
    (d / "a.md").write_text("---\nname: a\nkernel: true\n---\nbody\n")
    with pytest.raises(ValueError) as ei:
        Spine(d).list_entries()
    msg = str(ei.value)
    assert "seats" in msg and "never inferred" in msg
    assert str(d / "a.md") in msg


def test_spine_entry_constructor_requires_seats():
    """Even a hand-built SpineEntry cannot dodge the declaration rule."""
    from pathlib import Path as _P
    with pytest.raises(ValueError):
        SpineEntry(name="x", kernel=True, body="b", path=_P("x.md"))
    with pytest.raises(ValueError):
        SpineEntry(name="x", kernel=True, body="b", path=_P("x.md"),
                   seats=["admin"])


@pytest.mark.parametrize("raw,expected", [
    ("[resident, build]", ["resident", "build"]),
    ("resident, build", ["resident", "build"]),
    ("[resident]", ["resident"]),
    ("resident", ["resident"]),
    ("[build]", ["build"]),
])
def test_seats_spellings_parse(tmp_path, raw, expected):
    d = tmp_path / "spine"
    d.mkdir()
    (d / "a.md").write_text(f"---\nname: a\nseats: {raw}\n---\nbody\n")
    assert Spine(d).list_entries()[0].seats == expected


def test_seats_order_is_preserved(tmp_path):
    d = tmp_path / "spine"
    d.mkdir()
    (d / "a.md").write_text("---\nname: a\nseats: [build, resident]\n---\nbody\n")
    assert Spine(d).list_entries()[0].seats == ["build", "resident"]


def test_unknown_seat_in_frontmatter_is_an_error(tmp_path):
    d = tmp_path / "spine"
    d.mkdir()
    (d / "a.md").write_text("---\nname: a\nseats: [resident, admin]\n---\nbody\n")
    with pytest.raises(ValueError) as ei:
        Spine(d).list_entries()
    assert "admin" in str(ei.value)
    assert str(d / "a.md") in str(ei.value)  # names the offending file


def test_empty_seats_key_is_an_error_not_a_silent_default(tmp_path):
    """`seats: []` — an entry that loads in NO seat — is a footgun, rejected
    just like a missing key (there is no default of any kind)."""
    d = tmp_path / "spine"
    d.mkdir()
    (d / "a.md").write_text("---\nname: a\nseats: []\n---\nbody\n")
    with pytest.raises(ValueError):
        Spine(d).list_entries()


# --- entries_for_seat ----------------------------------------------------

def test_resident_seat_loads_all_entries(split_spine):
    names = [e.name for e in Spine(split_spine).entries_for_seat(RESIDENT_SEAT)]
    assert names == ["nonnegotiables", "bearings", "people", "walls",
                     "rhythm", "cautions", "genesis"]


def test_build_seat_loads_operational_set_only(split_spine):
    names = [e.name for e in Spine(split_spine).entries_for_seat(BUILD_SEAT)]
    # includes the five operational entries...
    assert names == ["nonnegotiables", "people", "walls", "rhythm", "cautions"]
    # ...and excludes both biography entries.
    assert "bearings" not in names
    assert "genesis" not in names


def test_entries_for_unknown_seat_raises(split_spine):
    with pytest.raises(ValueError) as ei:
        Spine(split_spine).entries_for_seat("admin")
    assert "admin" in str(ei.value)


# --- assemble_for_seat: the bake ----------------------------------------

def test_resident_bake_equals_load_kernel_byte_for_byte(split_spine):
    """ZERO REGRESSION: the resident seat bakes exactly what load_kernel()
    baked before seats existed — kernel entries only, everything else
    retrieval-served. Byte-for-byte, given the same spine."""
    s = Spine(split_spine)
    assert s.assemble_for_seat(RESIDENT_SEAT) == s.load_kernel()
    # and concretely: only the kernel entry's body, nothing biographical.
    assert s.assemble_for_seat(RESIDENT_SEAT) == "Walls are physical."


def test_build_bake_includes_operational_and_excludes_biography(split_spine):
    baked = Spine(split_spine).assemble_for_seat(BUILD_SEAT)
    # the five operational entries are all baked (a build seat cannot retrieve)
    assert "Walls are physical." in baked      # 00-nonnegotiables (kernel)
    assert "plink: long leash." in baked       # 10-people (non-kernel, baked)
    assert "Where the walls are." in baked      # 20
    assert "Build rhythm." in baked             # 30
    assert "Cautions." in baked                 # 40
    # biography never rides a build seat
    assert "I am the resident." not in baked    # 05-bearings
    assert "The long origin story." not in baked  # 50-genesis


def test_build_bake_is_filename_ordered(split_spine):
    baked = Spine(split_spine).assemble_for_seat(BUILD_SEAT)
    assert (baked.index("Walls are physical.")
            < baked.index("plink: long leash.")
            < baked.index("Where the walls are.")
            < baked.index("Build rhythm.")
            < baked.index("Cautions."))


def test_assemble_for_unknown_seat_raises(split_spine):
    with pytest.raises(ValueError):
        Spine(split_spine).assemble_for_seat("everything")


def test_load_kernel_is_unchanged_and_seat_agnostic(split_spine):
    """load_kernel() still selects on `kernel`, never on `seats` — it is the
    resident bake and nothing about the split moved it."""
    assert Spine(split_spine).load_kernel() == "Walls are physical."


def test_valid_seat_names_are_exactly_resident_and_build():
    assert SEATS == frozenset({"resident", "build"})


# --- assemble refuses a kernel-less spine (redline 1, second clause) ------

def _kernelless_spine(tmp_path):
    d = tmp_path / "spine"
    d.mkdir()
    (d / "10-people.md").write_text(
        "---\nname: people\nseats: [resident, build]\n---\nplink: long leash.\n")
    (d / "30-rhythm.md").write_text(
        "---\nname: rhythm\nseats: [resident, build]\n---\nBuild rhythm.\n")
    return d


@pytest.mark.parametrize("seat", [RESIDENT_SEAT, BUILD_SEAT])
def test_assemble_aborts_when_no_kernel_entry_visible(tmp_path, seat):
    """A spine with no kernel/`00` entry must ABORT assembly for every seat —
    never bake a kernel-less session. The assembler itself refuses (the
    bootstrap-level empty check is not the wall)."""
    d = _kernelless_spine(tmp_path)
    with pytest.raises(ValueError) as ei:
        Spine(d).assemble_for_seat(seat)
    assert "no kernel entry" in str(ei.value)


def test_assemble_aborts_when_kernel_entry_is_hidden_from_seat(tmp_path):
    """The kernel check is PER-SEAT visibility, not mere existence: a kernel
    entry declared `[resident]` only leaves the build seat kernel-less, and
    the build bake must refuse rather than emit 10/20/30/40 without the
    non-negotiables."""
    d = _kernelless_spine(tmp_path)
    (d / "00-kern.md").write_text(
        "---\nname: kern\nkernel: true\nseats: [resident]\n---\nWalls.\n")
    s = Spine(d)
    assert s.assemble_for_seat(RESIDENT_SEAT) == "Walls."  # resident fine
    with pytest.raises(ValueError):
        s.assemble_for_seat(BUILD_SEAT)
