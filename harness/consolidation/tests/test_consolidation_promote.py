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


# ==========================================================================
# Memory v2 phase 1 — the v1 defect, pinned end-to-end through the walker.
#
# v1 ran three nights, proposed thirty promotions, and every one was
# rejected. The arithmetic was right; the inputs were not. These tests are
# the regression fence around that.
# ==========================================================================

def test_self_query_reads_never_promote(store, spine, spine_dir, log, log_path):
    """Reading about a memory must not promote it.

    This is the 2026-07-28 slate in one test: nine of ten items were in
    Claudette's surfaced block AS she reviewed them, so her attention was
    indistinguishable from their usefulness. Twenty self-queries, well over
    the threshold of 3, and the walker must propose nothing."""
    add_memory(store, "a memory she kept poking at", mid="m-poked")
    for _ in range(20):
        append_log(log_path, returned_ids=["m-poked"], days_ago=1,
                   caller="self_query")
    cfg = _cfg(store, spine_dir, log_path)

    report = build_proposals(cfg, now=FIXED_NOW, store=store, spine=spine, log=log)
    assert [p for p in report.proposals if p.kind is ProposalKind.PROMOTE] == []


def test_consolidation_reads_never_promote(store, spine, spine_dir, log, log_path):
    """The walker must not warm what it touches while measuring."""
    add_memory(store, "read only by the walker", mid="m-walked")
    for _ in range(10):
        append_log(log_path, returned_ids=["m-walked"], days_ago=1,
                   caller="consolidation")
    cfg = _cfg(store, spine_dir, log_path)

    report = build_proposals(cfg, now=FIXED_NOW, store=store, spine=spine, log=log)
    assert [p for p in report.proposals if p.kind is ProposalKind.PROMOTE] == []


def test_pre_v2_lines_do_not_promote(store, spine, spine_dir, log, log_path):
    """Provenance cannot be backfilled, so unattributable history is inert
    rather than charitably counted as service."""
    add_memory(store, "recalled before the field existed", mid="m-legacy")
    for _ in range(9):
        append_log(log_path, returned_ids=["m-legacy"], days_ago=2, caller=None)
    cfg = _cfg(store, spine_dir, log_path)

    report = build_proposals(cfg, now=FIXED_NOW, store=store, spine=spine, log=log)
    assert [p for p in report.proposals if p.kind is ProposalKind.PROMOTE] == []


def test_mixed_traffic_counts_only_the_service_half(store, spine, spine_dir, log, log_path):
    """The realistic shape: a memory that genuinely serves turns AND gets
    poked at. Only the serving half is evidence."""
    add_memory(store, "genuinely useful and also discussed", mid="m-mixed")
    for _ in range(4):
        append_log(log_path, returned_ids=["m-mixed"], days_ago=2)  # service
    for _ in range(15):
        append_log(log_path, returned_ids=["m-mixed"], days_ago=2,
                   caller="self_query")
    cfg = _cfg(store, spine_dir, log_path)

    report = build_proposals(cfg, now=FIXED_NOW, store=store, spine=spine, log=log)
    promos = [p for p in report.proposals if p.kind is ProposalKind.PROMOTE]
    assert len(promos) == 1
    assert promos[0].evidence.reference_count == 4  # not 19


def test_last_referenced_ignores_self_query(store, spine, spine_dir, log, log_path):
    """The same defect lived in the 'last returned <date>' field, and it lied
    the same way — every 07-28 item read "last returned today" because she
    was reading them, right then, to review them."""
    add_memory(store, "served long ago, poked at today", mid="m-stale")
    for _ in range(4):
        append_log(log_path, returned_ids=["m-stale"], days_ago=20)   # service
    append_log(log_path, returned_ids=["m-stale"], days_ago=0,
               caller="self_query")                                   # today
    cfg = _cfg(store, spine_dir, log_path, window_days=60)

    report = build_proposals(cfg, now=FIXED_NOW, store=store, spine=spine, log=log)
    promos = [p for p in report.proposals if p.kind is ProposalKind.PROMOTE]
    assert len(promos) == 1
    last = promos[0].evidence.last_referenced_at
    assert last is not None
    # 20 days old, NOT today — the self-query read must not refresh it.
    assert last < (FIXED_NOW - __import__("datetime").timedelta(days=10)).isoformat()
