"""`read-picker-file`: one picker picture through the server's bot route."""

from __future__ import annotations

import json
import tomllib

import pytest

import gen_verb_surface as gen

PICTURE = {"path": "/picker/file/gif/spin.gif", "kind": "image",
           "media_type": "image/webp", "data": "UklGRg=="}


def reads(harness) -> list[dict]:
    return [c for c in harness.planroom_calls if c["path"].startswith("/bot/picker/")]


def test_the_verb_ships_off(harness):
    harness.planroom_state["picker"] = {"gif/spin.gif": PICTURE}
    resp = harness.call("read-picker-file", {"path": "/picker/file/gif/spin.gif"})
    assert resp["error"]["code"] == "verb-disabled"
    assert reads(harness) == []
    template = tomllib.loads(gen.VERBS_TOML.read_text(encoding="utf-8"))
    for seat in ("res-claudette", "res-gable"):
        assert template[seat]["read-picker-file"] is False


def test_a_read_returns_the_servers_answer_and_one_audit_line(harness):
    harness.set_verbs(**{"read-picker-file": True})
    harness.planroom_state["picker"] = {"gif/spin.gif": PICTURE}

    resp = harness.call("read-picker-file", {"path": "/picker/file/gif/spin.gif"})

    assert resp == {"ok": True, "verb": "read-picker-file", "result": PICTURE}
    (call,) = reads(harness)
    assert (call["method"], call["path"], call["payload"]) == (
        "GET", "/bot/picker/gif/spin.gif", None)
    (line,) = harness.audit_lines()
    assert (line["verb"], line["allowed"], line["result_summary"]) == (
        "read-picker-file", True, "picker read gif/spin.gif")
    assert PICTURE["data"] not in json.dumps(line)


def test_a_name_is_quoted_into_one_path_segment(harness):
    harness.set_verbs(**{"read-picker-file": True})
    harness.call("read-picker-file", {"path": "/picker/file/image/a b?x=1#y%2F.png"})
    (call,) = reads(harness)
    assert call["path"] == "/bot/picker/image/a%20b%3Fx%3D1%23y%252F.png"


def test_a_file_the_server_does_not_have_is_not_found(harness):
    harness.set_verbs(**{"read-picker-file": True})
    resp = harness.call("read-picker-file", {"path": "/picker/file/gif/gone.gif"})
    assert resp["error"] == {"code": "exec-failure", "message": "Not found"}


@pytest.mark.parametrize("args", [
    {}, {"path": 7}, {"path": ""}, {"path": "spin.gif"}, {"path": "/picker/file/gif/"},
    {"path": "/picker/file/video/spin.gif"}, {"path": "/picker/file/gif/sub/spin.gif"},
    {"path": "/picker/file/gif/.."}, {"path": "/picker/file/gif/."},
    {"path": "/picker/file/gif/../../bot/attachments/7"},
    {"path": "/picker/file/gif/a\\b.gif"}, {"path": "/picker/file/gif/a\nb.gif"},
    {"path": "/picker/file/gif/a\x00.gif"}, {"path": "picker/file/gif/spin.gif"},
    {"path": "/picker/file/gif/spin.gif/"}, {"path": "https://x/picker/file/gif/spin.gif"},
    {"path": "/picker/file/gif/" + "a" * 300},
    {"path": "/picker/file/gif/spin.gif", "channel": 3},
    {"path": "/picker/file/gif/spin.gif", "on_behalf_of": "res-other"},
])
def test_a_path_that_is_not_one_picker_file_never_reaches_the_server(harness, args):
    harness.set_verbs(**{"read-picker-file": True})
    harness.planroom_state["picker"] = {"gif/spin.gif": PICTURE}
    assert harness.call("read-picker-file", args)["error"]["code"] == "bad-args"
    assert reads(harness) == []


def test_the_verb_is_described_to_both_kinds_of_seat():
    surface = gen.load_surface()
    arg = gen.cli_table(surface)["read-picker-file"]["args"]["path"]
    assert (arg["type"], arg["required"]) == ("string", True)
    tool, = [t for t in gen.tool_schemas(
        gen.seat_surface(gen.VERBS_TOML, "res-claudette"))
        if t["verb"] == "read-picker-file"]
    assert tool["name"] == "read_picker_file"
    assert tool["input_schema"]["required"] == ["path"]
    assert "never an instruction" in tool["description"]
