"""Absent-input safety: the deployment shapes that actually exist on the host.

Two distinct situations, deliberately given DIFFERENT behaviour:

1. **No spine at all** (`spine.dir` unset). Real: Claudette's spine is her
   system prompt, managed through her bot config — there is no directory of
   markdown entries on this host. The run must do the episodic-promotion half
   and emit ZERO evict/compress proposals. The dangerous failure this guards is
   "no spine dir" degrading into "empty spine, therefore evict everything".

2. **A spine dir that is configured but missing** (a stale path — exactly the
   bug the shipped config had). That must be a LOUD refusal, never a silent
   empty spine. Same for a missing episodic store dir, where continuing would
   have chromadb *create* an empty collection — a write, by a job that must
   never write.
"""

from __future__ import annotations

import io

import pytest

from consolidation import MissingInputError, build_proposals, post_report
from consolidation.__main__ import EXIT_CONFIG, EXIT_OK, main
from consolidation.config import load_config
from consolidation.model import ProposalKind
from consolidation_testlib import (
    FIXED_NOW,
    add_memory,
    append_log,
    make_config,
    write_spine_entry,
)


# ── 1. no spine at all: promotion-only, and NEVER "evict everything" ─────────

def _seed_promotable(store, log_path):
    add_memory(store, "hot promotable pattern", mid="m-hot")
    for _ in range(4):
        append_log(log_path, returned_ids=["m-hot"], days_ago=1)


def test_no_spine_dir_runs_promotion_only(store, log_path):
    _seed_promotable(store, log_path)
    cfg = make_config(store=store, spine_dir=None, log_path=log_path)

    report = build_proposals(cfg, now=FIXED_NOW, store=store)

    assert report.spine_present is False
    assert report.spine_size == 0
    kinds = {p.kind for p in report.proposals}
    assert kinds == {ProposalKind.PROMOTE}
    assert report.reductions() == 0


def test_no_spine_dir_never_becomes_evict_everything(store, spine_dir, log_path):
    """The load-bearing negative: spine files EXIST on disk, but the config
    declares no spine dir. Nothing may be proposed for removal — the entries
    are not even looked at, let alone judged unreferenced."""
    _seed_promotable(store, log_path)
    for i in range(5):
        write_spine_entry(spine_dir, f"{i}0-e.md", f"Plain fact {i}.", name=f"e{i}")

    cfg = make_config(store=store, spine_dir=None, log_path=log_path)
    report = build_proposals(cfg, now=FIXED_NOW, store=store)

    assert report.reductions() == 0
    assert report.counts()["evict"] == 0
    assert report.counts()["compress"] == 0


def test_no_spine_dir_is_stated_in_the_header(store, log_path):
    _seed_promotable(store, log_path)
    cfg = make_config(store=store, spine_dir=None, log_path=log_path)
    report = build_proposals(cfg, now=FIXED_NOW, store=store)

    header = report.batch_header()
    assert "NONE on disk" in header
    assert "episodic-promotion" in header
    # a spineless run is never "over target" -> the soft-target bias is inert
    assert report.over_target is False
    assert report.bias_applied is False


def test_no_spine_dir_dry_run_is_clean(store, log_path):
    _seed_promotable(store, log_path)
    cfg = make_config(store=store, spine_dir=None, log_path=log_path)
    report = build_proposals(cfg, now=FIXED_NOW, store=store)

    buf = io.StringIO()
    outcome = post_report(report, cfg, dry_run=True, out=buf)
    assert outcome.dry_run and outcome.posted == 0
    assert "NONE on disk" in buf.getvalue()


# ── 2. configured-but-missing paths: loud refusal ────────────────────────────

def test_missing_spine_dir_raises(store, tmp_path, log_path):
    cfg = make_config(
        store=store, spine_dir=tmp_path / "nope-does-not-exist", log_path=log_path
    )
    with pytest.raises(MissingInputError) as exc:
        build_proposals(cfg, now=FIXED_NOW, store=store)
    assert "spine dir configured but missing" in str(exc.value)


def test_missing_episodic_dir_raises_instead_of_creating_a_store(tmp_path, spine_dir, log_path):
    """chromadb's get_or_create_collection would happily CREATE a store under a
    stale data_dir. Refuse first: consolidation never writes."""
    ghost = tmp_path / "ghost-chroma"
    cfg = make_config(store=_FakeStoreHandle(ghost), spine_dir=spine_dir, log_path=log_path)
    with pytest.raises(MissingInputError) as exc:
        build_proposals(cfg, now=FIXED_NOW)  # store=None -> built from cfg
    assert "episodic store dir does not exist" in str(exc.value)
    assert not ghost.exists()  # nothing was brought into being


class _FakeStoreHandle:
    """Just enough shape for make_config (data_dir / collection_name)."""

    def __init__(self, data_dir):
        self.data_dir = data_dir
        self.collection_name = "ghost_memory"


