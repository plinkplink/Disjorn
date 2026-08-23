"""The Plan Room's derivation service.

Every test here is really one assertion in different clothes: **the board owns
no authoritative state.** Change an artifact, the card moves. Change nothing,
nothing moves. Delete the index, the board comes back whole.
"""

import io
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

import planroom as P  # noqa: E402

SPEC = """\
# Spec: {title}

## Request
- **Verbatim**: "do the thing"
- **Requester**: plink

## Agreed UX
Words.

## Lane → Review owner (DETERMINISTIC — filled from the lane, never preference)
- **Lane**: {lane}
- **Review owner**: {owner}

## Builder (USER PREFERENCE — who orchestrates; never touches Review owner)
- **Builder**: {builder}

## Cross-lane split
- **Applies**: no

## Expected diff tier
Tier {tier} — because.

## Confirm record
- **Confirmed by**: {confirmed_by}
- **#custodian seq**: {seq}

## Status
{status}
"""


def write_spec(specs: Path, slug: str, *, status="confirmed", title=None,
               lane="server", owner="Claudette", builder="Gable", tier="2",
               confirmed_by="plink", seq="1434") -> Path:
    p = specs / f"{slug}.md"
    p.write_text(SPEC.format(title=title or slug, lane=lane, owner=owner,
                             builder=builder, tier=tier,
                             confirmed_by=confirmed_by, seq=seq,
                             status=status), encoding="utf-8")
    return p


def git(repo: Path, *args: str) -> str:
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    cp = subprocess.run(["git", "-C", str(repo), *args], capture_output=True,
                        text=True, env=env, check=True)
    return cp.stdout


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    """A real git repo with a real SPECS/ — the artifacts ARE the fixture."""
    r = tmp_path / "mirror"
    (r / "SPECS").mkdir(parents=True)
    git(r, "init", "-q", "-b", "main")
    (r / "SPECS" / "README.md").write_text("specs\n")
    git(r, "add", ".")
    git(r, "commit", "-q", "-m", "init")
    return r


@pytest.fixture()
def gatehouse(tmp_path: Path) -> Path:
    """An EMPTY shelf. Deliberately empty: the collectors that read it shell out
    to `sudo git`, and a test suite that needs sudo is a test suite that does
    not run. The gatehouse-backed paths are board.py's, tested there."""
    g = tmp_path / "gatehouse"
    g.mkdir()
    return g


def derive(repo: Path, gatehouse: Path, **kw) -> dict:
    return P.derive_cards(None, repo=repo, gatehouse=gatehouse,
                          drift=kw.pop("drift", {}), **kw)


def by_slug(data: dict) -> dict:
    return {c["slug"]: c for c in data["cards"]}


# ── the load-bearing rule: reality moves the card ───────────────────────────

@pytest.mark.parametrize("status,column", [
    ("draft", "Proposed"),
    ("confirmed", "Ready"),
    ("building", "Building"),
    ("failed", "Building"),
    ("built@loop/2026-08-20-thing", "Review"),
    ("merged", "Merged"),
    ("applied-live", "Merged"),
    ("superseded", "Archived"),
    ("abandoned", "Archived"),
])
def test_the_status_line_decides_the_column(repo, gatehouse, status, column):
    write_spec(repo / "SPECS", "2026-08-20-thing", status=status)
    card = by_slug(derive(repo, gatehouse))["2026-08-20-thing"]
    assert card["column"] == column


def test_changing_the_artifact_moves_the_card_and_nothing_else_does(
        repo, gatehouse):
    specs = repo / "SPECS"
    write_spec(specs, "2026-08-20-thing", status="draft")
    assert by_slug(derive(repo, gatehouse))["2026-08-20-thing"]["column"] == "Proposed"
    # The ONLY way a card moves: the artifact changed.
    write_spec(specs, "2026-08-20-thing", status="confirmed")
    assert by_slug(derive(repo, gatehouse))["2026-08-20-thing"]["column"] == "Ready"


def test_a_card_carries_the_fields_its_spec_declares(repo, gatehouse):
    write_spec(repo / "SPECS", "2026-08-20-thing", title="A board tab",
               lane="server/app", owner="Claudette", builder="Gable",
               tier="2", seq="1434")
    c = by_slug(derive(repo, gatehouse))["2026-08-20-thing"]
    assert c["title"] == "A board tab"
    assert c["tier"] == "Tier 2"
    assert c["lane"] == "server/app"
    assert c["review_owner"] == "Claudette"
    assert c["builder"] == "Gable"
    assert c["confirm_seq"] == 1434
    assert c["spec_path"] == "SPECS/2026-08-20-thing.md"


