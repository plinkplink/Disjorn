"""The Plan Room's five broker verbs, and the three rebuild triggers.

The claim under test, stated once: **the two write verbs are structurally
unable to touch derived state.** Not "are checked" — unable. Derived state has
no write path anywhere in this house, so the strongest test available is to
record every call a write verb makes and assert it only ever reaches a
board-native endpoint. That is what `harness.planroom_calls` is for.
"""

import json

import pytest

from broker_testlib import *  # noqa: F401,F403 — the `harness` fixture
import brokerd


READ = ["board-list", "board-card", "board-search"]
WRITE = ["board-flag", "board-comment"]
ALL_BOARD = READ + WRITE

CARD = {
    "slug": "2026-08-20-plan-room", "column": "Ready",
    "title": "The Plan Room", "tier": "Tier 2", "review_owner": "Claudette",
    "builder": "Gable", "confirm_seq": 1434, "flags": [], "kind": "spec",
    "blocked": False, "blocked_reason": None, "comment_count": 0,
    "spec_path": "SPECS/2026-08-20-plan-room.md",
}


@pytest.fixture()
def board(harness):
    """The harness with one card on its fake board."""
    harness.planroom_state["cards"] = [dict(CARD)]
    harness.set_verbs(**{v: True for v in ALL_BOARD})
    return harness


# ── the shape of the surface ────────────────────────────────────────────────

def test_all_five_verbs_exist(harness):
    for verb in ALL_BOARD:
        assert verb in harness.broker.verbs


def test_every_board_verb_is_off_by_default(harness):
    harness.set_verbs()  # everything explicitly OFF
    for verb in ALL_BOARD:
        resp = harness.call(verb, {"slug": "2026-08-20-x", "text": "x",
                                   "action": "unblock"})
        assert resp["ok"] is False
        assert resp["error"]["code"] == "verb-disabled", verb
    assert harness.planroom_calls == [], "a disabled verb reached the network"


@pytest.mark.parametrize("verb", ALL_BOARD)
def test_unknown_args_are_refused(board, verb):
    resp = board.call(verb, {"slug": "2026-08-20-plan-room", "text": "x",
                             "action": "unblock", "wat": 1})
    assert resp["ok"] is False
    assert resp["error"]["code"] == "bad-args"
    assert "wat" in resp["error"]["message"]


@pytest.mark.parametrize("verb", ALL_BOARD)
def test_every_call_writes_exactly_one_audit_line(board, verb):
    before = len(board.audit_lines())
    board.call(verb, {"slug": "2026-08-20-plan-room", "text": "hi",
                      "action": "unblock"})
    assert len(board.audit_lines()) == before + 1


# ── the wall: writes touch board-native state and nothing else ──────────────

def test_a_write_verb_only_ever_reaches_a_board_native_endpoint(board):
    """THE test. Every path a write verb hits, recorded and asserted."""
    board.call("board-flag", {"slug": "2026-08-20-plan-room",
                              "action": "blocked", "reason": "waiting on plink"})
    board.call("board-comment", {"slug": "2026-08-20-plan-room", "text": "read it"})
    writes = [c for c in board.planroom_calls if c["method"] != "GET"]
    assert {c["path"] for c in writes} == {
        "/planroom/cards/2026-08-20-plan-room/flag",
        "/planroom/cards/2026-08-20-plan-room/comment",
    }
    for call in writes:
        # Not one of these payloads can name a column, a status, or a seq.
        assert set(call["payload"]) <= {"blocked", "reason", "text", "author"}


def test_no_board_verb_can_move_a_card(board):
    """There is no argument, on any of the five, that names a column. A card
    changes columns only because reality moved."""
    for verb in ALL_BOARD:
        resp = board.call(verb, {"slug": "2026-08-20-plan-room",
                                 "column": "Merged", "text": "x",
                                 "action": "unblock"})
        assert resp["ok"] is False, verb
        assert resp["error"]["code"] == "bad-args"


