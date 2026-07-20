"""Promotions: episodic -> spine, gated on retrieval-log reference counts."""

from consolidation import ProposalKind, build_proposals
from consolidation_testlib import FIXED_NOW, add_memory, append_log, make_config


def _cfg(store, spine_dir, log_path, **kw):
    return make_config(store=store, spine_dir=spine_dir, log_path=log_path, **kw)


def test_frequently_recalled_memory_is_promoted(store, spine, spine_dir, log, log_path):
    add_memory(store, "plink prefers TOML config files", mid="m-hot")
    for _ in range(4):
        append_log(log_path, returned_ids=["m-hot"], days_ago=2)
    cfg = _cfg(store, spine_dir, log_path)

    report = build_proposals(cfg, now=FIXED_NOW, store=store, spine=spine, log=log)
    promos = [p for p in report.proposals if p.kind is ProposalKind.PROMOTE]
    assert len(promos) == 1
    p = promos[0]
    assert p.target == "m-hot"
    # reference-count evidence is present and correct
    assert p.evidence.reference_count == 4
    assert p.evidence.window_days == cfg.window_days
    assert p.evidence.last_referenced_at is not None


def test_rarely_recalled_memory_is_not_promoted(store, spine, spine_dir, log, log_path):
    add_memory(store, "a passing detail", mid="m-cold")
    append_log(log_path, returned_ids=["m-cold"], days_ago=1)  # only 1 < threshold 3
    cfg = _cfg(store, spine_dir, log_path)

    report = build_proposals(cfg, now=FIXED_NOW, store=store, spine=spine, log=log)
    assert not [p for p in report.proposals if p.kind is ProposalKind.PROMOTE]


def test_references_outside_window_dont_count(store, spine, spine_dir, log, log_path):
    add_memory(store, "stale hits", mid="m-stale")
    for _ in range(5):
        append_log(log_path, returned_ids=["m-stale"], days_ago=90)  # window is 30d
    cfg = _cfg(store, spine_dir, log_path)

    report = build_proposals(cfg, now=FIXED_NOW, store=store, spine=spine, log=log)
    assert not [p for p in report.proposals if p.kind is ProposalKind.PROMOTE]


def test_already_in_spine_is_not_repromoted(store, spine, spine_dir, log, log_path):
    content = "plink prefers TOML config files"
    add_memory(store, content, mid="m-dup")
    for _ in range(4):
        append_log(log_path, returned_ids=["m-dup"], days_ago=2)
    # the spine already carries this pattern
    from consolidation_testlib import write_spine_entry

    write_spine_entry(spine_dir, "10-prefs.md", f"Note: {content}.", name="prefs")
    cfg = _cfg(store, spine_dir, log_path)

    report = build_proposals(cfg, now=FIXED_NOW, store=store, spine=spine, log=log)
    assert not [p for p in report.proposals if p.kind is ProposalKind.PROMOTE]


def test_superseded_memory_is_not_promoted(store, spine, spine_dir, log, log_path):
    add_memory(store, "old belief", mid="m-old", superseded_by="m-new")
    for _ in range(5):
        append_log(log_path, returned_ids=["m-old"], days_ago=2)
    cfg = _cfg(store, spine_dir, log_path)

    report = build_proposals(cfg, now=FIXED_NOW, store=store, spine=spine, log=log)
    assert not [p for p in report.proposals if p.kind is ProposalKind.PROMOTE]


def test_max_promotions_cap(store, spine, spine_dir, log, log_path):
    for i in range(6):
        add_memory(store, f"hot memory number {i}", mid=f"m-{i}")
        for _ in range(3 + i):  # all above threshold, varying strength
            append_log(log_path, returned_ids=[f"m-{i}"], days_ago=1)
    cfg = _cfg(store, spine_dir, log_path, max_promotions=3)

    report = build_proposals(cfg, now=FIXED_NOW, store=store, spine=spine, log=log)
    promos = [p for p in report.proposals if p.kind is ProposalKind.PROMOTE]
    assert len(promos) == 3
    # strongest evidence kept (m-5 had the most hits)
    kept = {p.target for p in promos}
    assert "m-5" in kept and "m-0" not in kept
