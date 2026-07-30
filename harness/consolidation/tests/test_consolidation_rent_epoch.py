"""Rent-epoch gate (Claudette's question, #custodian 2026-07-26): a spine
entry with no read data resolves as NO DATA, SKIP — never as unreferenced,
evict. Rent assessment runs only once `spine_reads_logged_since` is declared
AND has aged a full window. Declared, never inferred."""

from consolidation import ProposalKind, build_proposals
from consolidation_testlib import FIXED_NOW, add_memory, append_log, make_config, write_spine_entry


def _seed(spine_dir, store, log_path):
    # one evictable plain entry + one promotable episodic memory
    write_spine_entry(spine_dir, "50-plain.md", "Unreferenced fact.", name="plain")
    add_memory(store, "hot pattern", mid="m-hot")
    for _ in range(5):
        append_log(log_path, returned_ids=["m-hot"], days_ago=1)


def test_epoch_unset_means_skip_not_evict(store, spine, spine_dir, log, log_path):
    _seed(spine_dir, store, log_path)
    cfg = make_config(
        store=store, spine_dir=spine_dir, log_path=log_path,
        spine_reads_logged_since=None,
    )

    report = build_proposals(cfg, now=FIXED_NOW, store=store, spine=spine, log=log)
    assert report.counts()["evict"] == 0
    assert report.counts()["compress"] == 0
    assert report.rent_inactive_reason is not None
    header = report.batch_header()
    assert "rent assessment INACTIVE" in header
    assert "unmeasured, not unreferenced" in header
    # promotions are unaffected — the gate only stops removals
    assert report.counts()["promote"] == 1


def test_epoch_younger_than_window_still_skips(store, spine, spine_dir, log, log_path):
    _seed(spine_dir, store, log_path)
    cfg = make_config(
        store=store, spine_dir=spine_dir, log_path=log_path,
        window_days=30,
        spine_reads_logged_since="2026-07-10",  # 10 days before FIXED_NOW
    )

    report = build_proposals(cfg, now=FIXED_NOW, store=store, spine=spine, log=log)
    assert report.counts()["evict"] == 0
    assert "covers only" in report.rent_inactive_reason


def test_aged_epoch_opens_the_gate(store, spine, spine_dir, log, log_path):
    _seed(spine_dir, store, log_path)
    cfg = make_config(
        store=store, spine_dir=spine_dir, log_path=log_path,
        window_days=30,
        spine_reads_logged_since="2026-06-01",  # 49 days before FIXED_NOW
    )

    report = build_proposals(cfg, now=FIXED_NOW, store=store, spine=spine, log=log)
    assert report.rent_inactive_reason is None
    assert report.counts()["evict"] == 1


def test_unparseable_epoch_fails_closed(store, spine, spine_dir, log, log_path):
    _seed(spine_dir, store, log_path)
    cfg = make_config(
        store=store, spine_dir=spine_dir, log_path=log_path,
        spine_reads_logged_since="the day we wired it",
    )

    report = build_proposals(cfg, now=FIXED_NOW, store=store, spine=spine, log=log)
    assert report.counts()["evict"] == 0
    assert "unparseable" in report.rent_inactive_reason


def test_gate_disables_soft_target_bias_instead_of_zeroing_promotions(
    store, spine, spine_dir, log, log_path
):
    # Over-target spine + rent inactive: bias must NOT hold promotions to the
    # (forbidden, hence zero) reductions — promotions survive untouched.
    _seed(spine_dir, store, log_path)
    cfg = make_config(
        store=store, spine_dir=spine_dir, log_path=log_path,
        soft_target_spine_size=0,  # over target
        spine_reads_logged_since=None,
    )

    report = build_proposals(cfg, now=FIXED_NOW, store=store, spine=spine, log=log)
    assert report.counts()["promote"] == 1
    assert report.bias_applied is False
    assert report.promotions_suppressed == 0


def test_default_epoch_is_unset(store, spine_dir, log_path):
    cfg = make_config(store=store, spine_dir=spine_dir, log_path=log_path,
                      spine_reads_logged_since=None)
    assert cfg.spine_reads_logged_since is None