def test_a_read_verb_never_writes(board):
    for verb, args in (("board-list", {}),
                       ("board-card", {"slug": "2026-08-20-plan-room"}),
                       ("board-search", {"text": "plan"})):
        board.planroom_calls.clear()
        board.call(verb, args)
        assert all(c["method"] == "GET" for c in board.planroom_calls), verb


# ── identity is the broker's, never the caller's ────────────────────────────

def test_a_comment_is_attributed_by_the_broker_not_by_the_caller(board):
    """Same rule file-proposal has always run under: the resident supplies
    data, the broker supplies the authority AND the attribution."""
    board.call("board-comment", {"slug": "2026-08-20-plan-room", "text": "mine"})
    (call,) = [c for c in board.planroom_calls if c["path"].endswith("/comment")]
    assert call["payload"]["author"] == "res-test"


def test_a_caller_cannot_supply_its_own_author(board):
    resp = board.call("board-comment", {"slug": "2026-08-20-plan-room",
                                        "text": "x", "author": "plink"})
    assert resp["ok"] is False
    assert resp["error"]["code"] == "bad-args"


def test_a_flag_is_attributed_by_the_broker(board):
    board.call("board-flag", {"slug": "2026-08-20-plan-room",
                              "action": "blocked", "reason": "held"})
    (call,) = [c for c in board.planroom_calls if c["path"].endswith("/flag")]
    assert call["payload"]["author"] == "res-test"


# ── board-flag: a flag with a reason, never a column ────────────────────────

def test_blocking_needs_a_reason(board):
    """A card blocked for no stated reason is one nobody can unblock, because
    nobody can tell what would have to change."""
    resp = board.call("board-flag", {"slug": "2026-08-20-plan-room",
                                     "action": "blocked"})
    assert resp["ok"] is False
    assert resp["error"]["code"] == "bad-args"
    assert "reason" in resp["error"]["message"]
    assert board.planroom_calls == [], "a refused flag reached the network"


def test_blocking_with_a_blank_reason_is_refused(board):
    resp = board.call("board-flag", {"slug": "2026-08-20-plan-room",
                                     "action": "blocked", "reason": "   "})
    assert resp["ok"] is False
    assert resp["error"]["code"] == "bad-args"


def test_unblocking_needs_no_reason(board):
    resp = board.call("board-flag", {"slug": "2026-08-20-plan-room",
                                     "action": "unblock"})
    assert resp["ok"] is True
    assert resp["result"]["blocked"] is False


def test_a_blocked_card_does_not_move(board):
    resp = board.call("board-flag", {"slug": "2026-08-20-plan-room",
                                     "action": "blocked", "reason": "held"})
    assert resp["ok"] is True
    assert resp["result"]["blocked"] is True
    assert resp["result"]["column"] == "Ready", "blocked is a flag, not a column"


@pytest.mark.parametrize("action", ["archive", "merge", "Blocked", "", "move"])
def test_only_blocked_and_unblock_are_actions(board, action):
    resp = board.call("board-flag", {"slug": "2026-08-20-plan-room",
                                     "action": action, "reason": "x"})
    assert resp["ok"] is False
    assert resp["error"]["code"] == "bad-args"


# ── slugs are validated before they reach the network ───────────────────────

@pytest.mark.parametrize("slug", [
    "../../etc/passwd", "not a slug", "SPECS/2026-08-20-plan-room.md",
    "2026-08-20-" + "x" * 80, "", "keyboard-ZZZZZZZ", "backlog-abc",
    "-flag-looking", "2026-13-45-x/../y",
])
def test_a_slug_that_is_not_a_slug_never_reaches_the_server(board, slug):
    resp = board.call("board-card", {"slug": slug})
    assert resp["ok"] is False
    assert resp["error"]["code"] == "bad-args"
    assert board.planroom_calls == []


@pytest.mark.parametrize("slug", [
    "2026-08-20-plan-room", "backlog-12", "keyboard-abc1234",
    "keyboard-" + "a" * 40,
])
def test_the_three_legitimate_slug_shapes_are_accepted(board, slug):
    board.planroom_state["cards"] = [dict(CARD, slug=slug)]
    resp = board.call("board-card", {"slug": slug})
    assert resp["ok"] is True, resp


