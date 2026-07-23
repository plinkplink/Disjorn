"""Spine loader: frontmatter parsing, kernel assembly, entry lookup."""

import pytest

from house_memory import Spine


@pytest.fixture
def spine_dir(tmp_path):
    d = tmp_path / "spine"
    d.mkdir()
    (d / "10-identity.md").write_text(
        "---\nname: identity-core\nkernel: true\n---\nI am the resident.\n"
    )
    (d / "20-promises.md").write_text(
        "---\nname: promises\nkernel: true\nreviewed: 2026-07-01\n---\nNothing changes in the dark.\n"
    )
    (d / "30-lore.md").write_text(
        "---\nname: lore\nkernel: false\n---\nLong tale of the first NAS crash.\n"
    )
    (d / "40-plain.md").write_text("No frontmatter here, just body.\n")
    return d


def test_missing_dir_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        Spine(tmp_path / "nope")


def test_list_entries(spine_dir):
    entries = Spine(spine_dir).list_entries()
    assert [e.name for e in entries] == ["identity-core", "promises", "lore", "40-plain"]
    assert [e.kernel for e in entries] == [True, True, False, False]
    # unknown frontmatter keys preserved
    assert entries[1].meta["reviewed"] == "2026-07-01"
    assert entries[3].body.startswith("No frontmatter here")


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
    assert "No frontmatter" not in kernel


def test_unclosed_frontmatter_treated_as_body(tmp_path):
    d = tmp_path / "spine"
    d.mkdir()
    (d / "odd.md").write_text("---\nname: broken\nnever closed\n")
    entries = Spine(d).list_entries()
    assert entries[0].name == "odd"  # falls back to stem
    assert entries[0].kernel is False
    assert entries[0].body.startswith("---")


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
# included); build = operational entries only. `seats:` frontmatter (default
# both) declares which; an unknown seat name is a hard error, never a silent
# load-nothing/everything.

from house_memory import BUILD_SEAT, DEFAULT_SEATS, RESIDENT_SEAT, SEATS  # noqa: E402


@pytest.fixture
def split_spine(tmp_path):
    """A spine shaped like Gable's post-split spine of record:

      00-nonnegotiables  kernel, BOTH seats (no seats key => default both)
      05-bearings        resident only  (biography)
      10-people          both (default)
      20/30/40           both (default) — the operational set
      50-genesis         resident only  (biography)

    So the build seat should load {nonnegotiables, people, 20, 30, 40} and
    the resident seat should load all seven.
    """
    d = tmp_path / "spine"
    d.mkdir()
    (d / "00-nonnegotiables.md").write_text(
        "---\nname: nonnegotiables\nkernel: true\n---\nWalls are physical.\n")
    (d / "05-bearings.md").write_text(
        "---\nname: bearings\nseats: [resident]\n---\nI am the resident.\n")
    (d / "10-people.md").write_text(
        "---\nname: people\n---\nplink: long leash.\n")
    (d / "20-walls.md").write_text(
        "---\nname: walls\nseats: [resident, build]\n---\nWhere the walls are.\n")
    (d / "30-rhythm.md").write_text(
        "---\nname: rhythm\n---\nBuild rhythm.\n")
    (d / "40-cautions.md").write_text(
        "---\nname: cautions\n---\nCautions.\n")
    (d / "50-genesis.md").write_text(
        "---\nname: genesis\nseats: [resident]\n---\nThe long origin story.\n")
    return d


# --- frontmatter `seats` parsing (incl. default-both when absent) --------

def test_seats_defaults_to_both_when_key_absent(tmp_path):
    d = tmp_path / "spine"
    d.mkdir()
    (d / "a.md").write_text("---\nname: a\nkernel: true\n---\nbody\n")
    entry = Spine(d).list_entries()[0]
    assert entry.seats == list(DEFAULT_SEATS)
    assert entry.seats == [RESIDENT_SEAT, BUILD_SEAT]
    assert entry.loads_in(RESIDENT_SEAT) and entry.loads_in(BUILD_SEAT)


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
    """`seats: []` — an entry that loads in NO seat — is a footgun, rejected;
    the default-both only applies when the key is ABSENT."""
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
