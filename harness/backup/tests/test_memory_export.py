"""Claudette's export: lossless round-trip, superseded records included, and every tamper caught."""

import json

import pytest

import memory_export as mx
from house_memory import Memory, MemoryStore, StubEmbedder


def _mem(content, **kw):
    return Memory(content=content, subject="plink", source_author="plink",
                  author_of_memory="claudette", **kw)


@pytest.fixture
def live(tmp_path):
    store = MemoryStore(tmp_path / "live", mx.COLLECTION, StubEmbedder(dim=64))
    a = store.remember(_mem("plink lives in oslo", tags=["location"], salience=5))[0]
    store.remember(_mem("gable keeps his memory in files", confidence="rumor"))
    store.forget(a.id, supersede_with=_mem("plink lives in bergen"))
    return tmp_path / "live"


def test_export_is_sorted_and_carries_superseded(live, tmp_path):
    out = tmp_path / "export.json"
    facts = mx.export(live, out)
    records = json.loads(out.read_bytes())
    assert facts == {"count": 3, "export_sha256": mx.sha256(out), "store_count": 3,
                     "ids_sha256": mx.ids_sha256(r["id"] for r in records)}
    assert [r["id"] for r in records] == sorted(r["id"] for r in records)
    assert sum(1 for r in records if r["metadata"].get("superseded_by")) == 1
    assert all(r["embedding"] for r in records)


def test_verify_round_trip_passes(live, tmp_path):
    out = tmp_path / "export.json"
    facts = mx.export(live, out)
    assert mx.verify(out, facts["count"], facts["export_sha256"], tmp_path / "scratch") == []


def test_verify_catches_count_and_sha_mismatch(live, tmp_path):
    out = tmp_path / "export.json"
    facts = mx.export(live, out)
    fails = mx.verify(out, facts["count"] + 1, "0" * 64, tmp_path / "scratch")
    assert any("sha" in f for f in fails)
    assert any("manifest says 4" in f for f in fails)


def test_verify_catches_a_dropped_embedding(live, tmp_path):
    out = tmp_path / "export.json"
    facts = mx.export(live, out)
    records = json.loads(out.read_bytes())
    records[0]["embedding"] = None
    out.write_bytes(mx.dumps(records))
    rc = mx.main(["verify", "--export", str(out), "--count", str(facts["count"]),
                  "--sha", mx.sha256(out), "--scratch", str(tmp_path / "scratch")])
    assert rc == 1


def test_export_refuses_a_missing_or_empty_store(tmp_path):
    with pytest.raises(SystemExit, match="no chroma.sqlite3"):
        mx.export(tmp_path / "nowhere", tmp_path / "x.json")
    assert not (tmp_path / "nowhere").exists()
    MemoryStore(tmp_path / "empty", mx.COLLECTION, StubEmbedder(dim=64))
    with pytest.raises(SystemExit, match="zero records"):
        mx.export(tmp_path / "empty", tmp_path / "x.json")
