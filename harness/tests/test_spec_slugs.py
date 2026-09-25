"""Every spec filename is a slug the board verbs accept: the stem is the card
key, the branch, the build unit and the sidecar key."""

import sys
from pathlib import Path

HARNESS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HARNESS / "broker"))

import brokerd  # noqa: E402

SPECS = HARNESS.parent / "SPECS"


def test_every_spec_stem_matches_the_board_slug_pattern():
    stems = sorted(p.stem for p in SPECS.glob("20*.md"))
    assert stems, f"no SPECS/20*.md under {SPECS}"
    bad = [s for s in stems if not brokerd.BOARD_SLUG_RE.match(s)]
    assert not bad, ("not a canonical spec slug; rename to lowercase "
                     "YYYY-MM-DD-kebab-name.md:\n" + "\n".join(bad))
