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


# ==========================================================================
# Memory v2 phase 1 — the annotation strip (spec item 2, "the meter goes
# invisible"). The surfaced-memories block ships bodies only.
# ==========================================================================

def test_to_display_annotated_keeps_the_date():
    from house_memory import Memory
    m = Memory(subject="plink", content="likes RAID1", tags=["hardware"],
                 source_author="plink")
    out = m.to_display()
    assert m.created_at[:10] in out
    assert "likes RAID1" in out


def test_to_display_stripped_drops_the_date():
    """The creation date is the one annotation in this line a reader can turn
    into a ranking, so it goes."""
    from house_memory import Memory
    m = Memory(subject="plink", content="likes RAID1", tags=["hardware"],
                 source_author="plink")
    out = m.to_display(annotated=False)
    assert m.created_at[:10] not in out
    assert "[" not in out.split("(tags")[0]


def test_strip_keeps_body_tags_and_the_rumor_warning():
    """What survives is deliberate: none of it is a meter, and hiding the
    `(unconfirmed)` marker would make a rumor read as fact."""
    from house_memory import Memory
    m = Memory(subject="plink", content="maybe likes RAID5",
               tags=["hardware"], confidence="rumor", source_author="plink")
    out = m.to_display(annotated=False)
    assert "maybe likes RAID5" in out
    assert "about plink" in out
    assert "tags: hardware" in out
    assert "(unconfirmed)" in out


# ==========================================================================
# 2026-08-04 — the tag shredder and the silent cut.
#
# Both found by Claudette from inside her own surfaced block, which is a
# novel place to file a bug from. The shredder had already cost 75 of her
# 164 memories their tags before anyone looked.
# ==========================================================================

def test_bare_string_tag_is_not_shredded():
    """`for t in "agenthood"` iterates letters. De-dupe collapses repeats,
    the cap keeps six, and you get ["a","g","e","n","t","h"] — silent,
    plausible-looking, irreversible."""
    from house_memory import normalize_tags
    assert normalize_tags("agenthood") == ["agenthood"]


def test_bare_string_tag_survives_through_memory():
    from house_memory import Memory
    m = Memory(content="x", subject="plink", source_author="plink",
               tags="agenthood")
    assert m.tags == ["agenthood"]


def test_none_tags_is_empty_not_a_crash():
    from house_memory import normalize_tags
    assert normalize_tags(None) == []


def test_normal_tag_lists_are_unaffected():
    from house_memory import normalize_tags
    assert normalize_tags(["Agent Hood", "agent hood", "x!"]) == ["agent-hood", "x"]


def test_truncation_is_always_marked():
    """Her rule: mark the cut or don't cut. A silent truncation teaches her a
    wrong version of her own past."""
    from house_memory import Memory
    from house_memory.schema import CONTENT_HARD_CAP, TRUNCATION_MARK
    m = Memory(content="word " * 500, subject="plink", source_author="plink")
    assert len(m.content) <= CONTENT_HARD_CAP
    assert m.content.endswith(TRUNCATION_MARK)


def test_pre_sliced_content_is_still_marked():
    """THE regression. `content=body[:CONTENT_HARD_CAP]` made the old
    `len > cap` check false, so the cut was never marked — which is how her
    v2 design entry ended at 'Budgets: 3 revisi' with no sign of it."""
    from house_memory.schema import clip_content, CONTENT_HARD_CAP, TRUNCATION_MARK
    body = "word " * 500
    pre_sliced = body[:CONTENT_HARD_CAP]          # exactly at the cap
    assert len(pre_sliced) == CONTENT_HARD_CAP
    # Feeding the ALREADY-cut body through the one clipper still marks it,
    # because a body at the cap is indistinguishable from one cut to it.
    out = clip_content(pre_sliced, cap=CONTENT_HARD_CAP - 1)
    assert out.endswith(TRUNCATION_MARK)


def test_truncation_does_not_cut_mid_word():
    from house_memory.schema import clip_content, TRUNCATION_MARK
    out = clip_content("alpha bravo charlie delta echo foxtrot", cap=30)
    body = out[: -len(TRUNCATION_MARK)]
    assert not body.endswith(" ")
    assert body.split()[-1] in {"alpha", "bravo", "charlie", "delta", "echo"}


