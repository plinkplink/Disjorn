"""Soft-target bias: over target, reductions must be >= additions (suggestions)."""

from consolidation import ProposalKind, build_proposals
from consolidation_testlib import FIXED_NOW, add_memory, append_log, make_config, write_spine_entry


def _seed_promotions(store, log_path, n):
    for i in range(n):
        add_memory(store, f"promotable pattern {i}", mid=f"m-{i}")
        for _ in range(3 + i):
            append_log(log_path, returned_ids=[f"m-{i}"], days_ago=1)


def test_over_target_holds_promotions_to_reductions(store, spine, spine_dir, log, log_path):
    _seed_promotions(store, log_path, 5)  # 5 promotion candidates
    # one plain unreferenced entry -> exactly 1 reduction (eviction)
    write_spine_entry(spine_dir, "90-plain.md", "Unreferenced fact.", name="plain")
    # target below current spine size -> over target
    cfg = make_config(store=store, spine_dir=spine_dir, log_path=log_path, soft_target_spine_size=0)

    report = build_proposals(cfg, now=FIXED_NOW, store=store, spine=spine, log=log)
    assert report.over_target is True
    assert report.bias_applied is True
    assert report.additions() <= report.reductions()
    assert report.additions() == 1  # trimmed from 5 to match 1 reduction
    assert report.promotions_suppressed == 4
    # strongest-evidence promotion survived (m-4 had the most hits)
    kept = {p.target for p in report.proposals if p.kind is ProposalKind.PROMOTE}
    assert kept == {"m-4"}


def test_under_target_no_bias(store, spine, spine_dir, log, log_path):
    _seed_promotions(store, log_path, 3)
    write_spine_entry(spine_dir, "90-plain.md", "Unreferenced fact.", name="plain")
    cfg = make_config(store=store, spine_dir=spine_dir, log_path=log_path, soft_target_spine_size=100)

    report = build_proposals(cfg, now=FIXED_NOW, store=store, spine=spine, log=log)
    assert report.over_target is False
    assert report.bias_applied is False
    assert report.additions() == 3  # nothing suppressed


def test_over_target_but_reductions_win_no_trim(store, spine, spine_dir, log, log_path):
    _seed_promotions(store, log_path, 1)  # only 1 addition
    for i in range(3):  # 3 plain reductions
        write_spine_entry(spine_dir, f"9{i}-plain.md", f"Unreferenced fact {i}.", name=f"plain-{i}")
    cfg = make_config(store=store, spine_dir=spine_dir, log_path=log_path, soft_target_spine_size=0)

    report = build_proposals(cfg, now=FIXED_NOW, store=store, spine=spine, log=log)
    assert report.over_target is True
    assert report.bias_applied is False  # additions(1) already <= reductions(3)
    assert report.additions() == 1
