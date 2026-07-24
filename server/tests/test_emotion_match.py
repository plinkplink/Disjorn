"""Emotion-tag matcher tests: the deterministic ladder (aliases -> lexicon ->
stemming -> fuzzy -> tokens) and its integration with chibi.resolve()."""

from pathlib import Path

import pytest

from app.services import chibi, emotion_match

# A pack vocabulary shaped like Claudette's (normalized, as chibi indexes it).
KEYS = {
    "smug", "sly", "amused", "laughing", "cryinglaughing", "curious",
    "fascinated", "scheming", "shrugging", "unimpressed", "eyeroll",
    "content", "hearteyes", "confused", "exhausted", "sad", "mindblown",
}


# ---------------------------------------------------------------------------
# Ladder unit tests (pure function)
# ---------------------------------------------------------------------------

def test_exact_key_returned_as_is():
    assert emotion_match.match("Smug", KEYS) == "smug"
    assert emotion_match.match("Heart-Eyes", KEYS) == "hearteyes"
    assert emotion_match.match("crying laughing", KEYS) == "cryinglaughing"


def test_lexicon_synonyms():
    assert emotion_match.match("Wry", KEYS) == "sly"
    assert emotion_match.match("Intrigued", KEYS) == "curious"
    assert emotion_match.match("Smirk", KEYS) == "sly"
    assert emotion_match.match("deadpan", KEYS) == "unimpressed"


def test_lexicon_candidate_order_falls_through_shallow_packs():
    # "wry" prefers sly; a pack without Sly gets the next candidate.
    assert emotion_match.match("wry", KEYS - {"sly"}) == "amused"


def test_stemming_both_directions():
    assert emotion_match.match("Shrug", KEYS) == "shrugging"  # tag < key
    assert emotion_match.match("Laughs", KEYS) == "laughing"
    assert emotion_match.match("Amusement", KEYS) == "amused"  # noun form
    assert emotion_match.match("Confusion", {"confused"}) == "confused"


def test_fuzzy_rescues_typos_only():
    assert emotion_match.match("Schemeing", KEYS) == "scheming"
    # short words never fuzz: "sad" must not drift to anything else
    assert emotion_match.match("mud", KEYS) is None
    # distinct emotions stay distinct
    assert emotion_match.match("glad", KEYS - {"smug", "sly", "amused"}) is None


def test_multiword_tokens():
    assert emotion_match.match("quietly smug", KEYS) == "smug"
    assert emotion_match.match("evil grin", KEYS) == "scheming"


def test_punctuation_never_sinks_a_tag():
    # Claudette's live miss 2026-07-24: Fired Up / Fired-Up, and the quoted
    # form plink reported. All must land on the same chibi.
    keys = KEYS | {"pumped", "chargedup"}
    assert emotion_match.match("Fired Up", keys) == "pumped"
    assert emotion_match.match("Fired-Up", keys) == "pumped"
    assert emotion_match.match('"Fired Up"', keys) == "pumped"
    assert emotion_match.match("'Smug'", KEYS) == "smug"
    assert emotion_match.match("Heart-Eyes!", KEYS) == "hearteyes"


def test_pack_aliases_beat_lexicon():
    aliases = {"wry": ("Eye-Roll",)}
    assert emotion_match.match("Wry", KEYS, aliases) == "eyeroll"


def test_total_miss_is_none():
    assert emotion_match.match("photosynthesis", KEYS) is None
    assert emotion_match.match("", KEYS) is None


def test_deterministic():
    for _ in range(5):
        assert emotion_match.match("Wry", KEYS) == "sly"
        assert emotion_match.match("Shrug", KEYS) == "shrugging"


def test_parse_aliases(tmp_path: Path):
    f = tmp_path / "Aliases.txt"
    f.write_text(
        "# taste file\n"
        "Wry -> Sly, Amused\n"
        "side eye->Eye-Roll  # trailing comment\n"
        "malformed line without arrow\n"
        "-> NoAlias\n",
        encoding="utf-8",
    )
    parsed = emotion_match.parse_aliases(f)
    assert parsed == {"wry": ("Sly", "Amused"), "sideeye": ("Eye-Roll",)}
    assert emotion_match.parse_aliases(tmp_path / "missing.txt") == {}


# ---------------------------------------------------------------------------
# Integration through chibi.resolve()
# ---------------------------------------------------------------------------

@pytest.fixture()
def pack(tmp_path: Path) -> Path:
    root = tmp_path / "testpack"
    happy = root / "Happy_and_Confident"
    happy.mkdir(parents=True)
    for name in ("Smug.png", "Sly.png", "Laughing.png"):
        (happy / name).write_bytes(b"png")
    sadness = root / "Sadness"
    sadness.mkdir()
    (sadness / "Shrugging.png").write_bytes(b"png")
    chibi.clear_cache()
    yield root
    chibi.clear_cache()


def test_resolve_exact_still_wins(pack: Path):
    assert chibi.resolve(str(pack), "Smug") == \
        "chibi:testpack/Happy_and_Confident/Smug.png"


def test_resolve_via_lexicon(pack: Path):
    assert chibi.resolve(str(pack), "Wry") == \
        "chibi:testpack/Happy_and_Confident/Sly.png"


def test_resolve_via_stemming(pack: Path):
    assert chibi.resolve(str(pack), "Shrug") == \
        "chibi:testpack/Sadness/Shrugging.png"


def test_resolve_miss_returns_none(pack: Path):
    assert chibi.resolve(str(pack), "photosynthesis") is None


def test_resolve_aliases_txt_overrides_and_cache_invalidates(pack: Path):
    assert chibi.resolve(str(pack), "Wry") == \
        "chibi:testpack/Happy_and_Confident/Sly.png"
    # Writing the taste file must take effect without a cache clear:
    # Aliases.txt mtime is part of the pack signature.
    (pack / "Aliases.txt").write_text("Wry -> Laughing\n", encoding="utf-8")
    assert chibi.resolve(str(pack), "Wry") == \
        "chibi:testpack/Happy_and_Confident/Laughing.png"
