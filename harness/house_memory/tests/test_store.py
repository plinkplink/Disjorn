"""MemoryStore: roundtrip, supersede, subject filtering, export/import,
instance isolation. Tmp dirs + StubEmbedder only."""

from house_memory import MemoryStore, RetrievalLog, StubEmbedder
from house_memory_testlib import make_memory


def test_remember_recall_roundtrip(store):
    mem, first_seen = store.remember(make_memory("plink builds a NAS on debian"))
    assert first_seen is True
    out = store.recall("debian NAS build")
    assert [m.id for m in out][0] == mem.id
    got = out[0]
    assert got.content == mem.content
    assert got.subject == "plink"
    assert got.author_of_memory == "testbot"


def test_first_seen_subject_flag(store):
    _, first = store.remember(make_memory("one", subject="alice"))
    assert first is True
    _, second = store.remember(make_memory("two", subject="Alice"))  # normalizes to same
    assert second is False


def test_first_seen_warmup_across_reopen(tmp_path, embedder):
    data_dir = tmp_path / "chroma"
    s1 = MemoryStore(data_dir, "mem", embedder)
    s1.remember(make_memory("hello", subject="alice"))
    s2 = MemoryStore(data_dir, "mem", embedder)  # fresh instance, same dir
    _, first = s2.remember(make_memory("hello again", subject="alice"))
    assert first is False


def test_subject_filter_and_normalization(store):
    a = store.remember(make_memory("likes green tea", subject="alice"))[0]
    store.remember(make_memory("likes green tea too", subject="bob"))
    out = store.recall("green tea", subject=" @Alice ")  # normalized on read
    assert [m.id for m in out] == [a.id]
    assert all(m.subject == "alice" for m in out)


def test_forget_hard_delete(store):
    mem = store.remember(make_memory("temporary fact"))[0]
    assert store.forget(mem.id) is True
    assert store.recall("temporary fact") == []


def test_forget_with_supersede(store):
    old = store.remember(make_memory("plink lives in oslo"))[0]
    new = make_memory("plink lives in bergen now")
    assert store.forget(old.id, supersede_with=new) is True
    out = store.recall("where does plink live", limit=10)
    ids = [m.id for m in out]
    assert new.id in ids
    assert old.id not in ids  # superseded filtered out of recall


def test_supersede_missing_id_returns_false(store):
    assert store.forget("no-such-id", supersede_with=make_memory("x")) is False


def test_recall_logs_retrieval(tmp_path, embedder):
    log = RetrievalLog(tmp_path / "logs" / "retrieval.jsonl", resident="testbot")
    store = MemoryStore(tmp_path / "chroma", "mem", embedder, retrieval_log=log)
    old = store.remember(make_memory("plink lives in oslo"))[0]
    store.forget(old.id, supersede_with=make_memory("plink lives in bergen"))
    store.recall("where does plink live", subject="Plink", limit=10)
    records = log.read()
    assert len(records) == 1
    rec = records[0]
    assert rec.resident == "testbot"
    assert rec.subject_filter == "plink"  # normalized
    assert old.id in rec.raw_ids  # raw includes superseded
    assert old.id not in rec.returned_ids  # returned does not
    assert len(rec.distances) == len(rec.raw_ids)


def test_export_import_fidelity(tmp_path, embedder):
    src = MemoryStore(tmp_path / "src", "mem", embedder)
    a = src.remember(make_memory("alpha fact", subject="alice", tags=["Tea Time"]))[0]
    b = src.remember(make_memory("beta fact", subject="bob", confidence="rumor"))[0]
    src.forget(a.id, supersede_with=make_memory("alpha fact v2", subject="alice"))

    exported = src.export_all()
    assert len(exported) == 3  # superseded record travels too
    assert all(r["embedding"] is not None for r in exported)

    dst = MemoryStore(tmp_path / "dst", "mem", embedder)
    assert dst.import_all(exported) == 3
    assert dst.count() == 3
    # byte-level fidelity: re-export matches, including embeddings + metadata
    assert dst.export_all() == exported
    # semantics survive: superseded stays hidden, subjects warm
    ids = [m.id for m in dst.recall("alpha fact", limit=10)]
    assert a.id not in ids
    _, first = dst.remember(make_memory("more bob", subject="bob"))
    assert first is False
    assert b.id in [m.id for m in dst.recall("beta fact", subject="bob", limit=10)]


def test_import_all_empty(store):
    assert store.import_all([]) == 0


def test_instances_are_isolated(tmp_path, embedder):
    s1 = MemoryStore(tmp_path / "a", "mem", embedder)
    s2 = MemoryStore(tmp_path / "b", "mem", embedder)
    s1.remember(make_memory("only in a"))
    assert s2.recall("only in a") == []
    assert s2.count() == 0
