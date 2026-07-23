"""bootstrap.py — seat-aware assembly of ~/.claude/CLAUDE.md + MEMORY.md.

The seat split (spec 2026-07-22) makes bootstrap read RESIDENT_SEAT and gate
both the kernel bake and the MEMORY.md index on it. The load-bearing property
these tests pin is ZERO REGRESSION: with the seat unset or 'resident',
bootstrap bakes byte-for-byte what it baked before seats existed, given the
same spine. Plus: the build seat bakes the operational set and never
biography; an unknown seat fails closed; and no seat lets a resident-only
entry into a build's index.

bootstrap.main() reads os.environ and writes files; we drive it by
monkeypatching the environment and pointing HOME at a tmp dir. No chroma, no
network — bootstrap imports only house_memory.spine.
"""

import re

import pytest

from house_memory import Spine
from house_memory import bootstrap


def _write_split_spine(d):
    """Gable's post-split spine of record (same shape as test_spine's
    split_spine fixture): kernel non-negotiables in both seats, biography
    resident-only, operational entries in both."""
    d.mkdir(parents=True, exist_ok=True)
    (d / "00-nonnegotiables.md").write_text(
        "---\nname: nonnegotiables\nkernel: true\n---\nWalls are physical.\n")
    (d / "05-bearings.md").write_text(
        "---\nname: bearings\nseats: [resident]\n---\nI am Gable.\n")
    (d / "10-people.md").write_text(
        "---\nname: people\n---\nplink; Claudette.\n")
    (d / "40-cautions.md").write_text(
        "---\nname: cautions\n---\nCautions.\n")
    (d / "50-genesis.md").write_text(
        "---\nname: genesis\nseats: [resident]\n---\nOrigin story.\n")
    return d


def _kernel_body(claude_md_text):
    """The baked kernel, minus the trailing provenance comment (which carries
    a wall-clock stamp and so is deliberately excluded from byte comparison)."""
    return re.sub(r"\n\n<!-- assembled from .*? -->\n\Z", "",
                  claude_md_text, flags=re.S)


@pytest.fixture
def rig(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    spine_dir = _write_split_spine(tmp_path / "spine")

    def run(seat_env):
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.setenv("RESIDENT_SPINE_DIR", str(spine_dir))
        monkeypatch.setenv("RESIDENT_MEMORY_DIR", str(tmp_path / "memory"))
        if seat_env is None:
            monkeypatch.delenv("RESIDENT_SEAT", raising=False)
        else:
            monkeypatch.setenv("RESIDENT_SEAT", seat_env)
        rc = bootstrap.main()
        claude = home / ".claude" / "CLAUDE.md"
        memory = home / "MEMORY.md"
        return (rc,
                claude.read_text() if claude.exists() else None,
                memory.read_text() if memory.exists() else None)

    run.spine_dir = spine_dir
    return run


# --- zero regression: resident (and unset) == pre-seat behaviour ---------

@pytest.mark.parametrize("seat_env", [None, "resident", ""])
def test_resident_and_unset_bake_is_load_kernel_byte_for_byte(rig, seat_env):
    rc, claude, _ = rig(seat_env)
    assert rc == 0
    baked = _kernel_body(claude)
    # exactly what the pre-seat code (load_kernel) would have baked
    assert baked == Spine(rig.spine_dir).load_kernel()
    assert baked == "Walls are physical."
    # biography is NOT in the resident bake — it is retrieval-served, as today
    assert "I am Gable." not in baked
    assert "Origin story." not in baked


def test_resident_memory_index_lists_all_entries(rig):
    rc, _, memory = rig("resident")
    assert rc == 0
    for name in ("nonnegotiables", "bearings", "people", "cautions", "genesis"):
        assert f"[{name}]" in memory


# --- build seat: operational set baked, biography absent ------------------

def test_build_bake_has_operational_set_and_no_biography(rig):
    rc, claude, _ = rig("build")
    assert rc == 0
    baked = _kernel_body(claude)
    assert "Walls are physical." in baked   # 00-nonnegotiables (kernel)
    assert "plink; Claudette." in baked      # 10-people (non-kernel, baked)
    assert "Cautions." in baked              # 40-cautions
    assert "I am Gable." not in baked        # 05-bearings — biography
    assert "Origin story." not in baked      # 50-genesis — biography


def test_build_memory_index_hides_resident_only_entries(rig):
    """A build seat is never even told a resident-only entry exists."""
    rc, _, memory = rig("build")
    assert rc == 0
    assert "[nonnegotiables]" in memory
    assert "[people]" in memory
    assert "[cautions]" in memory
    assert "[bearings]" not in memory
    assert "[genesis]" not in memory


def test_provenance_comment_records_the_seat(rig):
    for seat in ("resident", "build"):
        _, claude, memory = rig(seat)
        assert f"(seat: {seat})" in claude
        assert f"(seat: {seat})" in memory


# --- fail closed ----------------------------------------------------------

def test_unknown_seat_fails_closed_and_writes_nothing(rig, tmp_path):
    rc, claude, memory = rig("admin")
    assert rc == 2
    assert claude is None  # nothing baked on an unknown seat