# ── skim by default, detail on request ──────────────────────────────────────

def test_board_list_returns_one_line_per_card(board):
    board.planroom_state["cards"] = [dict(CARD),
                                     dict(CARD, slug="2026-08-19-other",
                                          title="Another", column="Merged")]
    resp = board.call("board-list", {})
    assert resp["ok"] is True
    assert len(resp["result"]["cards"]) == 2
    assert all(isinstance(c, str) for c in resp["result"]["cards"])


def test_a_board_line_never_prints_a_bare_identifier(board):
    """brief's rule, inherited: an item you have to go look up is an item that
    gets deferred."""
    line = brokerd.format_board_line(CARD)
    assert "2026-08-20-plan-room" in line
    assert "The Plan Room" in line
    assert "Ready" in line
    assert "Claudette" in line
    assert "seq 1434" in line


def test_a_blocked_card_says_why_on_its_one_line(board):
    line = brokerd.format_board_line(
        dict(CARD, blocked=True, blocked_reason="waiting on the chown"))
    assert "BLOCKED: waiting on the chown" in line


def test_a_card_with_no_reason_still_says_it_is_blocked(board):
    line = brokerd.format_board_line(dict(CARD, blocked=True))
    assert "BLOCKED: no reason given" in line


def test_board_list_honours_its_limit(board):
    board.planroom_state["cards"] = [dict(CARD, slug=f"2026-08-{d:02d}-x")
                                     for d in range(1, 12)]
    resp = board.call("board-list", {"limit": 3})
    assert len(resp["result"]["cards"]) == 3
    assert resp["result"]["truncated"] is True


def test_board_list_passes_its_filters_through(board):
    board.call("board-list", {"column": "Ready", "owner": "Claudette",
                              "lane": "server", "blocked": "no"})
    (call,) = board.planroom_calls
    for bit in ("column=Ready", "owner=Claudette", "lane=server",
                "blocked=false"):
        assert bit in call["path"], call["path"]


def test_a_filter_value_is_url_encoded(board):
    """A lane called `a&b=c` must not become two query parameters."""
    board.call("board-list", {"lane": "a&b=c"})
    (call,) = board.planroom_calls
    assert "lane=a%26b%3Dc" in call["path"]


def test_blocked_takes_yes_or_no_and_nothing_else(board):
    assert board.call("board-list", {"blocked": "maybe"})["ok"] is False
    assert board.call("board-list", {"blocked": "yes"})["ok"] is True


def test_board_card_returns_comments(board):
    board.planroom_state["comments"]["2026-08-20-plan-room"] = [
        {"text": "one"}, {"text": "two"}]
    resp = board.call("board-card", {"slug": "2026-08-20-plan-room"})
    assert len(resp["result"]["comments"]) == 2
    assert resp["result"]["card"]["confirm_seq"] == 1434


def test_board_search_passes_the_text_through_encoded(board):
    board.call("board-search", {"text": "blueprint backdrop & grid"})
    (call,) = board.planroom_calls
    assert "q=blueprint%20backdrop%20%26%20grid" in call["path"]


def test_board_search_returns_one_line_per_hit(board):
    resp = board.call("board-search", {"text": "plan"})
    assert resp["ok"] is True
    assert resp["result"]["cards"] == [brokerd.format_board_line(CARD)]


# ── the face: staleness declared, never denied ──────────────────────────────

def test_every_read_says_when_it_was_derived_and_from_what(board):
    for verb, args in (("board-list", {}),
                       ("board-card", {"slug": "2026-08-20-plan-room"}),
                       ("board-search", {"text": "plan"})):
        face = board.call(verb, args)["result"]["face"]
        assert "abc1234deadb" in face, verb
        assert "2026-08-23" in face, verb


def test_an_unavailable_board_says_so_rather_than_looking_empty(board):
    board.planroom_state["face"] = {"available": False,
                                    "unavailable_reason": "index never written"}
    face = board.call("board-list", {})["result"]["face"]
    assert "UNAVAILABLE" in face
    assert "index never written" in face


