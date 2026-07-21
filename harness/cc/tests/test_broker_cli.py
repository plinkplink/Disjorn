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
