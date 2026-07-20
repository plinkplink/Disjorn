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