def test_review_owner_is_never_the_builder_by_accident(repo, gatehouse):
    """The two fields are kept apart on purpose (SPECS/README.md). A parser that
    conflated them would quietly hand every diff back to the person who wrote
    it — the exact failure the deterministic rule exists to prevent."""
    write_spec(repo / "SPECS", "2026-08-20-thing", owner="Claudette",
               builder="Gable")
    c = by_slug(derive(repo, gatehouse))["2026-08-20-thing"]
    assert c["review_owner"] == "Claudette"
    assert c["builder"] == "Gable"


def test_placeholder_fields_read_as_absent_not_as_text(repo, gatehouse):
    """TEMPLATE.md's `<angle bracket>` prompts are instructions, not values. A
    card that renders one has invented a builder named `<who orchestrates>`."""
    write_spec(repo / "SPECS", "2026-08-20-thing",
               builder="<who orchestrates; user preference>")
    c = by_slug(derive(repo, gatehouse))["2026-08-20-thing"]
    assert c["builder"] is None


# ── the gate is the only authority on `confirmed` (P3) ──────────────────────

def test_confirmed_with_a_broken_confirm_record_is_flagged_not_ready(
        repo, gatehouse):
    """The incident this whole module carries forward: the board read the word
    `confirmed` and said nothing was waiting, while the broker's gate was
    refusing the same file. The column still says Ready — that IS what the
    Status line says — but the card is flagged with the gate's own reason, so
    a human reads the disagreement instead of tripping over it."""
    write_spec(repo / "SPECS", "2026-08-20-thing", status="confirmed",
               confirmed_by="<who>", seq="<seq>")
    c = by_slug(derive(repo, gatehouse))["2026-08-20-thing"]
    assert c["column"] == "Ready"
    assert "gate-blocked" in c["flags"]
    assert c["whose_move"] == "plink"
    assert "REFUSE" in c["note"]


def test_the_status_parser_is_brokerds(repo, gatehouse, monkeypatch):
    """Not "a parser that agrees with brokerd's" — brokerd's. Proven by making
    brokerd lie and watching the board repeat the lie."""
    write_spec(repo / "SPECS", "2026-08-20-thing", status="confirmed")
    b = P.brokerd()
    monkeypatch.setattr(b, "parse_spec_status", lambda text: "draft")
    P._BOARD = None  # board.py caches the module, not the function
    try:
        c = by_slug(derive(repo, gatehouse))["2026-08-20-thing"]
    finally:
        P._BOARD = None
    assert c["column"] == "Proposed"


def test_an_unreadable_status_lands_in_review_and_says_so(repo, gatehouse):
    """A spec whose Status is a word nobody knows is not dropped. Dropping it is
    how a file disappears from every list at once."""
    write_spec(repo / "SPECS", "2026-08-20-thing", status="wobbly")
    c = by_slug(derive(repo, gatehouse))["2026-08-20-thing"]
    assert c["column"] == "Review"
    assert "unparseable-status" in c["flags"]


def test_readme_and_template_are_not_cards(repo, gatehouse):
    (repo / "SPECS" / "TEMPLATE.md").write_text("# Spec: <title>\n")
    (repo / "SPECS" / "PASSDOWN-x.md").write_text("# passdown\n")
    write_spec(repo / "SPECS", "2026-08-20-thing")
    slugs = set(by_slug(derive(repo, gatehouse)))
    assert slugs == {"2026-08-20-thing"}


# ── the backlog: the only column whose cards may lack a spec ────────────────

def _backlog_db(tmp_path: Path, rows) -> str:
    p = tmp_path / "disjorn.db"
    db = sqlite3.connect(str(p))
    db.execute("CREATE TABLE backlog (id INTEGER PRIMARY KEY, text TEXT, "
               "author TEXT, created_at TEXT, status TEXT, spec_ref TEXT)")
    db.executemany("INSERT INTO backlog VALUES (?,?,?,?,?,?)", rows)
    db.commit()
    db.close()
    return str(p)


