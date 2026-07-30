"""Eviction cap (Claudette's floor, #custodian 2026-07-26): at most
`max_evictions` EVICT proposals per run — "let it be timid once so I can see
its taste before it gets volume." Deferred, not spared: the header says how
many were held back, and they return on later runs."""

from consolidation import ProposalKind, build_proposals
from consolidation_testlib import (
    FIXED_NOW,
    append_log,
    make_config,
    write_spine_entry,
)


def _seed_plain_entries(spine_dir, n):
    for i in range(n):
        write_spine_entry(
            spine_dir, f"5{i}-plain-{i}.md", f"Unreferenced fact {i}.", name=f"plain-{i}"
        )


def test_cap_limits_eviction_proposals_and_says_so(store, spine, spine_dir, log, log_path):
    _seed_plain_entries(spine_dir, 6)
    cfg = make_config(store=store, spine_dir=spine_dir, log_path=log_path, max_evictions=2)

    report = build_proposals(cfg, now=FIXED_NOW, store=store, spine=spine, log=log)
    evicts = [p for p in report.proposals if p.kind is ProposalKind.EVICT]
    assert len(evicts) == 2
    assert report.evictions_deferred == 4
    header = report.batch_header()
    assert "Eviction cap active" in header
    assert "4 eviction candidate(s)" in header
    assert "deferred, not spared" in header


def test_weakest_rent_goes_first(store, spine, spine_dir, log, log_path):
    # A: never referenced at all. B: referenced once, but outside the window
    # (rc 0, stale last-seen). C: referenced once inside the window (rc 1,
    # still a candidate under evict_max_references=1). Rank: A, B, C.
    write_spine_entry(spine_dir, "50-a.md", "Fact A.", name="entry-a")
    write_spine_entry(spine_dir, "51-b.md", "Fact B.", name="entry-b")
    write_spine_entry(spine_dir, "52-c.md", "Fact C.", name="entry-c")
    append_log(log_path, returned_ids=["entry-b"], days_ago=40)
    append_log(log_path, returned_ids=["entry-c"], days_ago=1)
    cfg = make_config(
        store=store, spine_dir=spine_dir, log_path=log_path,
        evict_max_references=1, max_evictions=2,
    )

    report = build_proposals(cfg, now=FIXED_NOW, store=store, spine=spine, log=log)
    kept = [p.target for p in report.proposals if p.kind is ProposalKind.EVICT]
    assert kept == ["entry-a", "entry-b"]
    assert report.evictions_deferred == 1


def test_compressions_are_never_capped(store, spine, spine_dir, log, log_path):
    # 3 plain (evictable) + 2 constraint-shaped (compress) with cap 1:
    # evictions capped to 1, BOTH compressions survive untouched.
    _seed_plain_entries(spine_dir, 3)
    for i in range(2):
        write_spine_entry(
            spine_dir, f"6{i}-rule-{i}.md",
            f"Never do the thing {i}, because it broke prod.", name=f"rule-{i}"
        )
    cfg = make_config(store=store, spine_dir=spine_dir, log_path=log_path, max_evictions=1)

    report = build_proposals(cfg, now=FIXED_NOW, store=store, spine=spine, log=log)
    counts = report.counts()
    assert counts["evict"] == 1
    assert counts["compress"] == 2
    assert report.evictions_deferred == 2


def test_negative_cap_means_uncapped(store, spine, spine_dir, log, log_path):
    _seed_plain_entries(spine_dir, 5)
    cfg = make_config(store=store, spine_dir=spine_dir, log_path=log_path, max_evictions=-1)

    report = build_proposals(cfg, now=FIXED_NOW, store=store, spine=spine, log=log)
    assert report.counts()["evict"] == 5
    assert report.evictions_deferred == 0
    assert "Eviction cap" not in report.batch_header()


def test_default_cap_is_twenty(store, spine_dir, log_path):
    cfg = make_config(store=store, spine_dir=spine_dir, log_path=log_path)
    assert cfg.max_evictions == 20