def test_the_servers_refusal_is_carried_through_verbatim(board):
    """A resident told "the Plan Room index is unavailable" can act; one told
    "HTTP 503" has to go find someone."""
    board.planroom_state["http_error"] = "The Plan Room index is unavailable"
    resp = board.call("board-list", {})
    assert resp["ok"] is False
    assert resp["error"]["code"] == "exec-failure"
    assert resp["error"]["message"] == "The Plan Room index is unavailable"


def test_a_server_failure_audits_as_an_error_not_a_denial(board):
    """It ran and failed; it was not refused. The trail must tell them apart."""
    board.planroom_state["http_error"] = "boom"
    board.call("board-list", {})
    last = board.audit_lines()[-1]
    assert last["allowed"] is True
    assert "error" in last["result_summary"]


# ── the index rebuild ───────────────────────────────────────────────────────

def test_no_planroom_config_means_no_rebuild_and_no_complaint(harness):
    out = harness.broker._planroom_rebuild("test")
    assert out == {"rebuilt": False,
                   "reason": "no [planroom].index configured"}


def test_a_rebuild_failure_never_fails_refresh_mirror(harness, tmp_path,
                                                       monkeypatch):
    """A refresh that fetched everything correctly and then failed to rewrite a
    cache has still refreshed the mirror. Saying otherwise would teach
    residents that a red refresh-mirror means nothing."""
    harness.broker.planroom = {"index": str(tmp_path / "idx.db")}
    monkeypatch.setattr(brokerd, "_load_planroom_module",
                        lambda: (_ for _ in ()).throw(RuntimeError("no module")))
    harness.set_verbs(**{"refresh-mirror": True})
    resp = harness.call("refresh-mirror", {})
    assert resp["ok"] is True, "the refresh itself succeeded"
    assert resp["result"]["planroom"]["rebuilt"] is False
    assert "no module" in resp["result"]["planroom"]["reason"]
    assert "PLAN ROOM REBUILD FAILED" in harness.audit_lines()[-1]["result_summary"]


def test_refresh_mirror_rebuilds_the_index(harness, tmp_path, monkeypatch):
    calls = []

    class FakePlanroom:
        @staticmethod
        def derive_cards(config, lane_owners=None):
            calls.append({"config": config, "lane_owners": lane_owners})
            return {"face": {}, "cards": [{"slug": "x"}]}

        @staticmethod
        def rebuild(path, data):
            return ["plan room: x Ready → Building — a thing"]

    harness.broker.planroom = {"index": str(tmp_path / "idx.db"),
                               "lane_owners": {"server/": "Claudette"}}
    monkeypatch.setattr(brokerd, "_load_planroom_module", lambda: FakePlanroom)
    harness.set_verbs(**{"refresh-mirror": True})
    resp = harness.call("refresh-mirror", {})
    assert resp["result"]["planroom"] == {"rebuilt": True, "cards": 1,
                                          "transitions": 1,
                                          "why": "refresh-mirror"}
    assert calls[0]["lane_owners"] == {"server/": "Claudette"}


def test_a_transition_posts_exactly_one_custodian_line(harness, tmp_path,
                                                        monkeypatch):
    """ONE system line per COLUMN TRANSITION, never per edit."""
    class FakePlanroom:
        @staticmethod
        def derive_cards(config, lane_owners=None):
            return {"face": {}, "cards": []}

        @staticmethod
        def rebuild(path, data):
            return ["plan room: a Ready → Building — one",
                    "plan room: b Proposed → Ready — two"]

    harness.broker.planroom = {"index": str(tmp_path / "idx.db")}
    monkeypatch.setattr(brokerd, "_load_planroom_module", lambda: FakePlanroom)
    harness.broker._planroom_rebuild("test")
    assert len(harness.proposals) == 1
    body = harness.proposals[0]["body"]
    assert "a Ready → Building" in body
    assert "b Proposed → Ready" in body


