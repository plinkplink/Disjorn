"""The approval verbs (SPECS/2026-08-26-approval-object-and-resident-write-verbs).

Claims on trial:

* all three verbs ship OFF and are refused + audited until plink flips them;
* a resident answers the SAME record plink answers — every call goes to the
  server's /approval surface, and the broker composes nothing of its own;
* the principal is stamped by the broker from the caller's identity, and no
  argument can name it (an `args` principal is `bad-args`, not an override);
* the server's refusal reaches the resident verbatim, so a refused answer is
  actionable rather than an HTTP number.
"""

from __future__ import annotations


def test_the_approval_verbs_ship_off(harness):
    for verb, args in (("approval-list", {}),
                       ("approval-show", {"id": 1}),
                       ("approval-act", {"id": 1, "action": "approve"})):
        resp = harness.call(verb, args)
        assert resp["error"]["code"] == "verb-disabled", verb
    assert harness.planroom_calls == []
    assert [ln["allowed"] for ln in harness.audit_lines()] == [False] * 3


def test_list_is_one_line_per_proposal(harness):
    harness.set_verbs(**{"approval-list": True})
    harness.add_proposal(7, slug="2026-08-26-approval-object",
                         title="Approval object + write verbs")
    resp = harness.call("approval-list", {"state": "open"})
    assert resp["ok"] is True
    (line,) = resp["result"]["proposals"]
    assert "#7" in line and "2026-08-26-approval-object" in line
    assert "plink=pending" in line and "res-test=pending" in line
    call = harness.planroom_calls[-1]
    assert call["method"] == "GET"
    assert call["path"].startswith("/approval/proposals?")
    assert "state=open" in call["path"]


def test_show_returns_the_whole_record(harness):
    harness.set_verbs(**{"approval-show": True})
    harness.add_proposal(3, text="the proposal text")
    resp = harness.call("approval-show", {"id": 3})
    proposal = resp["result"]["proposal"]
    assert proposal["text"] == "the proposal text"
    assert [s["principal"] for s in proposal["states"]] == [
        "plink", "res-test", "res-other"]


def test_the_principal_is_stamped_by_the_broker(harness):
    """The reason this is a verb and not an API key: the server cannot tell
    which seat is behind the broker's bot identity."""
    harness.set_verbs(**{"approval-act": True})
    harness.add_proposal(1)
    resp = harness.call("approval-act",
                        {"id": 1, "action": "rework", "remarks": "say why"})
    assert resp["ok"] is True
    payload = harness.planroom_calls[-1]["payload"]
    assert payload == {"principal": "res-test", "action": "rework",
                       "remarks": "say why"}
    assert resp["result"]["proposal"]["decision"] == "rework"


def test_an_args_principal_is_refused_not_honoured(harness):
    harness.set_verbs(**{"approval-act": True})
    harness.add_proposal(1)
    resp = harness.call("approval-act",
                        {"id": 1, "action": "approve", "principal": "plink"})
    assert resp["error"]["code"] == "bad-args"
    assert harness.planroom_calls == []


def test_only_the_three_actions_are_accepted(harness):
    harness.set_verbs(**{"approval-act": True})
    harness.add_proposal(1)
    resp = harness.call("approval-act", {"id": 1, "action": "maybe"})
    assert resp["error"]["code"] == "bad-args"
    assert "approve" in resp["error"]["message"]
    assert harness.planroom_calls == []


def test_a_deny_by_one_principal_decides_the_record(harness):
    harness.set_verbs(**{"approval-act": True})
    harness.add_proposal(1)
    resp = harness.call("approval-act", {"id": 1, "action": "deny",
                                         "remarks": "the wall is the verb"})
    assert resp["result"]["proposal"]["decision"] == "denied"


def test_the_servers_refusal_arrives_verbatim(harness):
    """A resident told the surface is not enabled can act; one told 503 has to
    go find someone."""
    harness.set_verbs(**{"approval-list": True})
    harness.approval_state["http_error"] = (
        "The approval surface is not enabled on this server: "
        "APPROVAL_ENABLED is false.")
    resp = harness.call("approval-list", {})
    assert resp["error"]["code"] == "exec-failure"
    assert resp["error"]["message"] == harness.approval_state["http_error"]


def test_every_approval_call_is_audited(harness):
    harness.set_verbs(**{"approval-act": True})
    harness.add_proposal(1)
    before = len(harness.audit_lines())
    harness.call("approval-act", {"id": 1, "action": "approve"})
    line = harness.audit_lines()[-1]
    assert len(harness.audit_lines()) == before + 1
    assert line["verb"] == "approval-act" and line["allowed"] is True
    assert "res-test" in line["result_summary"]