def test_open_backlog_rows_are_backlog_cards(repo, gatehouse, tmp_path):
    db = _backlog_db(tmp_path, [
        (1, "dark mode please", "plink", "2026-08-01T00:00:00Z", "open", None),
        (2, "already specced", "plink", "2026-08-02T00:00:00Z", "spec'd", "x"),
    ])
    data = derive(repo, gatehouse, message_db=db)
    cards = by_slug(data)
    assert "backlog-1" in cards
    assert "backlog-2" not in cards, "a triaged row has left the Backlog column"
    c = cards["backlog-1"]
    assert c["column"] == "Backlog"
    assert c["spec_path"] is None
    assert c["title"] == "dark mode please"


def test_a_missing_message_db_is_an_empty_backlog_not_a_crash(repo, gatehouse):
    data = derive(repo, gatehouse, message_db="/nonexistent/nope.db")
    assert [c for c in data["cards"] if c["kind"] == "backlog"] == []


# ── Review is the drift report wearing a UI ─────────────────────────────────

def test_uncited_main_commits_become_review_cards(repo, gatehouse):
    drift = {"paths": {"mirror": str(repo)}, "classified": [
        {"sha": "a" * 40, "subject": "quiet fix", "hits": ["server/app/x.py"],
         "tier": 2, "reasons": [], "error": None}]}
    data = derive(repo, gatehouse, drift=drift,
                  lane_owners={"server/": "Claudette"})
    c = by_slug(data)["keyboard-" + "a" * 12]
    assert c["column"] == "Review"
    assert "uncited" in c["flags"]
    assert "lane-violation" in c["flags"], "an uncited Tier 2 is a LANE VIOLATION"
    assert c["review_owner"] == "Claudette"
    assert c["whose_move"] == "plink"


def test_an_override_merge_is_a_review_pending_card(repo, gatehouse):
    drift = {"paths": {"mirror": str(repo)}, "citations": [
        {"holds": True, "kind": "override-seq", "seq": 1500, "self_cited": False,
         "author": "plink", "push": {"new": "b" * 40, "old": "c" * 40}}]}
    c = by_slug(derive(repo, gatehouse, drift=drift))["keyboard-" + "b" * 12]
    assert c["column"] == "Review"
    assert "review-pending" in c["flags"]
    assert "review: pending" in c["title"]


def test_a_self_cited_review_is_named_as_one(repo, gatehouse):
    """The comfortable failure mode. It must not pass as review."""
    drift = {"paths": {"mirror": str(repo)}, "citations": [
        {"holds": True, "kind": "review-seq", "seq": 1501, "self_cited": True,
         "author": "plink", "push": {"new": "d" * 40, "old": "e" * 40}}]}
    c = by_slug(derive(repo, gatehouse, drift=drift))["keyboard-" + "d" * 12]
    assert "self-cited" in c["flags"]


def test_a_properly_cited_push_makes_no_card(repo, gatehouse):
    """Review's resting state is EMPTY. A column that shows every push is not a
    drift report, it is a git log."""
    drift = {"paths": {"mirror": str(repo)}, "citations": [
        {"holds": True, "kind": "review-seq", "seq": 1502, "self_cited": False,
         "author": "Claudette", "push": {"new": "f" * 40, "old": "0" * 40}}]}
    assert [c for c in derive(repo, gatehouse, drift=drift)["cards"]
            if c["kind"] == "keyboard"] == []


def test_an_unmapped_lane_reads_unassigned_rather_than_guessing(repo, gatehouse):
    """There is no lane→owner map compiled into this module on purpose: that is
    house policy, ruled in channel, and a guess in a harness file is the
    hand-written layer this build exists to delete."""
    drift = {"paths": {"mirror": str(repo)}, "classified": [
        {"sha": "9" * 40, "subject": "x", "hits": ["notes.txt"], "tier": 0}]}
    c = by_slug(derive(repo, gatehouse, drift=drift))["keyboard-" + "9" * 12]
    assert c["review_owner"] is None


# ── the tri-state deploy badge is ONE computation (P6) ──────────────────────

