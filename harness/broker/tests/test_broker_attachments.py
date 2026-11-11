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


# ---------------------------------------------------------------- post-file

import ast
import base64
import hashlib
import os
from pathlib import Path

import broker_planroom

BODY = b"# report\nfigures\n"


@pytest.fixture()
def outbox(harness, tmp_path):
    """The calling seat's outbox, with the verb on."""
    box = tmp_path / "outbox" / "res-test"
    box.mkdir(parents=True)
    harness.broker.paths["files_outbox_root"] = str(tmp_path / "outbox")
    harness.set_verbs(**{"post-file": True})
    return box


def posts(harness) -> list[dict]:
    return [c for c in harness.planroom_calls if c["path"] == "/bot/attachments"]


def post(harness, name="report.md", **more) -> dict:
    return harness.call("post-file", {"path": f"/files-outbox/{name}", "channel": 3, **more})


def key_of(channel: int, name: str, text: str, body: bytes) -> str:
    return hashlib.sha256(json.dumps(
        [channel, name, text, hashlib.sha256(body).hexdigest()]).encode()).hexdigest()


def test_post_file_ships_off(harness, tmp_path):
    box = tmp_path / "outbox" / "res-test"
    box.mkdir(parents=True)
    harness.broker.paths["files_outbox_root"] = str(tmp_path / "outbox")
    (box / "report.md").write_bytes(BODY)

    assert post(harness)["error"]["code"] == "verb-disabled"
    assert posts(harness) == [] and (box / "report.md").exists()
    template = tomllib.loads(gen.VERBS_TOML.read_text(encoding="utf-8"))
    for seat in ("res-claudette", "res-gable"):
        assert template[seat]["post-file"] is False


def test_a_post_sends_the_file_as_the_calling_seat_and_empties_the_outbox(harness, outbox):
    (outbox / "report.md").write_bytes(BODY)

    resp = post(harness, text="the figures")

    assert resp == {"ok": True, "verb": "post-file", "result": {
        "attachment_id": 41, "message_id": 9, "seq": 3, "channel_id": 3,
        "size_bytes": len(BODY), "outbox_removed": True}}
    (call,) = posts(harness)
    assert (call["method"], call["payload"]) == ("POST", {
        "on_behalf_of": "res-test", "channel_id": 3, "filename": "report.md",
        "data": base64.b64encode(BODY).decode(), "text": "the figures",
        "daily_quota_bytes": 25 * 1024 * 1024,
        "idempotency_key": key_of(3, "report.md", "the figures", BODY)})
    assert list(outbox.iterdir()) == []
    (line,) = harness.audit_lines()
    assert (line["verb"], line["allowed"], line["result_summary"]) == (
        "post-file", True, "attachment post 41")
    assert "figures\\n" not in json.dumps(line)
    assert base64.b64encode(BODY).decode() not in json.dumps(line)


def test_the_quota_sent_is_the_seats_own_line_in_broker_toml(harness, outbox):
    harness.broker.budgets["res-test"] = {"bot_daily_upload_bytes": 500}
    harness.broker.budgets["res-other"] = {"bot_daily_upload_bytes": 9}
    (outbox / "report.md").write_bytes(BODY)

    assert post(harness)["ok"] is True
    assert posts(harness)[0]["payload"]["daily_quota_bytes"] == 500


@pytest.mark.parametrize("quota", [True, "500", -1, 1.5])
def test_a_quota_that_is_not_a_byte_count_posts_nothing(harness, outbox, quota):
    harness.broker.budgets["res-test"] = {"bot_daily_upload_bytes": quota}
    (outbox / "report.md").write_bytes(BODY)

    assert post(harness)["error"]["code"] == "internal"
    assert posts(harness) == [] and (outbox / "report.md").exists()


def test_the_servers_refusal_reaches_the_seat_and_the_file_stays(harness, outbox):
    words = "daily upload quota is 500 bytes; 490 are used today and this file is 17"
    harness.planroom_state["post_refusal"] = words
    (outbox / "report.md").write_bytes(BODY)

    assert post(harness)["error"] == {"code": "exec-failure", "message": words}
    assert (outbox / "report.md").read_bytes() == BODY
    assert harness.audit_lines()[0]["result_summary"] == f"error: {words}"


def test_a_file_over_the_quota_is_refused_before_any_network_call(harness, outbox):
    harness.broker.budgets["res-test"] = {"bot_daily_upload_bytes": 16}
    (outbox / "report.md").write_bytes(BODY)

    error = post(harness)["error"]

    assert error["code"] == "bad-args"
    assert "report.md is 17 bytes" in error["message"] and "1 to 16 bytes" in error["message"]
    assert posts(harness) == [] and (outbox / "report.md").exists()


@pytest.mark.parametrize("name", [
    "a.zip", "a.exe", "a.html", "a.svg", "a.xml", "a.mp4", "noextension", "a.unknownext",
])
def test_a_type_no_bot_may_post_is_refused_before_any_network_call(harness, outbox, name):
    (outbox / name).write_bytes(BODY)

    error = post(harness, name)["error"]

    assert error["code"] == "bad-args" and "may not post a file of this type" in error["message"]
    assert posts(harness) == [] and (outbox / name).exists()


