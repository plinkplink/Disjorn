"""WP-H5: broker CLI arg validation + socket round-trip tests.

Client-side schemas must mirror PROTOCOL.md closely enough that obviously
bad calls fail locally (exit 2, no socket contact), while valid calls do a
clean one-request/one-response round trip and map broker error codes to
the documented exit codes.
"""

from __future__ import annotations

import json

import pytest


def run_cli(broker_cli, capsys, argv, env=None):
    """Run the CLI's main(); return (exit_code, parsed stdout JSON)."""
    code = broker_cli.main(argv, environ=env or {})
    out = capsys.readouterr().out
    return code, (json.loads(out) if out.strip() else None)


# ── client-side validation: must fail with exit 2, without a socket ──────

@pytest.mark.parametrize("argv", [
    ["read-own-log", "--lines", "0"],
    ["read-own-log", "--lines", "501"],
    ["read-own-log", "--grep", "x" * 201],
    ["read-own-log", "--grep", ""],
    ["read-prod-logs", "--lines", "-5"],
    ["file-proposal", "--text", ""],
    ["file-proposal", "--text", "x" * 4001],
    ["classify-diff", "--repo", "relative/path", "--range", "main..wip"],
    ["classify-diff", "--repo", "/a/../b", "--range", "main..wip"],
    ["classify-diff", "--repo", "/repo", "--range=-rf..main"],
    ["classify-diff", "--repo", "/repo", "--range", "bad range!"],
    ["classify-diff", "--repo", "/repo", "--range", "a" * 201],
    ["classify-diff", "--repo", "/repo", "--range", "m..w", "--gates", "not json"],
    ["classify-diff", "--repo", "/repo", "--range", "m..w", "--gates", "[1,2]"],
    ["classify-diff", "--repo", "/repo", "--range", "m..w",
     "--gates", json.dumps({"pad": "x" * 9000})],
    ["query-own-audit", "--from", "2026/07/19", "--to", "2026-07-19"],
    ["query-own-audit", "--from", "2026-07-19", "--to", "19-07-2026"],
    ["query-own-audit", "--from", "2026-07-01", "--to", "2026-07-19",
     "--limit", "501"],
    ["start-build", "--spec", ""],
    ["start-build", "--spec=-oops.md"],
])
def test_client_side_rejection(broker_cli, capsys, argv):
    # No socket exists at this path: reaching the transport would exit 3,
    # so exit 2 proves rejection happened before any connection attempt.
    code, out = run_cli(broker_cli, capsys,
                        ["--socket", "/nonexistent/broker.sock"] + argv)
    assert code == 2, f"expected local rejection for {argv}"
    assert out["ok"] is False
    assert out["error"]["code"] == "bad-args-local"


def test_unknown_flag_rejected(broker_cli, capsys):
    with pytest.raises(SystemExit) as exc:
        broker_cli.main(["read-metrics", "--frobnicate"], environ={})
    assert exc.value.code == 2
    capsys.readouterr()


def test_valid_args_pass_client_validation(broker_cli):
    """The mirrored schemas accept everything PROTOCOL.md accepts."""
    parser = broker_cli.build_parser()
    good = [
        ["read-own-log"],
        ["read-own-log", "--lines", "500", "--grep", "ERROR",
         "--path", "/home/resident/logs/x.log"],
        ["read-prod-logs", "--lines", "1"],
        ["classify-diff", "--repo", "/home/resident/worktree",
         "--range", "main..res/gable/topic",
         "--gates", '{"tests": true, "typecheck": true}'],
        ["file-proposal", "--text", "I noticed a thing."],
        ["query-own-audit", "--from", "2026-07-01", "--to", "2026-07-19",
         "--limit", "500"],
        ["restart-disjorn"],
        ["run-server-tests"],
        ["refresh-mirror"],
        ["start-build", "--spec", "2026-07-21-gif-picker.md"],
        ["read-metrics"],
    ]
    for argv in good:
        ns = parser.parse_args(argv)
        req = broker_cli.build_request(ns.verb, ns)
        assert req["verb"] == argv[0]
        assert isinstance(req["args"], dict)


def test_only_provided_keys_sent(broker_cli):
    """Defaults are the server's job: omitted flags must not appear."""
    parser = broker_cli.build_parser()
    ns = parser.parse_args(["read-own-log"])
    assert broker_cli.build_request("read-own-log", ns) == \
        {"verb": "read-own-log", "args": {}}
    ns = parser.parse_args(["read-own-log", "--lines", "7"])
    assert broker_cli.build_request("read-own-log", ns)["args"] == {"lines": 7}


