"""Framing guarantees: eviction reads as supersession, evidence is everywhere."""

from consolidation import ProposalKind, build_proposals
from consolidation_testlib import FIXED_NOW, add_memory, append_log, make_config, write_spine_entry


def _run(store, spine, spine_dir, log, log_path, **kw):
    cfg = make_config(store=store, spine_dir=spine_dir, log_path=log_path, **kw)
    return build_proposals(cfg, now=FIXED_NOW, store=store, spine=spine, log=log)


def test_eviction_renders_as_supersession_never_deletion(store, spine, spine_dir, log, log_path):
    write_spine_entry(spine_dir, "20-fact.md", "A plain unreferenced fact.", name="plain")
    report = _run(store, spine, spine_dir, log, log_path)
    evict = next(p for p in report.proposals if p.kind is ProposalKind.EVICT)
    text = evict.render().lower()
    assert "supersede" in text or "supersession" in text
    assert "commit" in text
    assert "reversible" in text
    assert "delete" not in text and "deletion" not in text


def test_every_proposal_carries_reference_count_evidence(store, spine, spine_dir, log, log_path):
    # one of each kind in a single run
    add_memory(store, "hot pattern worth promoting", mid="m-hot")
    for _ in range(4):
        append_log(log_path, returned_ids=["m-hot"], days_ago=1)
    write_spine_entry(spine_dir, "20-plain.md", "Unreferenced plain fact.", name="plain")
    write_spine_entry(spine_dir, "30-lesson.md", "A why worth keeping.", name="why", kind="lesson")

    report = _run(store, spine, spine_dir, log, log_path)
    kinds = {p.kind for p in report.proposals}
    assert {ProposalKind.PROMOTE, ProposalKind.EVICT, ProposalKind.COMPRESS} <= kinds
    for p in report.proposals:
        assert p.evidence is not None
        assert p.evidence.window_days == report.window_days
        # every rendered proposal shows the reviewer the count evidence
        rendered = p.render().lower()
        assert "evidence:" in rendered
        assert "returned" in rendered


def test_compression_keeps_the_why(store, spine, spine_dir, log, log_path):
    write_spine_entry(spine_dir, "30-why.md", "Because it burned us once.", name="why", kind="why")
    report = _run(store, spine, spine_dir, log, log_path)
    comp = next(p for p in report.proposals if p.kind is ProposalKind.COMPRESS)
    text = comp.render().lower()
    assert "compress" in text
    assert "delete" not in text