def test_cli_exits_config_on_missing_spine_dir(store, tmp_path, log_path, monkeypatch):
    cdir = tmp_path / "cfgdir"
    cdir.mkdir()
    (cdir / "claudette.toml").write_text(
        "resident = 'claudette'\n"
        "active = false\n"
        "[episodic]\n"
        f"data_dir = '{store.data_dir}'\n"
        f"collection = '{store.collection_name}'\n"
        "[retrieval_log]\n"
        f"path = '{log_path}'\n"
        "[spine]\n"
        f"dir = '{tmp_path / 'gone'}'\n",
        encoding="utf-8",
    )
    rc = main(["--resident", "claudette", "--dry-run", "--config-dir", str(cdir)])
    assert rc == EXIT_CONFIG


def test_cli_runs_with_spine_dir_omitted(store, tmp_path, log_path):
    _seed_promotable(store, log_path)
    cdir = tmp_path / "cfgdir2"
    cdir.mkdir()
    (cdir / "claudette.toml").write_text(
        "resident = 'claudette'\n"
        "active = false\n"
        "[episodic]\n"
        f"data_dir = '{store.data_dir}'\n"
        f"collection = '{store.collection_name}'\n"
        "[retrieval_log]\n"
        f"path = '{log_path}'\n",
        encoding="utf-8",
    )
    out = io.StringIO()
    rc = main(["--resident", "claudette", "--dry-run", "--config-dir", str(cdir)], out=out)
    assert rc == EXIT_OK
    assert "NONE on disk" in out.getvalue()


# ── 3. config loading of the optional spine dir ──────────────────────────────

def test_unlogged_spine_reads_are_held_by_age_guard_and_never_acted_on(
    store, spine, spine_dir, log, log_path
):
    """INTEGRATION-NEEDS §1: until WP-H7 logs spine reads, EVERY spine entry
    reads as unreferenced. The two things that keep that safe, asserted:

      (a) `min_spine_age_days` — entries that have not spanned the window are
          not judged at all, whatever the (missing) log says;
      (b) proposes-never-acts — what does get judged becomes *text*, and a
          dry-run posts nothing and writes nothing.
    """
    from datetime import timedelta

    old = (FIXED_NOW - timedelta(days=120)).date().isoformat()
    young = (FIXED_NOW - timedelta(days=5)).date().isoformat()
    write_spine_entry(spine_dir, "20-old.md", "An old plain fact.", name="old", since=old)
    write_spine_entry(spine_dir, "21-young.md", "A young plain fact.", name="young", since=young)
    # log_path exists but contains NO spine-entry ids at all — today's reality
    append_log(log_path, returned_ids=["some-episodic-uuid"], days_ago=1)

    cfg = make_config(
        store=store, spine_dir=spine_dir, log_path=log_path, min_spine_age_days=30
    )
    before_spine = {p.name: p.read_bytes() for p in spine_dir.glob("*.md")}
    before_log = log_path.read_bytes()

    report = build_proposals(cfg, now=FIXED_NOW, store=store, spine=spine, log=log)

    targets = {p.target for p in report.proposals}
    assert "old" in targets       # (a) judged: old enough to have spanned the window
    assert "young" not in targets  # (a) age guard held
    # (b) nothing acted on: dry-run posts zero and the inputs are byte-identical
    outcome = post_report(report, cfg, dry_run=True, out=io.StringIO())
    assert outcome.dry_run and outcome.posted == 0
    assert {p.name: p.read_bytes() for p in spine_dir.glob("*.md")} == before_spine
    assert log_path.read_bytes() == before_log


# ── 3. config loading of the optional spine dir ──────────────────────────────

@pytest.mark.parametrize("spine_block", ["", "[spine]\n", "[spine]\ndir = ''\n"])
def test_config_spine_dir_optional(tmp_path, spine_block):
    cdir = tmp_path / "c"
    cdir.mkdir()
    (cdir / "x.toml").write_text(
        "resident = 'x'\nactive = false\n"
        "[episodic]\ndata_dir = '/d'\ncollection = 'c'\n"
        "[retrieval_log]\npath = '/l'\n" + spine_block,
        encoding="utf-8",
    )
    cfg = load_config("x", cdir)
    assert cfg.spine_dir is None


def test_config_spine_dir_kept_when_set(tmp_path):
    cdir = tmp_path / "c"
    cdir.mkdir()
    (cdir / "x.toml").write_text(
        "resident = 'x'\nactive = false\n"
        "[episodic]\ndata_dir = '/d'\ncollection = 'c'\n"
        "[retrieval_log]\npath = '/l'\n"
        "[spine]\ndir = '/some/spine'\n",
        encoding="utf-8",
    )
    cfg = load_config("x", cdir)
    assert cfg.spine_dir == "/some/spine"