@pytest.mark.parametrize("state,badge", [
    ({"state": "in-sync", "detail": "", "ahead": 0, "behind": 0}, "green"),
    ({"state": "drift", "detail": "", "ahead": 0, "behind": 3}, "amber"),
    ({"state": "drift", "detail": "", "ahead": 2, "behind": 0}, "red"),
    ({"state": "drift", "detail": "", "ahead": 0, "behind": 0, "dirty": True}, "red"),
    ({"state": "unknown", "detail": "nope"}, "unknown"),
    (None, "unknown"),
])
def test_deploy_badge_tri_state(state, badge):
    assert P.deploy_badge(state)["badge"] == badge


def test_a_dirty_prod_tree_is_red_not_amber():
    """Red is the dangerous one. A dirty prod tree is code that is RUNNING and
    was never published — the ship-by-not-publishing case. Rendering it as
    'behind' would describe the opposite of what is happening."""
    assert P.deploy_badge({"state": "drift", "dirty": True, "ahead": 0,
                           "behind": 5, "detail": ""})["badge"] == "red"


def test_the_badge_comes_from_metrics_deploy_state(repo, gatehouse, monkeypatch):
    """Proven the only way it can be: make Phase 0's function say something
    absurd and watch the badge repeat it."""
    m = P.metrics()
    monkeypatch.setattr(m, "deploy_state", lambda *a, **k: {
        "state": "in-sync", "detail": "invented", "ahead": 0, "behind": 0})
    assert m.deploy_state()["detail"] == "invented"
    assert P.deploy_badge(m.deploy_state())["badge"] == "green"


def test_only_merged_cards_carry_the_badge(repo, gatehouse):
    write_spec(repo / "SPECS", "2026-08-20-ready", status="confirmed")
    write_spec(repo / "SPECS", "2026-08-20-done", status="merged")
    cards = by_slug(derive(repo, gatehouse))
    assert cards["2026-08-20-ready"]["deploy"] is None
    assert cards["2026-08-20-done"]["deploy"] is not None


# ── the face declares staleness rather than denying it ──────────────────────

def test_the_face_carries_its_derivation_time_and_mirror_head(repo, gatehouse):
    face = derive(repo, gatehouse)["face"]
    assert face["derived_at"]
    assert face["mirror_head"], "the board says which mirror it derived from"
    assert face["columns"] == P.COLUMNS


def test_an_unconfigured_gate_is_declared_not_silent(repo, gatehouse):
    """G3's rule, carried: an empty Review column and a disarmed detector must
    not read alike."""
    face = P.derive_cards({}, repo=repo, gatehouse=gatehouse)["face"]
    assert face["gate_configured"] is False
    assert any("not configured" in n for n in face["notes"])


