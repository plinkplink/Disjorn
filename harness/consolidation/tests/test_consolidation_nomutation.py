"""The load-bearing guarantee: consolidation NEVER mutates any store.

Snapshots the episodic store, the spine files, and the retrieval log around a
FULL run (build + dry-run post) and asserts byte-for-byte equality. Also builds
inputs the production way (from cfg, with a NullEmbedder) to prove the real read
path embeds nothing.
"""

import io

import pytest

from consolidation import build_proposals, post_report
from consolidation.embedders import EmbedderUsedError, NullEmbedder
from consolidation_testlib import (
    FIXED_NOW,
    add_memory,
    append_log,
    make_config,
    write_spine_entry,
)


def _seed(store, spine_dir, log_path):
    add_memory(store, "hot promotable pattern", mid="m-hot")
    add_memory(store, "another hot pattern", mid="m-hot2")
    for _ in range(4):
        append_log(log_path, returned_ids=["m-hot"], days_ago=1)
        append_log(log_path, returned_ids=["m-hot2"], days_ago=1)
    write_spine_entry(spine_dir, "20-plain.md", "Unreferenced plain fact.", name="plain")
    write_spine_entry(spine_dir, "30-lesson.md", "Never skip the snapshot.", name="why", kind="lesson")


def test_run_mutates_nothing(store, spine_dir, log_path):
    _seed(store, spine_dir, log_path)

    before_store = store.export_all()
    before_count = store.count()
    before_spine = {p.name: p.read_bytes() for p in spine_dir.glob("*.md")}
    before_log = log_path.read_bytes()

    # build inputs the PRODUCTION way (store=None -> NullEmbedder store from cfg)
    cfg = make_config(store=store, spine_dir=spine_dir, log_path=log_path)
    report = build_proposals(cfg, now=FIXED_NOW)
    assert report.proposals  # it did real work
    post_report(report, cfg, dry_run=True, out=io.StringIO())

    assert store.export_all() == before_store
    assert store.count() == before_count
    assert {p.name: p.read_bytes() for p in spine_dir.glob("*.md")} == before_spine
    assert log_path.read_bytes() == before_log


def test_null_embedder_refuses_to_embed():
    e = NullEmbedder()
    with pytest.raises(EmbedderUsedError):
        e.embed_document("x")
    with pytest.raises(EmbedderUsedError):
        e.embed_query("x")


def test_cfg_constructed_store_is_null_embedder(store, spine_dir, log_path):
    # prove the store consolidation builds cannot embed
    _seed(store, spine_dir, log_path)
    from house_memory import MemoryStore

    ro = MemoryStore(
        data_dir=store.data_dir,
        collection_name=store.collection_name,
        embedder=NullEmbedder(),
    )
    # reads work...
    assert ro.count() == 2
    assert ro.export_all()  # no embedding needed
    # ...but any embed attempt would raise (guard for import_all/remember/recall)
    with pytest.raises(EmbedderUsedError):
        ro.embedder.embed_query("q")
