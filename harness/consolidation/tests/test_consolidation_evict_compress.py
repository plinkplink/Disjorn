"""Evictions and compressions of under-referenced spine entries."""

from datetime import timedelta

from consolidation import ProposalKind, build_proposals
from consolidation_testlib import (
    FIXED_NOW,
    append_log,
    make_config,
    write_spine_entry,
)


def _cfg(store, spine_dir, log_path, **kw):
    return make_config(store=store, spine_dir=spine_dir, log_path=log_path, **kw)


def test_unreferenced_plain_entry_is_evicted(store, spine, spine_dir, log, log_path):
    # a plain, non-constraint fact nobody has retrieved
    write_spine_entry(spine_dir, "20-fact.md", "The office plant is a pothos.", name="office-plant")
    cfg = _cfg(store, spine_dir, log_path)

    report = build_proposals(cfg, now=FIXED_NOW, store=store, spine=spine, log=log)
    evicts = [p for p in report.proposals if p.kind is ProposalKind.EVICT]
    assert len(evicts) == 1
    assert evicts[0].target == "office-plant"
    assert evicts[0].evidence.reference_count == 0


def test_referenced_entry_is_kept(store, spine, spine_dir, log, log_path):
    write_spine_entry(spine_dir, "20-fact.md", "The office plant is a pothos.", name="office-plant")
    append_log(log_path, returned_ids=["office-plant"], days_ago=1)  # rc=1 > evict_max 0
    cfg = _cfg(store, spine_dir, log_path)

    report = build_proposals(cfg, now=FIXED_NOW, store=store, spine=spine, log=log)
    assert not [p for p in report.proposals if p.kind is ProposalKind.EVICT]


def test_constraint_shaped_entry_compresses_not_evicts_via_kind(store, spine, spine_dir, log, log_path):
    write_spine_entry(
        spine_dir, "30-lesson.md",
        "Snapshot the store before extraction.",
        name="migration-lesson", kind="lesson",
    )
    cfg = _cfg(store, spine_dir, log_path)

    report = build_proposals(cfg, now=FIXED_NOW, store=store, spine=spine, log=log)
    assert not [p for p in report.proposals if p.kind is ProposalKind.EVICT]
    comps = [p for p in report.proposals if p.kind is ProposalKind.COMPRESS]
    assert len(comps) == 1
    assert comps[0].constraint_shaped is True


def test_constraint_detected_by_keyword(store, spine, spine_dir, log, log_path):
    # no kind/tags, but a constraint keyword in the body ("never")
    write_spine_entry(
        spine_dir, "30-why.md",
        "Never convert her store in place because a lost memory is unforgivable.",
        name="reversibility",
    )
    cfg = _cfg(store, spine_dir, log_path)

    report = build_proposals(cfg, now=FIXED_NOW, store=store, spine=spine, log=log)
    assert not [p for p in report.proposals if p.kind is ProposalKind.EVICT]
    assert [p for p in report.proposals if p.kind is ProposalKind.COMPRESS]


def test_constraint_detected_by_tag(store, spine, spine_dir, log, log_path):
    write_spine_entry(
        spine_dir, "30-promise.md",
        "The office plant is a pothos.",  # plain body, but tagged promise
        name="tagged", tags="promise, misc",
    )
    cfg = _cfg(store, spine_dir, log_path)

    report = build_proposals(cfg, now=FIXED_NOW, store=store, spine=spine, log=log)
    assert not [p for p in report.proposals if p.kind is ProposalKind.EVICT]
    assert [p for p in report.proposals if p.kind is ProposalKind.COMPRESS]


def test_kernel_entries_are_never_touched(store, spine, spine_dir, log, log_path):
    write_spine_entry(spine_dir, "00-kernel.md", "I am the custodian.", name="identity", kernel=True)
    cfg = _cfg(store, spine_dir, log_path)

    report = build_proposals(cfg, now=FIXED_NOW, store=store, spine=spine, log=log)
    assert report.proposals == []  # kernel excluded from removal


def test_young_entry_excluded_by_age_guard(store, spine, spine_dir, log, log_path):
    # entry created 5 days ago; min_spine_age_days=30 -> too young to judge
    since = (FIXED_NOW - timedelta(days=5)).date().isoformat()
    write_spine_entry(spine_dir, "20-new.md", "A brand new plain fact.", name="new-fact", since=since)
    cfg = _cfg(store, spine_dir, log_path, min_spine_age_days=30)

    report = build_proposals(cfg, now=FIXED_NOW, store=store, spine=spine, log=log)
    assert report.proposals == []


def test_old_entry_passes_age_guard(store, spine, spine_dir, log, log_path):
    since = (FIXED_NOW - timedelta(days=120)).date().isoformat()
    write_spine_entry(spine_dir, "20-old.md", "An old plain fact.", name="old-fact", since=since)
    cfg = _cfg(store, spine_dir, log_path, min_spine_age_days=30)

    report = build_proposals(cfg, now=FIXED_NOW, store=store, spine=spine, log=log)
    assert [p for p in report.proposals if p.kind is ProposalKind.EVICT]


def test_topic_variations_merge_to_one_compression(store, spine, spine_dir, log, log_path):
    # three constraint-shaped entries on one topic -> one merge-compress
    for i in range(3):
        write_spine_entry(
            spine_dir, f"4{i}-var.md",
            f"Lesson variant {i}: always snapshot first.",
            name=f"snapshot-{i}", kind="lesson", topic="migration-safety",
        )
    cfg = _cfg(store, spine_dir, log_path)

    report = build_proposals(cfg, now=FIXED_NOW, store=store, spine=spine, log=log)
    comps = [p for p in report.proposals if p.kind is ProposalKind.COMPRESS]
    assert len(comps) == 1  # merged
    assert len(comps[0].members) == 3
    assert comps[0].target == "topic:migration-safety"