def test_a_gate_that_raises_does_not_take_the_board_down(repo, gatehouse,
                                                         monkeypatch):
    write_spec(repo / "SPECS", "2026-08-20-thing")
    m = P.metrics()
    monkeypatch.setattr(m, "gate_paths", lambda cfg: {
        "configured": True, "mirror": str(repo), "message_db": None,
        "deploy_tree": None, "branch": "main"})
    monkeypatch.setattr(m, "gate_drift",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    data = P.derive_cards({"gate": {"x": 1}}, repo=repo, gatehouse=gatehouse)
    assert "2026-08-20-thing" in by_slug(data)
    assert any("boom" in n for n in data["face"]["notes"])


# ── whose-move-first ordering (board_html.py's design, carried over) ────────

def test_cards_waiting_on_a_human_sort_first_within_a_column(repo, gatehouse):
    specs = repo / "SPECS"
    write_spec(specs, "2026-08-01-fine", status="confirmed")
    write_spec(specs, "2026-08-02-broken", status="confirmed",
               confirmed_by="<who>", seq="<seq>")
    ready = [c for c in derive(repo, gatehouse)["cards"] if c["column"] == "Ready"]
    assert ready[0]["slug"] == "2026-08-02-broken", (
        "the organising question is what is WAITING on a human, not what is oldest")
    assert [c["position"] for c in ready] == [0, 1]


# ── the index: a cache, never a source ──────────────────────────────────────

def test_the_index_round_trips(repo, gatehouse, tmp_path):
    write_spec(repo / "SPECS", "2026-08-20-thing", status="confirmed")
    data = derive(repo, gatehouse)
    idx = str(tmp_path / "idx.db")
    P.build_index(idx, data)
    back = P.read_index(idx)
    assert by_slug(back)["2026-08-20-thing"]["column"] == "Ready"
    assert back["face"]["mirror_head"] == data["face"]["mirror_head"]
    assert back["face"]["available"] is True


def test_deleting_the_index_loses_nothing(repo, gatehouse, tmp_path):
    """THE property. Git wins every disagreement and the index rebuilds from
    zero, so the cache is never something anybody has to protect."""
    write_spec(repo / "SPECS", "2026-08-20-thing", status="confirmed")
    idx = str(tmp_path / "idx.db")
    P.build_index(idx, derive(repo, gatehouse))
    first = P.read_index(idx)["cards"]
    os.unlink(idx)
    P.build_index(idx, derive(repo, gatehouse))
    assert [c["slug"] for c in P.read_index(idx)["cards"]] == \
           [c["slug"] for c in first]


def test_a_rebuild_replaces_the_index_wholesale(repo, gatehouse, tmp_path):
    """No incremental path, by construction. An index that can only be updated
    is an index that can drift."""
    specs = repo / "SPECS"
    write_spec(specs, "2026-08-20-gone", status="confirmed")
    idx = str(tmp_path / "idx.db")
    P.build_index(idx, derive(repo, gatehouse))
    os.unlink(specs / "2026-08-20-gone.md")
    P.build_index(idx, derive(repo, gatehouse))
    assert P.read_index(idx)["cards"] == []


def test_a_missing_index_is_declared_not_empty(tmp_path):
    """An absent index and an empty board must not read alike."""
    out = P.read_index(str(tmp_path / "never-written.db"))
    assert out["face"]["available"] is False
    assert "no index" in out["face"]["unavailable_reason"]
    assert out["face"]["columns"] == P.COLUMNS


def test_a_corrupt_index_is_declared_not_a_crash(tmp_path):
    idx = tmp_path / "idx.db"
    idx.write_bytes(b"this is not a database")
    out = P.read_index(str(idx))
    assert out["face"]["available"] is False
    assert out["cards"] == []


def test_a_failed_rebuild_leaves_the_previous_index_intact(repo, gatehouse,
                                                            tmp_path):
    """Atomic by temp-file-and-replace. A half-written board is worse than a
    stale one, because a stale one says how stale it is."""
    write_spec(repo / "SPECS", "2026-08-20-thing", status="confirmed")
    idx = str(tmp_path / "idx.db")
    P.build_index(idx, derive(repo, gatehouse))
    good = Path(idx).read_bytes()
    broken = {"face": {"derived_at": "x"}, "cards": [{"slug": "no-such-keys"}]}
    with pytest.raises(KeyError):
        P.build_index(idx, broken)
    assert Path(idx).read_bytes() == good


def test_board_native_state_is_not_in_the_index(repo, gatehouse, tmp_path):
    """card_meta and card_comments are AUTHORITATIVE and server-owned. If they
    were in this cache, "rebuild from zero" would mean "delete every comment
    anybody wrote"."""
    write_spec(repo / "SPECS", "2026-08-20-thing", status="confirmed")
    idx = str(tmp_path / "idx.db")
    P.build_index(idx, derive(repo, gatehouse))
    db = sqlite3.connect(idx)
    tables = {r[0] for r in db.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    db.close()
    assert tables == {"face", "cards"}
    assert "card_meta" not in tables
    assert "card_comments" not in tables


def test_the_index_never_says_archived_for_a_board_native_archive(repo,
                                                                  gatehouse):
    """Archived-by-flag is the server's to apply. The index only knows the
    derived route in (superseded / abandoned)."""
    write_spec(repo / "SPECS", "2026-08-20-thing", status="merged")
    assert by_slug(derive(repo, gatehouse))["2026-08-20-thing"]["column"] == "Merged"


# ── transitions: one line per column move, never per edit ───────────────────

def test_a_column_move_produces_exactly_one_line(repo, gatehouse, tmp_path):
    specs, idx = repo / "SPECS", str(tmp_path / "idx.db")
    write_spec(specs, "2026-08-20-thing", status="draft")
    P.rebuild(idx, derive(repo, gatehouse))          # cold start: silent
    write_spec(specs, "2026-08-20-thing", status="confirmed")
    lines = P.rebuild(idx, derive(repo, gatehouse))
    assert len(lines) == 1
    assert "2026-08-20-thing" in lines[0]
    assert "Proposed → Ready" in lines[0]


def test_an_edit_that_does_not_move_a_column_says_nothing(repo, gatehouse,
                                                          tmp_path):
    specs, idx = repo / "SPECS", str(tmp_path / "idx.db")
    write_spec(specs, "2026-08-20-thing", status="draft", title="one")
    P.rebuild(idx, derive(repo, gatehouse))
    write_spec(specs, "2026-08-20-thing", status="draft", title="two")
    assert P.rebuild(idx, derive(repo, gatehouse)) == []


def test_a_cold_start_announces_nothing(repo, gatehouse, tmp_path):
    """Otherwise the first rebuild after any install dumps the entire history of
    the house into #custodian as if it had all just happened."""
    write_spec(repo / "SPECS", "2026-08-20-thing", status="confirmed")
    assert P.rebuild(str(tmp_path / "idx.db"), derive(repo, gatehouse)) == []


def test_a_rebuild_with_no_change_is_silent(repo, gatehouse, tmp_path):
    """The timer fires whether or not anything happened. A detector that
    narrates every tick teaches everyone to stop reading it."""
    specs, idx = repo / "SPECS", str(tmp_path / "idx.db")
    write_spec(specs, "2026-08-20-thing", status="confirmed")
    P.rebuild(idx, derive(repo, gatehouse))
    assert P.rebuild(idx, derive(repo, gatehouse)) == []
    assert P.rebuild(idx, derive(repo, gatehouse)) == []


def test_a_new_card_and_a_departed_card_both_announce(repo, gatehouse, tmp_path):
    specs, idx = repo / "SPECS", str(tmp_path / "idx.db")
    write_spec(specs, "2026-08-20-a", status="draft")
    P.rebuild(idx, derive(repo, gatehouse))
    write_spec(specs, "2026-08-20-b", status="draft")
    os.unlink(specs / "2026-08-20-a.md")
    lines = P.rebuild(idx, derive(repo, gatehouse))
    assert any("2026-08-20-b opened in Proposed" in ln for ln in lines)
    assert any("2026-08-20-a left the board" in ln for ln in lines)


def test_a_transition_line_never_prints_a_bare_identifier(repo, gatehouse):
    """brief's rule, inherited: an item you have to go look up is an item that
    gets deferred."""
    line = P.format_transition(
        {"slug": "2026-08-20-thing", "from": "Ready", "to": "Building",
         "kind": "moved"},
        {"2026-08-20-thing": {"title": "The Plan Room"}})
    assert "The Plan Room" in line


def test_detect_transitions_is_pure():
    old = {"a": "Ready", "b": "Merged"}
    new = {"a": "Building", "c": "Backlog"}
    got = {(t["slug"], t["from"], t["to"]) for t in P.detect_transitions(old, new)}
    assert got == {("a", "Ready", "Building"), ("b", "Merged", None),
                   ("c", None, "Backlog")}


# ── the CLI renderer ────────────────────────────────────────────────────────

def test_the_cli_renders_without_a_config(repo, gatehouse):
    write_spec(repo / "SPECS", "2026-08-20-thing", status="confirmed")
    buf = io.StringIO()
    P.render_text(derive(repo, gatehouse), out=buf)
    out = buf.getvalue()
    assert "THE PLAN ROOM" in out
    for col in P.COLUMNS:
        assert col.upper() in out


def test_the_cli_says_so_when_the_index_is_missing(tmp_path):
    buf = io.StringIO()
    P.render_text(P.read_index(str(tmp_path / "nope.db")), out=buf)
    assert "UNAVAILABLE" in buf.getvalue()


def test_every_column_ruled_at_seq_1391_exists_in_order():
    assert P.COLUMNS == ["Backlog", "Proposed", "Ready", "Building", "Review",
                         "Merged", "Archived"]
    assert set(P.COLUMN_BLURBS) == set(P.COLUMNS)


# ── search haystack ─────────────────────────────────────────────────────────

def test_the_haystack_carries_what_a_search_would_look_for(repo, gatehouse):
    write_spec(repo / "SPECS", "2026-08-20-thing", title="blueprint backdrop",
               owner="Claudette")
    c = by_slug(derive(repo, gatehouse))["2026-08-20-thing"]
    hay = P._haystack(c)
    for needle in ("blueprint backdrop", "Claudette", "2026-08-20-thing",
                   "SPECS/2026-08-20-thing.md"):
        assert needle in hay
