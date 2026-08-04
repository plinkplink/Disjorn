"""Dedup — the fourth walker re-activation gate.

Three defects live under the one word, and they fail in different directions,
so they get separate tests: clustering (noise), cluster-BEFORE-count (lost
signal), and spine containment (re-proposing forever). Plus the subject wall,
which is the guard that makes the other three safe to turn on.

Integration tests state their own `dedup_similarity` rather than leaning on the
default, so a future tuning of the default cannot silently change what these
tests are asserting.
"""

import pytest

from consolidation import ProposalKind, build_proposals
from consolidation.dedup import (
    Cluster,
    already_in_spine,
    cluster_records,
    containment,
    cosine,
    overlap,
)
from consolidation_testlib import (
    FIXED_NOW,
    add_memory,
    append_log,
    make_config,
    write_spine_entry,
)


def _cfg(store, spine_dir, log_path, **kw):
    return make_config(store=store, spine_dir=spine_dir, log_path=log_path, **kw)


def _rec(mid, content, emb, subject="plink"):
    return {"id": mid, "content": content, "embedding": emb,
            "metadata": {"subject": subject}}


# ── cosine ───────────────────────────────────────────────────────────────────

def test_cosine_basics():
    assert cosine([1.0, 0.0], [1.0, 0.0]) == pytest.approx(1.0)
    assert cosine([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0)


def test_missing_or_degenerate_vectors_are_never_similar():
    """'We cannot tell' must resolve to 'not similar'. A missing embedding can
    then only cause a false SPLIT (visible, harmless) and never a false MERGE
    (invisible, destroys a memory's shot at the spine)."""
    assert cosine(None, [1.0, 0.0]) == 0.0
    assert cosine([1.0, 0.0], None) == 0.0
    assert cosine([0.0, 0.0], [1.0, 0.0]) == 0.0
    assert cosine([1.0, 0.0], [1.0, 0.0, 0.0]) == 0.0  # mismatched dims


# ── clustering ───────────────────────────────────────────────────────────────

def test_identical_memories_form_one_cluster():
    v = [1.0, 0.0, 0.0]
    clusters = cluster_records([_rec("a", "same thing", v), _rec("b", "same thing", v)])
    assert len(clusters) == 1
    assert sorted(clusters[0].members) == ["a", "b"]


def test_dissimilar_memories_stay_separate():
    clusters = cluster_records([
        _rec("a", "toml config", [1.0, 0.0, 0.0]),
        _rec("b", "cat pictures", [0.0, 1.0, 0.0]),
    ])
    assert len(clusters) == 2


def test_subject_is_a_hard_wall_even_for_identical_text():
    """The guard that makes dedup safe. These two vectors are IDENTICAL, so
    every similarity measure says merge; merging them would emit one proposal
    attributing plink's preference to gable. A dedup pass that invents a
    memory is worse than no dedup pass."""
    v = [1.0, 0.0, 0.0]
    clusters = cluster_records([
        _rec("a", "prefers the terse version", v, subject="plink"),
        _rec("b", "prefers the terse version", v, subject="gable"),
    ])
    assert len(clusters) == 2
    assert {c.subject for c in clusters} == {"plink", "gable"}


def test_record_without_an_embedding_gets_its_own_cluster():
    v = [1.0, 0.0, 0.0]
    clusters = cluster_records([_rec("a", "x", v), _rec("b", "x", None)])
    assert len(clusters) == 2


def test_leader_clustering_does_not_chain():
    """A~B and B~C but A≁C. Single-link would swallow all three into one
    cluster, and a long enough chain of small steps merges memories that have
    nothing to do with each other. Leader clustering keeps every member within
    threshold of the SAME representative, so a cluster means something."""
    a = [1.0, 0.0, 0.0]
    b = [0.80, 0.60, 0.0]   # cos(a,b) = 0.80
    c = [0.28, 0.96, 0.0]   # cos(b,c) ~ 0.80, cos(a,c) ~ 0.28
    clusters = cluster_records([_rec("a", "aaa", a), _rec("b", "bb", b),
                                _rec("c", "c", c)], similarity=0.75)
    assert len(clusters) == 2
    sizes = sorted(c_.size for c_ in clusters)
    assert sizes == [1, 2]


def test_representative_is_the_longest_body_and_order_is_stable():
    """Deterministic input order (longest first, ties by id) means the fullest
    statement of the pattern represents it, and the same store produces the
    same clusters run after run instead of reading as churn."""
    v = [1.0, 0.0, 0.0]
    recs = [_rec("z", "short", v), _rec("a", "the much longer statement", v)]
    for ordering in (recs, list(reversed(recs))):
        clusters = cluster_records(ordering)
        assert len(clusters) == 1
        assert clusters[0].representative == "a"
        assert clusters[0].content == "the much longer statement"
        assert clusters[0].others == ["z"]


# ── spine containment ────────────────────────────────────────────────────────

MEMORY = ("plink prefers TOML config files because JSON has no comments and "
          "he reads these by hand at three in the morning")


def test_containment_is_asymmetric_on_purpose():
    """A spine line is usually a COMPRESSION of the memory that earned it, so
    it is shorter. Jaccard punishes exactly that length gap."""
    spine_line = "plink prefers TOML config files because JSON has no comments"
    assert containment(spine_line, MEMORY) == pytest.approx(1.0)
    assert containment(MEMORY, spine_line) < 1.0


def test_overlap_reads_both_directions():
    """Both shapes occur, so a single direction is a bug rather than a choice:
    a compressed spine line sits inside a long memory, and a paragraph-shaped
    spine entry can absorb a one-line memory whole (the shape the original
    substring check was written for)."""
    short_spine = "plink prefers TOML config files because JSON has no comments"
    long_spine = f"Background. {MEMORY} That is why the config lives in TOML."
    assert overlap(MEMORY, short_spine) == pytest.approx(1.0)
    assert overlap(MEMORY, long_spine) == pytest.approx(1.0)
    assert overlap(MEMORY, "the cat is on the mat and stays there") == 0.0


def test_compressed_spine_line_suppresses_repromotion():
    """The v1 behaviour this replaces: `needle in body` required the memory's
    ENTIRE text to be a literal substring of a spine body, which a compressed
    line essentially never satisfies — so promoted content was re-proposed on
    every run, forever."""
    spine_line = ("Note: plink prefers TOML config files because JSON has no "
                  "comments and he reads them by hand.")
    assert MEMORY not in spine_line.lower()          # substring says no
    assert already_in_spine(MEMORY, [spine_line.lower()])  # containment says yes


def test_unrelated_spine_does_not_suppress():
    assert not already_in_spine(MEMORY, ["the cat is on the mat and stays there"])


def test_empty_content_and_empty_spine_are_safe():
    assert not already_in_spine("", ["anything at all here"])
    assert not already_in_spine(MEMORY, [])


# ── integration: the two defects that change what reaches a reviewer ─────────

def test_near_duplicates_become_one_proposal(store, spine, spine_dir, log, log_path):
    """Defect 1 — the slate used to carry the same idea four times, each with
    its own evidence line."""
    text = "plink prefers TOML config files"
    for i in range(4):
        add_memory(store, text, mid=f"m-{i}")
    for i in range(4):
        append_log(log_path, returned_ids=[f"m-{i}"], days_ago=2)
    cfg = _cfg(store, spine_dir, log_path, dedup_similarity=0.9)

    report = build_proposals(cfg, now=FIXED_NOW, store=store, spine=spine, log=log)
    promos = [p for p in report.proposals if p.kind is ProposalKind.PROMOTE]
    assert len(promos) == 1
    assert promos[0].evidence.cluster_size == 4
    assert len(promos[0].members) == 3


def test_cluster_before_count_rescues_a_split_pattern(
    store, spine, spine_dir, log, log_path
):
    """Defect 2, the one that loses real signal rather than adding noise.

    Four memories saying one thing, each returned twice, against a threshold of
    3. Counting first tests every copy alone: four memories at 2, all below the
    bar, pattern dropped whole — despite the house having gone looking for it
    eight separate times."""
    text = "plink prefers TOML config files"
    for i in range(4):
        add_memory(store, text, mid=f"m-{i}")
        for _ in range(2):
            append_log(log_path, returned_ids=[f"m-{i}"], days_ago=2)
    cfg = _cfg(store, spine_dir, log_path, dedup_similarity=0.9,
               promote_min_references=3)

    report = build_proposals(cfg, now=FIXED_NOW, store=store, spine=spine, log=log)
    promos = [p for p in report.proposals if p.kind is ProposalKind.PROMOTE]
    assert len(promos) == 1, "pattern retrieved 8x must not read as 4 misses"
    assert promos[0].evidence.reference_count == 8


def test_pooled_count_is_events_not_copies(store, spine, spine_dir, log, log_path):
    """The inflation guard. Near-duplicates are exactly the memories that come
    back TOGETHER, so one recall returning all four must count once. Summing
    the members would report 12 for three events and manufacture the heat this
    pass exists to measure — the v1 defect in a new hat."""
    text = "plink prefers TOML config files"
    for i in range(4):
        add_memory(store, text, mid=f"m-{i}")
    for _ in range(3):
        append_log(log_path, returned_ids=["m-0", "m-1", "m-2", "m-3"], days_ago=2)
    cfg = _cfg(store, spine_dir, log_path, dedup_similarity=0.9)

    report = build_proposals(cfg, now=FIXED_NOW, store=store, spine=spine, log=log)
    promos = [p for p in report.proposals if p.kind is ProposalKind.PROMOTE]
    assert len(promos) == 1
    assert promos[0].evidence.reference_count == 3, "3 events, not 12 copies"


def test_reviewer_can_see_and_check_the_merge(store, spine, spine_dir, log, log_path):
    """A merge the reviewer cannot see is a merge they cannot check, and a
    pooled count they cannot audit. Both the ids and the arithmetic have to be
    on the rendered proposal."""
    text = "plink prefers TOML config files"
    for i in range(3):
        add_memory(store, text, mid=f"m-{i}")
    for i in range(3):
        append_log(log_path, returned_ids=[f"m-{i}"], days_ago=2)
    cfg = _cfg(store, spine_dir, log_path, dedup_similarity=0.9)

    report = build_proposals(cfg, now=FIXED_NOW, store=store, spine=spine, log=log)
    rendered = [p for p in report.proposals if p.kind is ProposalKind.PROMOTE][0].render()
    assert "near-identical" in rendered
    assert "retrieval events, not copies returned" in rendered
    for mid in ("m-0", "m-1", "m-2"):
        assert mid in rendered


def test_different_subjects_stay_separate_proposals(
    store, spine, spine_dir, log, log_path
):
    text = "prefers the terse version"
    add_memory(store, text, mid="m-plink", subject="plink")
    add_memory(store, text, mid="m-gable", subject="gable")
    for mid in ("m-plink", "m-gable"):
        for _ in range(3):
            append_log(log_path, returned_ids=[mid], days_ago=2)
    cfg = _cfg(store, spine_dir, log_path, dedup_similarity=0.9)

    report = build_proposals(cfg, now=FIXED_NOW, store=store, spine=spine, log=log)
    promos = [p for p in report.proposals if p.kind is ProposalKind.PROMOTE]
    assert len(promos) == 2
    assert {p.subject for p in promos} == {"plink", "gable"}


def test_a_compressed_spine_entry_stops_the_whole_cluster(
    store, spine, spine_dir, log, log_path
):
    """Defect 3 at the level it matters: the cluster is suppressed as a unit,
    so a promoted pattern stops coming back on every run."""
    text = ("plink prefers TOML config files because JSON has no comments and "
            "he reads these by hand at three in the morning")
    for i in range(3):
        add_memory(store, text, mid=f"m-{i}")
        for _ in range(2):
            append_log(log_path, returned_ids=[f"m-{i}"], days_ago=2)
    write_spine_entry(
        spine_dir, "10-prefs.md",
        "plink prefers TOML config files because JSON has no comments.",
        name="prefs",
    )
    cfg = _cfg(store, spine_dir, log_path, dedup_similarity=0.9)

    report = build_proposals(cfg, now=FIXED_NOW, store=store, spine=spine, log=log)
    assert not [p for p in report.proposals if p.kind is ProposalKind.PROMOTE]


def test_singleton_proposals_are_unchanged(store, spine, spine_dir, log, log_path):
    """Dedup must be invisible when there is nothing to dedup — no cluster
    language on a proposal that stands for exactly one memory."""
    add_memory(store, "plink runs Debian on the NAS", mid="m-solo")
    for _ in range(4):
        append_log(log_path, returned_ids=["m-solo"], days_ago=2)
    cfg = _cfg(store, spine_dir, log_path)

    report = build_proposals(cfg, now=FIXED_NOW, store=store, spine=spine, log=log)
    promos = [p for p in report.proposals if p.kind is ProposalKind.PROMOTE]
    assert len(promos) == 1
    assert promos[0].evidence.cluster_size == 1
    assert promos[0].members == []
    rendered = promos[0].render()
    assert "near-identical" not in rendered


def test_consolidation_reads_still_never_feed_heat(
    store, spine, spine_dir, log, log_path
):
    """The v1 defect, re-checked through the new pooled path. Group counting is
    a second place the caller filter could have been dropped — it is not, because
    it lives inside house_memory next to reference_counts."""
    text = "plink prefers TOML config files"
    for i in range(3):
        add_memory(store, text, mid=f"m-{i}")
        for _ in range(5):
            append_log(log_path, returned_ids=[f"m-{i}"], days_ago=2,
                       caller="consolidation")
    cfg = _cfg(store, spine_dir, log_path, dedup_similarity=0.9)

    report = build_proposals(cfg, now=FIXED_NOW, store=store, spine=spine, log=log)
    assert not [p for p in report.proposals if p.kind is ProposalKind.PROMOTE]
