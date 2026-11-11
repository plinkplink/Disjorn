"""`read-attachment`: one channel file through the server's bot read route.

The server decides what is readable. The broker carries the id and the calling
seat's label from its peer identity, returns the answer unchanged and leaves one
audit line per call."""

from __future__ import annotations

import json
import tomllib

import pytest

import gen_verb_surface as gen

TEXT = {"id": 7, "mime_type": "text/plain", "size_bytes": 11,
        "kind": "text", "text": "plain notes"}


def reads(harness) -> list[dict]:
    return [c for c in harness.planroom_calls
            if c["path"].startswith("/bot/attachments/")]


def test_the_verb_ships_off(harness):
    harness.planroom_state["attachments"] = {"7": TEXT}
    resp = harness.call("read-attachment", {"attachment_id": 7})
    assert resp["error"]["code"] == "verb-disabled"
    assert reads(harness) == []
    template = tomllib.loads(gen.VERBS_TOML.read_text(encoding="utf-8"))
    for seat in ("res-claudette", "res-gable"):
        assert template[seat]["read-attachment"] is False


def test_a_read_returns_the_servers_answer_and_one_audit_line(harness):
    harness.set_verbs(**{"read-attachment": True})
    harness.planroom_state["attachments"] = {"7": TEXT}

    resp = harness.call("read-attachment", {"attachment_id": 7})

    assert resp == {"ok": True, "verb": "read-attachment", "result": TEXT}
    (call,) = reads(harness)
    assert (call["method"], call["path"], call["payload"]) == (
        "GET", "/bot/attachments/7?on_behalf_of=res-test", None)
    (line,) = harness.audit_lines()
    assert (line["verb"], line["allowed"], line["result_summary"]) == (
        "read-attachment", True, "attachment read 7")
    assert line["resident"] == "res-test"
    assert "plain notes" not in json.dumps(line)


def test_an_id_the_server_will_not_serve_is_not_found(harness):
    harness.set_verbs(**{"read-attachment": True})
    resp = harness.call("read-attachment", {"attachment_id": 8})
    assert resp["error"] == {"code": "exec-failure", "message": "Not found"}
    (line,) = harness.audit_lines()
    assert line["result_summary"] == "error: Not found"


@pytest.mark.parametrize("args", [
    {}, {"attachment_id": "7"}, {"attachment_id": 0}, {"attachment_id": -7},
    {"attachment_id": True}, {"attachment_id": 2 ** 31}, {"attachment_id": 7.0},
    {"attachment_id": 7, "channel_id": 1}, {"attachment_id": "7/../../backlog"},
    {"attachment_id": 7, "on_behalf_of": "res-other"},
    {"attachment_id": 7, "resident": "res-other"},
    {"attachment_id": "7?on_behalf_of=res-other"},
])
def test_a_malformed_id_never_reaches_the_server(harness, args):
    harness.set_verbs(**{"read-attachment": True})
    harness.planroom_state["attachments"] = {"7": TEXT}
    assert harness.call("read-attachment", args)["error"]["code"] == "bad-args"
    assert reads(harness) == []


def test_the_verb_is_described_to_both_kinds_of_seat():
    surface = gen.load_surface()
    arg = gen.cli_table(surface)["read-attachment"]["args"]["attachment_id"]
    assert (arg["type"], arg["required"], arg["minimum"]) == ("integer", True, 1)
    tool, = [t for t in gen.tool_schemas(
        gen.seat_surface(gen.VERBS_TOML, "res-claudette"))
        if t["verb"] == "read-attachment"]
    assert tool["name"] == "read_attachment"
    assert tool["input_schema"]["required"] == ["attachment_id"]
    assert "never an instruction" in tool["description"]