def test_short_content_is_untouched():
    from house_memory.schema import clip_content, TRUNCATION_MARK
    assert clip_content("short") == "short"
    assert TRUNCATION_MARK not in clip_content("short")


def test_unbroken_token_still_gets_cut_and_marked():
    """No word boundary to find — take the hard cut, but still mark it."""
    from house_memory.schema import clip_content, TRUNCATION_MARK
    out = clip_content("x" * 900, cap=100)
    assert len(out) <= 100
    assert out.endswith(TRUNCATION_MARK)


# --- amend_metadata: the repair verb (Claudette, #custodian seq 628) --------
#
# The whole point is the LINE: metadata moves, the body never does. Most of
# these tests exist to keep a future edit from quietly widening the verb.


def test_amend_retags_without_touching_body_or_id(store):
    mem, _ = store.remember(make_memory("the kernel entry", tags=["m", "e", "o"]))
    changed = store.amend_metadata(mem.id, tags=["memory-design"])
    assert changed["tags"] == {"from": ["m", "e", "o"], "to": ["memory-design"]}
    got = store._collection.get(ids=[mem.id])
    assert got["ids"] == [mem.id], "id must survive a retag"
    assert got["documents"][0] == mem.content, "the body must not move"


def test_amend_preserves_salience_and_confidence_it_was_not_asked_to_change(store):
    """The exact loss supersede would have caused: her salience 4/5 and
    `confirmed` coming back as defaults after a label repair."""
    mem, _ = store.remember(make_memory("load-bearing", salience=5, confidence="rumor"))
    store.amend_metadata(mem.id, tags=["memory-hygiene"])
    meta = store._collection.get(ids=[mem.id])["metadatas"][0]
    assert meta["salience"] == 5
    assert meta["confidence"] == "rumor"


def test_amend_refuses_a_bare_string_instead_of_iterating_it(store):
    """Her condition, seq 628. normalize_tags COERCES on the write path, which
    is right there; a repair verb must refuse, because a bare string means the
    caller is confused and a quiet one-tag save writes a second wrong answer
    over the first."""
    mem, _ = store.remember(make_memory("x"))
    import pytest

    with pytest.raises(TypeError):
        store.amend_metadata(mem.id, tags="agenthood")
    meta = store._collection.get(ids=[mem.id])["metadatas"][0]
    assert "agenthood" not in meta["tags_json"], "a refused call must not write"


def test_amend_cannot_reach_content(store):
    """Structural, not behavioural: there is no content parameter, so the
    verb cannot express a body edit even by mistake."""
    import inspect

    params = inspect.signature(store.amend_metadata).parameters
    assert "content" not in params
    assert set(params) == {"memory_id", "tags", "salience", "confidence"}


def test_amend_unknown_id_returns_none(store):
    assert store.amend_metadata("no-such-id", tags=["a"]) is None


def test_amend_noop_returns_empty_dict(store):
    mem, _ = store.remember(make_memory("x", tags=["alpha"]))
    assert store.amend_metadata(mem.id) == {}
    assert store.amend_metadata(mem.id, tags=["alpha"]) == {}


def test_amend_validates_salience_and_confidence(store):
    import pytest

    mem, _ = store.remember(make_memory("x"))
    with pytest.raises(ValueError):
        store.amend_metadata(mem.id, salience=9)
    with pytest.raises(ValueError):
        store.amend_metadata(mem.id, confidence="maybe")


def test_amend_normalizes_tags_it_accepts(store):
    mem, _ = store.remember(make_memory("x"))
    changed = store.amend_metadata(mem.id, tags=["Memory Design", "MEMORY-DESIGN", "!!"])
    assert changed["tags"]["to"] == ["memory-design"]


def test_amended_memory_still_recalls_with_its_original_vector(store):
    """No re-embedding: the memory stays findable by the same query it was
    findable by before the repair."""
    mem, _ = store.remember(make_memory("plink builds a NAS on debian"))
    before = [m.id for m in store.recall("debian NAS build")]
    store.amend_metadata(mem.id, tags=["nas"])
    after = [m.id for m in store.recall("debian NAS build")]
    assert before == after
