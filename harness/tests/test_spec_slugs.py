"""Every spec filename is a slug the board verbs accept: the stem is the card
key, the branch, the build unit and the sidecar key."""

import sys
from pathlib import Path

HARNESS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HARNESS / "broker"))

import brokerd  # noqa: E402

SPECS = HARNESS.parent / "SPECS"


def bad_stems(specs: Path) -> list[str]:
    cards = brokerd._load_planroom_module().spec_files(specs)
    return [f.stem for f in cards if not brokerd.BOARD_SLUG_RE.match(f.stem)]


def test_every_spec_stem_matches_the_board_slug_pattern():
    assert SPECS.is_dir(), f"no SPECS/ at {SPECS}"
    bad = bad_stems(SPECS)
    assert not bad, ("not a canonical spec slug; rename to lowercase "
                     "YYYY-MM-DD-kebab-name.md:\n" + "\n".join(bad))


def test_an_undated_card_file_is_caught(tmp_path):
    for name in ("README", "TEMPLATE", "PASSDOWN-x", "2026-08-20-thing", "Foo"):
        (tmp_path / f"{name}.md").write_text("# x\n")
    assert bad_stems(tmp_path) == ["Foo"]