def test_a_rebuild_that_moved_nothing_says_nothing(harness, tmp_path,
                                                    monkeypatch):
    """A detector that narrates every tick teaches everyone to stop reading it."""
    class FakePlanroom:
        @staticmethod
        def derive_cards(config, lane_owners=None):
            return {"face": {}, "cards": []}

        @staticmethod
        def rebuild(path, data):
            return []

    harness.broker.planroom = {"index": str(tmp_path / "idx.db")}
    monkeypatch.setattr(brokerd, "_load_planroom_module", lambda: FakePlanroom)
    harness.broker._planroom_rebuild("test")
    assert harness.proposals == []


def test_announce_false_suppresses_the_lines_but_not_the_rebuild(harness,
                                                                  tmp_path,
                                                                  monkeypatch):
    class FakePlanroom:
        @staticmethod
        def derive_cards(config, lane_owners=None):
            return {"face": {}, "cards": []}

        @staticmethod
        def rebuild(path, data):
            return ["plan room: a Ready → Building — one"]

    harness.broker.planroom = {"index": str(tmp_path / "idx.db"),
                               "announce": False}
    monkeypatch.setattr(brokerd, "_load_planroom_module", lambda: FakePlanroom)
    out = harness.broker._planroom_rebuild("test")
    assert out["rebuilt"] is True
    assert harness.proposals == []


def test_rebuild_on_refresh_can_be_turned_off(harness, tmp_path, monkeypatch):
    monkeypatch.setattr(brokerd, "_load_planroom_module",
                        lambda: (_ for _ in ()).throw(AssertionError("called")))
    harness.broker.planroom = {"index": str(tmp_path / "idx.db"),
                               "rebuild_on_refresh": False}
    harness.set_verbs(**{"refresh-mirror": True})
    resp = harness.call("refresh-mirror", {})
    assert resp["result"]["planroom"] == {"rebuilt": False,
                                          "reason": "disabled by config"}


def test_two_rebuilds_never_run_at_once(harness, tmp_path):
    """The timer and a verb can fire at the same moment, and two rebuilds racing
    on one temp file is how a cache becomes a corrupt file nobody can explain."""
    harness.broker.planroom = {"index": str(tmp_path / "idx.db")}
    harness.broker._planroom_lock.acquire()
    try:
        out = harness.broker._planroom_rebuild("test")
    finally:
        harness.broker._planroom_lock.release()
    assert out == {"rebuilt": False, "reason": "a rebuild is already running"}


def test_the_timer_does_not_arm_without_an_index(harness):
    harness.broker.planroom = {}
    harness.broker._start_planroom_timer()
    assert harness.broker._planroom_thread is None


def test_the_timer_arms_once_and_stops_on_shutdown(harness, tmp_path):
    harness.broker.planroom = {"index": str(tmp_path / "idx.db"),
                               "timer_sec": 3600}
    harness.broker._start_planroom_timer()
    first = harness.broker._planroom_thread
    assert first is not None and first.is_alive()
    harness.broker._start_planroom_timer()
    assert harness.broker._planroom_thread is first, "armed twice"
    harness.broker.shutdown()
    first.join(timeout=5)
    assert not first.is_alive()


def test_a_zero_interval_disables_the_timer(harness, tmp_path):
    harness.broker.planroom = {"index": str(tmp_path / "idx.db"),
                               "timer_sec": 0}
    harness.broker._start_planroom_timer()
    t = harness.broker._planroom_thread
    assert t is not None
    t.join(timeout=5)
    assert not t.is_alive(), "timer_sec = 0 means the thread returns at once"


# ── the derivation module the broker loads ──────────────────────────────────

def test_the_broker_hands_the_derivation_its_own_parsers():
    """P3, by the one route the rule did not name. Run as a daemon this file is
    `__main__`, so without the sys.modules line planroom would importlib-load a
    SECOND copy of the broker — two parsers of one Status line again."""
    import sys
    mod = brokerd._load_planroom_module()
    assert mod.brokerd() is sys.modules["brokerd"]
    assert sys.modules["brokerd"].parse_spec_status is brokerd.parse_spec_status


def test_the_derivation_module_is_loaded_once():
    assert brokerd._load_planroom_module() is brokerd._load_planroom_module()