@pytest.mark.parametrize("path", [
    "report.md", "/files-outbox/", "/files-outbox/..", "/files-outbox/.",
    "/files-outbox/../outside.md", "/files-outbox/sub/report.md",
    "/files-outbox/sub/../report.md", "/files/report.md", "/etc/passwd",
    "/files-outbox//report.md", "files-outbox/report.md", "/files-outbox/re\nport.md",
    "/files-outbox/report.md\x00", "/files-outbox/" + "a" * 300 + ".md", "", 7,
])
def test_a_path_that_is_not_one_file_in_the_outbox_never_reaches_the_server(
    harness, outbox, tmp_path, path
):
    (outbox / "report.md").write_bytes(BODY)
    (outbox / "sub").mkdir()
    (outbox / "sub" / "report.md").write_bytes(BODY)
    (tmp_path / "outbox" / "outside.md").write_bytes(BODY)

    resp = harness.call("post-file", {"path": path, "channel": 3})

    assert resp["error"]["code"] == "bad-args"
    assert posts(harness) == []


@pytest.mark.parametrize("args", [
    {}, {"channel": "3"}, {"channel": 0}, {"channel": True}, {"channel": 2 ** 31},
    {"channel": 3, "on_behalf_of": "res-other"}, {"channel": 3, "resident": "res-other"},
    {"channel": 3, "daily_quota_bytes": 10 ** 12}, {"channel": 3, "filename": "x.md"},
    {"channel": 3, "text": 7}, {"channel": 3, "text": ""},
    {"channel": 3, "model": 7}, {"channel": 3, "model": ""},
    {"channel": 3, "model": "m" * 101}, {"channel": 3, "summoner": "s" * 101},
    {"channel": 3, "attribution": {"model": "x"}}, {"channel": 3, "verified": True},
    {"channel": 3, "idempotency_key": "ab" * 32},
])
def test_malformed_post_args_never_reach_the_server(harness, outbox, args):
    (outbox / "report.md").write_bytes(BODY)

    resp = harness.call("post-file", {"path": "/files-outbox/report.md", **args})

    assert resp["error"]["code"] == "bad-args"
    assert posts(harness) == [] and (outbox / "report.md").exists()


def test_a_link_out_of_the_outbox_is_not_followed(harness, outbox, tmp_path):
    secret = tmp_path / "secret.md"
    secret.write_bytes(BODY)
    (outbox / "report.md").symlink_to(secret)

    assert post(harness)["error"]["code"] == "bad-args"
    assert posts(harness) == [] and secret.read_bytes() == BODY


def test_an_outbox_that_is_itself_a_link_is_not_entered(harness, tmp_path):
    real = tmp_path / "elsewhere"
    real.mkdir()
    (real / "report.md").write_bytes(BODY)
    (tmp_path / "outbox").mkdir()
    (tmp_path / "outbox" / "res-test").symlink_to(real)
    harness.broker.paths["files_outbox_root"] = str(tmp_path / "outbox")
    harness.set_verbs(**{"post-file": True})

    assert post(harness)["error"] == {
        "code": "exec-failure", "message": "no outbox is set up for res-test"}
    assert posts(harness) == [] and (real / "report.md").exists()


def test_a_seat_with_no_outbox_posts_nothing(harness, tmp_path):
    (tmp_path / "outbox" / "res-other").mkdir(parents=True)
    (tmp_path / "outbox" / "res-other" / "report.md").write_bytes(BODY)
    harness.broker.paths["files_outbox_root"] = str(tmp_path / "outbox")
    harness.set_verbs(**{"post-file": True})

    assert post(harness)["error"]["message"] == "no outbox is set up for res-test"
    assert posts(harness) == []


def test_a_file_another_uid_wrote_is_not_read(harness, outbox, monkeypatch):
    (outbox / "report.md").write_bytes(BODY)
    real = os.fstat

    class Foreign:
        def __init__(self, st):
            self.st_mode, self.st_size, self.st_uid = st.st_mode, st.st_size, st.st_uid + 1

    monkeypatch.setattr(broker_planroom.os, "fstat", lambda fd: Foreign(real(fd)))

    error = post(harness)["error"]

    assert error["code"] == "bad-args" and "the seat itself wrote" in error["message"]
    assert posts(harness) == [] and (outbox / "report.md").exists()


@pytest.mark.parametrize("kind", ["directory", "fifo", "empty", "missing"])
def test_only_a_regular_file_with_bytes_in_it_is_posted(harness, outbox, kind):
    target = outbox / "report.md"
    if kind == "directory":
        target.mkdir()
    elif kind == "fifo":
        os.mkfifo(target)
    elif kind == "empty":
        target.write_bytes(b"")

    assert post(harness)["error"]["code"] == "bad-args"
    assert posts(harness) == []