# ── BROKER_DISABLE kill switch ───────────────────────────────────────────

def test_broker_disable_refuses_before_socket(broker_cli, capsys):
    code, out = run_cli(broker_cli, capsys, ["read-metrics"],
                        env={"BROKER_DISABLE": "1"})
    assert code == 20
    assert out["error"]["code"] == "broker-disabled-local"


def test_broker_disable_empty_string_is_off(broker_cli, capsys, fake_broker):
    sock, _ = fake_broker
    code, out = run_cli(broker_cli, capsys,
                        ["--socket", sock, "read-metrics"],
                        env={"BROKER_DISABLE": ""})
    assert code == 0 and out["ok"] is True


# ── round trip against the fake broker ───────────────────────────────────

def test_round_trip_ok(broker_cli, capsys, fake_broker):
    sock, _ = fake_broker
    code, out = run_cli(broker_cli, capsys,
                        ["--socket", sock, "read-own-log", "--lines", "5"])
    assert code == 0
    assert out["ok"] is True and out["verb"] == "read-own-log"
    assert out["result"]["lines"]


@pytest.mark.parametrize("error_code,exit_code", [
    ("unknown-caller", 10), ("unknown-verb", 11), ("verb-disabled", 12),
    ("bad-args", 13), ("exec-failure", 14), ("internal", 15),
])
def test_broker_errors_map_to_exit_codes(broker_cli, capsys, fake_broker,
                                         error_code, exit_code):
    sock, denials = fake_broker
    denials["read-metrics"] = error_code
    code, out = run_cli(broker_cli, capsys, ["--socket", sock, "read-metrics"])
    assert code == exit_code
    assert out["error"]["code"] == error_code


def test_transport_failure_exit_3(broker_cli, capsys):
    code, out = run_cli(broker_cli, capsys,
                        ["--socket", "/nonexistent/broker.sock", "read-metrics"])
    assert code == 3
    assert out["error"]["code"] == "transport"


# ── an arg the harness stamps through the environment ────────────────────

POST_FILE = ["post-file", "--path", "/files-outbox/report.md", "--channel", "3"]


def stamped(broker_cli, environ: dict) -> dict:
    parser = broker_cli.build_parser()
    return broker_cli.build_request("post-file", parser.parse_args(POST_FILE),
                                    environ)["args"]


def test_post_file_takes_model_and_summoner_from_the_environment(broker_cli):
    args = stamped(broker_cli, {"DISJORN_SUMMON_MODEL": "claude-x",
                                "DISJORN_SUMMONER": "plink"})
    assert args == {"path": "/files-outbox/report.md", "channel": 3,
                    "model": "claude-x", "summoner": "plink"}


@pytest.mark.parametrize("environ", [
    {}, {"DISJORN_SUMMON_MODEL": "", "DISJORN_SUMMONER": ""},
    {"DISJORN_SUMMON_MODEL": "m" * 101, "DISJORN_SUMMONER": "a\x00b"},
    {"MODEL": "claude-x", "SUMMONER": "plink"},
])
def test_an_absent_or_unusable_stamp_is_left_out_and_the_call_still_goes(
        broker_cli, environ):
    assert stamped(broker_cli, environ) == {
        "path": "/files-outbox/report.md", "channel": 3}


@pytest.mark.parametrize("flag", ["--model", "--summoner"])
def test_a_stamped_arg_has_no_flag(broker_cli, capsys, flag):
    with pytest.raises(SystemExit) as exc:
        broker_cli.main(POST_FILE + [flag, "claude-x"], environ={})
    assert exc.value.code == 2
    capsys.readouterr()


# ── an image result goes to a file, never to the terminal ────────────────

import base64
import os

WEBP = b"RIFF\x12\x00\x00\x00WEBPVP8 " + bytes(10)
PICKED = ["read-picker-file", "--path", "/picker/file/gif/spin.gif"]


def answers(broker_cli, monkeypatch, result):
    monkeypatch.setattr(broker_cli, "call_broker", lambda sock, req: {
        "ok": True, "verb": req["verb"], "result": json.loads(json.dumps(result))})


