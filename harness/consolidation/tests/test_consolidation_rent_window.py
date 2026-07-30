"""Rent window (Claudette's ruling, #custodian 2026-07-26): "slow-moving
spine needs time to go stale." Rent (evict/compress) is assessed over
`rent_window_days`; promotion heat stays on `window_days` — widening one
cannot loosen the other."""

from consolidation import ProposalKind, build_proposals
from consolidation_testlib import FIXED_NOW, add_memory, append_log, make_config, write_spine_entry


def test_reference_inside_rent_window_spares_the_entry(store, spine, spine_dir, log, log_path):
    # Referenced once 50 days ago: stale on a 30d window, alive on a 90d one.
    write_spine_entry(spine_dir, "50-slow.md", "Slow-moving fact.", name="slow")
    append_log(log_path, returned_ids=["slow"], days_ago=50)
    base = dict(store=store, spine_dir=spine_dir, log_path=log_path,
                window_days=30, spine_reads_logged_since="2026-01-01")

    narrow = build_proposals(make_config(**base), now=FIXED_NOW,
                             store=store, spine=spine, log=log)
    assert narrow.counts()["evict"] == 1  # 30d rent would condemn it

    wide = build_proposals(make_config(**base, rent_window_days=90), now=FIXED_NOW,
                           store=store, spine=spine, log=log)
    assert wide.counts()["evict"] == 0  # 90d rent says it's still earning


def test_promotion_heat_stays_on_window_days(store, spine, spine_dir, log, log_path):
    # 3 recalls 60 days ago: hot on a 90d window, cold on the 30d one.
    # Widening RENT must not loosen promotion.
    add_memory(store, "old pattern", mid="m-old")
    for _ in range(3):
        append_log(log_path, returned_ids=["m-old"], days_ago=60)
    cfg = make_config(store=store, spine_dir=spine_dir, log_path=log_path,
                      window_days=30, rent_window_days=90)

    report = build_proposals(cfg, now=FIXED_NOW, store=store, spine=spine, log=log)
    assert report.counts()["promote"] == 0


def test_epoch_gate_measures_against_rent_window(store, spine, spine_dir, log, log_path):
    write_spine_entry(spine_dir, "50-plain.md", "Unreferenced fact.", name="plain")
    cfg = make_config(
        store=store, spine_dir=spine_dir, log_path=log_path,
        window_days=30, rent_window_days=90,
        spine_reads_logged_since="2026-06-01",  # 49d old: >30, <90
    )

    report = build_proposals(cfg, now=FIXED_NOW, store=store, spine=spine, log=log)
    assert report.counts()["evict"] == 0
    assert "90d rent window" in report.rent_inactive_reason


def test_removal_evidence_carries_rent_window(store, spine, spine_dir, log, log_path):
    write_spine_entry(spine_dir, "50-plain.md", "Unreferenced fact.", name="plain")
    cfg = make_config(store=store, spine_dir=spine_dir, log_path=log_path,
                      window_days=30, rent_window_days=90)

    report = build_proposals(cfg, now=FIXED_NOW, store=store, spine=spine, log=log)
    evict = next(p for p in report.proposals if p.kind is ProposalKind.EVICT)
    assert evict.evidence.window_days == 90
    assert "(rent 90d)" in report.batch_header()


def test_default_rent_window_is_window_days(store, spine_dir, log_path):
    cfg = make_config(store=store, spine_dir=spine_dir, log_path=log_path, window_days=30)
    assert cfg.rent_window() == 30