def test_the_postable_types_match_the_servers():
    names = {"PDF_MIME", "_ALLOWED_PREFIXES", "_ALLOWED_MIMES", "_UNPOSTABLE_MIMES"}
    source = Path(__file__).resolve().parents[3] / "server/app/services/bot_files.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))
    tree.body = [n for n in tree.body
                 if isinstance(n, ast.Assign) and getattr(n.targets[0], "id", None) in names]
    server: dict = {}
    exec(compile(tree, str(source), "exec"), server)

    assert broker_planroom.POSTABLE_MIME_PREFIXES == server["_ALLOWED_PREFIXES"]
    assert broker_planroom.POSTABLE_MIMES == server["_ALLOWED_MIMES"]
    assert broker_planroom.UNPOSTABLE_MIMES == server["_UNPOSTABLE_MIMES"]


def test_post_file_is_described_to_both_kinds_of_seat():
    surface = gen.load_surface()
    args = gen.cli_table(surface)["post-file"]["args"]
    assert {k: (v["type"], v["required"]) for k, v in args.items()} == {
        "path": ("string", True), "channel": ("integer", True), "text": ("string", False),
        "model": ("string", False), "summoner": ("string", False)}
    assert {k: v["env"] for k, v in args.items() if "env" in v} == {
        "model": "DISJORN_SUMMON_MODEL", "summoner": "DISJORN_SUMMONER"}
    tool, = [t for t in gen.tool_schemas(
        gen.seat_surface(gen.VERBS_TOML, "res-gable")) if t["verb"] == "post-file"]
    assert tool["name"] == "post_file"
    assert tool["input_schema"]["required"] == ["path", "channel"]
    assert sorted(tool["input_schema"]["properties"]) == ["channel", "path", "text"]
    assert "posts nothing twice" in " ".join(tool["description"].split())


# ------------------------------------------------- a retry, and who is posting

def test_a_retry_after_a_failed_call_sends_the_same_key(harness, outbox):
    (outbox / "report.md").write_bytes(BODY)
    harness.planroom_state["http_error"] = "attachment API unreachable: timed out"
    assert post(harness, text="the figures")["error"]["code"] == "exec-failure"
    assert (outbox / "report.md").read_bytes() == BODY
    harness.planroom_state["http_error"] = None

    assert post(harness, text="the figures")["ok"] is True

    first, again = (c["payload"]["idempotency_key"] for c in posts(harness))
    assert first == again == key_of(3, "report.md", "the figures", BODY)


@pytest.mark.parametrize("change", ["text", "channel", "bytes", "name"])
def test_a_different_post_sends_a_different_key(harness, outbox, change):
    (outbox / "report.md").write_bytes(BODY)
    assert post(harness, text="the figures")["ok"] is True
    name = "other.md" if change == "name" else "report.md"
    (outbox / name).write_bytes(BODY + b"!" if change == "bytes" else BODY)

    assert harness.call("post-file", {
        "path": f"/files-outbox/{name}", "channel": 4 if change == "channel" else 3,
        "text": "said differently" if change == "text" else "the figures"})["ok"] is True

    first, second = (c["payload"]["idempotency_key"] for c in posts(harness))
    assert first != second


@pytest.mark.parametrize("said, sent", [
    ({"model": "claude-x", "summoner": "plink"}, {"model": "claude-x", "summoner": "plink"}),
    ({"summoner": "plink"}, {"model": None, "summoner": "plink"}),
    ({"model": "claude-x"}, {"model": "claude-x", "summoner": None}),
])
def test_what_the_seat_says_produced_the_file_is_passed_on_unverified(
    harness, outbox, said, sent
):
    (outbox / "report.md").write_bytes(BODY)

    assert post(harness, **said)["ok"] is True

    assert posts(harness)[0]["payload"]["attribution"] == sent


def test_a_post_that_says_nothing_of_its_origin_sends_no_attribution(harness, outbox):
    (outbox / "report.md").write_bytes(BODY)

    assert post(harness)["ok"] is True

    assert "attribution" not in posts(harness)[0]["payload"]


class Answer:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self) -> bytes:
        return b"{}"


@pytest.mark.parametrize("cfg, method, path, expected", [
    ({}, "POST", "/bot/attachments", 120),
    ({"file_post_timeout_sec": 300}, "POST", "/bot/attachments", 300),
    ({"file_post_timeout_sec": 300}, "GET", "/bot/attachments/7?on_behalf_of=res-test", 20),
    ({"file_post_timeout_sec": 300}, "POST", "/backlog", 20),
])
def test_only_a_file_post_waits_longer_for_the_server(
    tmp_path, monkeypatch, cfg, method, path, expected
):
    import urllib.request

    (tmp_path / "key").write_text("k")
    waited = []
    monkeypatch.setattr(urllib.request, "urlopen",
                        lambda req, timeout: waited.append(timeout) or Answer())

    broker_planroom._planroom_http(
        {"url": "http://127.0.0.1:1", "api_key_path": str(tmp_path / "key"), **cfg},
        method, path, {} if method == "POST" else None)

    assert waited == [expected]