def test_an_image_result_is_saved_and_its_base64_is_not_printed(
    broker_cli, capsys, monkeypatch, tmp_path
):
    data = base64.b64encode(WEBP).decode()
    answers(broker_cli, monkeypatch, {
        "path": "/picker/file/gif/spin.gif", "kind": "image",
        "media_type": "image/webp", "data": data})

    code = broker_cli.main(PICKED, environ={"BROKER_IMAGE_DIR": str(tmp_path / "seen")})
    printed = capsys.readouterr().out
    result = json.loads(printed)["result"]

    assert code == 0 and data not in printed and "data" not in result
    assert result["file"].startswith(str(tmp_path / "seen") + "/image-")
    assert result["file"].endswith(".webp") and result["file_bytes"] == len(WEBP)
    assert open(result["file"], "rb").read() == WEBP
    assert os.stat(result["file"]).st_mode & 0o077 == 0
    assert (result["path"], result["kind"]) == ("/picker/file/gif/spin.gif", "image")


def test_two_reads_of_one_picture_do_not_share_a_file(
    broker_cli, capsys, monkeypatch, tmp_path
):
    answers(broker_cli, monkeypatch, {
        "kind": "image", "media_type": "image/webp", "data": base64.b64encode(WEBP).decode()})
    files = []
    for _ in range(2):
        broker_cli.main(PICKED, environ={"BROKER_IMAGE_DIR": str(tmp_path)})
        files.append(json.loads(capsys.readouterr().out)["result"]["file"])
    assert files[0] != files[1]


@pytest.mark.parametrize("data, folder", [("not base64 !!", "ok"), ("QUJD", "\x00bad")])
def test_an_image_that_cannot_be_saved_still_prints_no_base64(
    broker_cli, capsys, monkeypatch, tmp_path, data, folder
):
    answers(broker_cli, monkeypatch, {"kind": "image", "media_type": "image/webp", "data": data})

    code = broker_cli.main(PICKED, environ={"BROKER_IMAGE_DIR": str(tmp_path / folder)})
    printed = capsys.readouterr().out
    result = json.loads(printed)["result"]

    assert code == 0 and data not in printed
    assert "file" not in result and f"{len(data)} base64 characters" in result["not_saved"]


@pytest.mark.parametrize("result", [
    {"kind": "text", "text": "plain notes", "data": "QUJD"},
    {"kind": "image", "data": 7},
    ["kind", "image"],
])
def test_a_result_that_is_not_an_image_is_printed_as_it_is(
    broker_cli, capsys, monkeypatch, tmp_path, result
):
    answers(broker_cli, monkeypatch, result)
    broker_cli.main(PICKED, environ={"BROKER_IMAGE_DIR": str(tmp_path / "seen")})
    assert json.loads(capsys.readouterr().out)["result"] == result
    assert not (tmp_path / "seen").exists()


def test_the_default_folder_is_under_home_where_the_read_tool_may_look(
    broker_cli, capsys, monkeypatch, tmp_path
):
    answers(broker_cli, monkeypatch, {
        "kind": "image", "media_type": "image/webp", "data": base64.b64encode(WEBP).decode()})

    broker_cli.main(PICKED, environ={"HOME": str(tmp_path)})

    saved = json.loads(capsys.readouterr().out)["result"]["file"]
    folder = tmp_path / ".cache" / "broker-images"
    assert os.path.dirname(saved) == str(folder)
    assert os.stat(folder).st_mode & 0o777 == 0o700


def test_a_save_removes_the_saves_of_an_earlier_day_and_nothing_else(
    broker_cli, capsys, monkeypatch, tmp_path
):
    old, fresh, other = (tmp_path / n for n in ("image-old.webp", "image-new.webp", "notes.txt"))
    for path in (old, fresh, other):
        path.write_bytes(b"x")
    long_ago = os.stat(old).st_mtime - broker_cli.IMAGE_KEEP_SECONDS - 60
    os.utime(old, (long_ago, long_ago))
    os.utime(other, (long_ago, long_ago))
    answers(broker_cli, monkeypatch, {
        "kind": "image", "media_type": "image/webp", "data": base64.b64encode(WEBP).decode()})

    broker_cli.main(PICKED, environ={"BROKER_IMAGE_DIR": str(tmp_path)})

    saved = os.path.basename(json.loads(capsys.readouterr().out)["result"]["file"])
    assert sorted(os.listdir(tmp_path)) == sorted(["image-new.webp", "notes.txt", saved])


def test_a_program_that_asks_for_the_base64_gets_it_and_no_file_is_made(
    broker_cli, capsys, monkeypatch, tmp_path
):
    result = {"kind": "image", "media_type": "image/webp",
              "data": base64.b64encode(WEBP).decode()}
    answers(broker_cli, monkeypatch, result)

    broker_cli.main(PICKED, environ={"BROKER_IMAGE_INLINE": "1", "HOME": str(tmp_path)})

    assert json.loads(capsys.readouterr().out)["result"] == result
    assert os.listdir(tmp_path) == []
