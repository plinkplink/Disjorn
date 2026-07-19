"""Migration tooling on two synthetic stores: migrate report, parallel_diff
containment pass, and containment failure when the new store lost an entry."""

import pytest

from house_memory import MemoryStore, RetrievalLog, migrate, parallel_diff
from house_memory.migration import _open_old_collection
from house_memory_testlib import make_memory


@pytest.fixture
def old_store(tmp_path, embedder):
    """Synthetic 'old' store with a supersession chain and a retrieval log."""
    store = MemoryStore(
        tmp_path / "old",
        "old_memory",
        embedder,
        retrieval_log=RetrievalLog(tmp_path / "old_retrieval.jsonl", resident="claudette"),
    )
    store.remember(make_memory("plink prefers debian for the NAS", subject="plink"))
    store.remember(make_memory("gable arrived at the house in july", subject="gable"))
    old = store.remember(make_memory("the server runs on port 9000", subject="server"))[0]
    store.forget(old.id, supersede_with=make_memory("the server runs on port 9100", subject="server"))
    # build a replayable log the way it happens in production: via recall
    store.recall("what port does the server run on")
    store.recall("who is gable", subject="gable")
    store.recall("NAS operating system", subject="plink")
    return store


def test_migrate_copies_everything_without_reembedding(tmp_path, old_store):
    class Boom:
        def embed_document(self, text):
            raise AssertionError("migration must not re-embed")

        def embed_query(self, text):
            raise AssertionError("migration must not embed queries")

    new_store = MemoryStore(tmp_path / "new", "new_memory", Boom())
    report = migrate(tmp_path / "old", new_store)  # collection auto-detected
    assert report.old_collection == "old_memory"
    assert report.total_old_records == 4  # includes the superseded record
    assert report.imported == 4
    assert report.new_store_count == 4
    assert report.complete is True


def test_migrate_requires_explicit_collection_when_ambiguous(tmp_path, old_store, embedder):
    # add a second collection to the old dir
    second = MemoryStore(tmp_path / "old", "other", embedder)
    second.remember(make_memory("stray"))
    new_store = MemoryStore(tmp_path / "new", "new_memory", embedder)
    with pytest.raises(ValueError, match="pass old_collection explicitly"):
        migrate(tmp_path / "old", new_store)
    report = migrate(tmp_path / "old", new_store, old_collection="old_memory")
    assert report.complete is True


def test_open_old_collection_never_creates(tmp_path, old_store):
    with pytest.raises(Exception):
        _open_old_collection(tmp_path / "old", "does_not_exist")


def test_parallel_diff_containment_ok(tmp_path, old_store, embedder):
    new_store = MemoryStore(tmp_path / "new", "new_memory", embedder)
    migrate(tmp_path / "old", new_store)
    report = parallel_diff(
        tmp_path / "old", new_store, tmp_path / "old_retrieval.jsonl"
    )
    assert report.total_queries == 3
    assert report.ok is True
    assert report.failed_queries == 0
    assert report.contained_queries == 3
    for d in report.details:
        assert d.contained
        assert d.missing_from_new == []
        assert set(d.old_returned_ids) <= set(d.new_returned_ids)
    # replayed queries carry the logged subject filters through
    assert {d.subject_filter for d in report.details} == {None, "gable", "plink"}


def test_parallel_diff_reports_missing_entry(tmp_path, old_store, embedder):
    new_store = MemoryStore(tmp_path / "new", "new_memory", embedder)
    migrate(tmp_path / "old", new_store)
    # simulate a migration that ate a memory: drop the gable entry from NEW
    victim = new_store.recall("who is gable", subject="gable")[0]
    new_store.forget(victim.id)

    report = parallel_diff(tmp_path / "old", new_store, tmp_path / "old_retrieval.jsonl")
    assert report.ok is False
    # every replayed query whose OLD results included the victim now fails
    failed = {d.query: d for d in report.details if not d.contained}
    assert set(failed) == {"what port does the server run on", "who is gable"}
    assert all(d.missing_from_new == [victim.id] for d in failed.values())
    assert report.failed_queries == 2
    assert report.contained_queries == 1


def test_parallel_diff_accepts_record_list(tmp_path, old_store, embedder):
    new_store = MemoryStore(tmp_path / "new", "new_memory", embedder)
    migrate(tmp_path / "old", new_store)
    records = RetrievalLog(tmp_path / "old_retrieval.jsonl", resident="claudette").read()
    report = parallel_diff(tmp_path / "old", new_store, records[:1])
    assert report.total_queries == 1
    assert report.ok is True


def test_parallel_diff_never_writes_new_retrieval_log(tmp_path, old_store, embedder):
    new_log_path = tmp_path / "new_retrieval.jsonl"
    new_store = MemoryStore(
        tmp_path / "new",
        "new_memory",
        embedder,
        retrieval_log=RetrievalLog(new_log_path, resident="claudette"),
    )
    migrate(tmp_path / "old", new_store)
    parallel_diff(tmp_path / "old", new_store, tmp_path / "old_retrieval.jsonl")
    assert not new_log_path.exists()  # replays bypass recall(), log untouched
