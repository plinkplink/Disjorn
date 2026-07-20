"""Socket-level integration tests for disjorn-broker (WP-H3).

Everything goes over a real unix socket with real SO_PEERCRED auth; the
current uid is mapped to the fake resident "res-test" in scratch configs.
"""

from __future__ import annotations

import json
import os
import socket
import threading
import time
from pathlib import Path

import pytest

from broker_testlib import ALL_VERBS, BrokerHarness
from brokerd import Broker, load_config


# ---------------------------------------------------------------- kill switches

def test_toggle_off_is_denied_and_audited(harness):
    resp = harness.call("restart-disjorn", {})
    assert resp["ok"] is False
    assert resp["error"]["code"] == "verb-disabled"
    (entry,) = harness.audit_lines()
    assert entry["resident"] == "res-test"
    assert entry["verb"] == "restart-disjorn"
    assert entry["allowed"] is False
    assert "disabled" in entry["result_summary"]
    # and the stub command was never invoked
    assert harness.recorded_argv() == []


def test_toggle_flip_takes_effect_without_restart(harness):
    assert harness.call("read-metrics", {})["error"]["code"] == "verb-disabled"
    harness.set_verbs(**{"read-metrics": True})  # no broker restart
    resp = harness.call("read-metrics", {})
    assert resp["ok"] is True
    assert resp["result"]["metrics"]["retrieval"]["hits"] == 42
    harness.set_verbs()  # flip back off
    assert harness.call("read-metrics", {})["error"]["code"] == "verb-disabled"


def test_missing_verbs_file_fails_closed(harness):
    harness.enable_all()
    Path(harness.verbs_path).unlink()
    resp = harness.call("read-metrics", {})
    assert resp["ok"] is False
    assert resp["error"]["code"] == "internal"
    assert harness.audit_lines()[-1]["allowed"] is False


# ------------------------------------------------------------------- identity

def test_unknown_uid_is_denied_and_audited(tmp_path):
    """A broker whose uid map does NOT contain us must turn us away."""
    audit = tmp_path / "audit2.jsonl"
    verbs = tmp_path / "verbs2.toml"
    verbs.write_text('[res-ghost]\n"read-metrics" = true\n')
    config = {
        "broker": {"socket_path": str(tmp_path / "b2.sock"),
                   "audit_log": str(audit)},
        "uids": {"0": "res-ghost"},  # root only; not the test uid
    }
    broker = Broker(config, str(verbs), transport=lambda c, b: {})
    t = threading.Thread(target=broker.serve_forever, daemon=True)
    t.start()
    deadline = time.time() + 5
    while not os.path.exists(config["broker"]["socket_path"]):
        assert time.time() < deadline
        time.sleep(0.01)
    try:
        with socket.socket(socket.AF_UNIX) as s:
            s.settimeout(10)
            s.connect(config["broker"]["socket_path"])
            s.sendall(b'{"verb": "read-metrics", "args": {}}\n')
            resp = json.loads(s.makefile().readline())
        assert resp["ok"] is False
        assert resp["error"]["code"] == "unknown-caller"
        (entry,) = [json.loads(ln) for ln in audit.read_text().splitlines()]
        assert entry["resident"] == f"uid:{os.getuid()}"
        assert entry["allowed"] is False
    finally:
        broker.shutdown()
        t.join(timeout=5)


# ------------------------------------------------------------ subprocess verbs

def test_restart_disjorn_runs_fixed_argv(harness):
    harness.set_verbs(**{"restart-disjorn": True})
    resp = harness.call("restart-disjorn", {})
    assert resp["ok"] is True
    assert resp["result"]["exit_code"] == 0
    # The configured argv ran exactly once with no caller-controlled extras.
    assert harness.recorded_argv() == [[]]


def test_restart_disjorn_rejects_any_args(harness):
    harness.set_verbs(**{"restart-disjorn": True})
    resp = harness.call("restart-disjorn", {"unit": "sshd"})
    assert resp["error"]["code"] == "bad-args"
    assert harness.recorded_argv() == []
    assert harness.audit_lines()[-1]["allowed"] is False


def test_run_server_tests_returns_summary_line(harness):
    harness.set_verbs(**{"run-server-tests": True})
    resp = harness.call("run-server-tests", {})
    assert resp["ok"] is True
    assert resp["result"]["summary"] == "148 passed in 0.01s"
    assert resp["result"]["exit_code"] == 0


def test_classify_diff_contract_roundtrip(harness):
    harness.set_verbs(**{"classify-diff": True})
    gates = {"tests": "pass", "typecheck": "pass"}
    resp = harness.call("classify-diff", {
        "repo": "/home/plink/somerepo", "range": "main..feature", "gates": gates})
    assert resp["ok"] is True
    cls = resp["result"]["classification"]
    assert cls["tier"] == 1
    assert cls["repo"] == "/home/plink/somerepo"
    assert cls["range"] == "main..feature"
    assert cls["gates"] == gates
    # --config is broker-supplied (protected by placement), never caller data
    assert cls["config"].endswith("protected-paths.toml")


def test_classify_diff_translates_resident_paths(harness, tmp_path):
    """A resident's container path is translated to the mapped host path
    before the classifier runs — residents never speak host layout."""
    harness.set_verbs(**{"classify-diff": True})
    resp = harness.call("classify-diff", {
        "repo": "/opt/disjorn/sub", "range": "main..x", "gates": {}})
    assert resp["ok"] is True
    assert resp["result"]["classification"]["repo"] == str(tmp_path / "mirror" / "sub")


def test_classify_diff_rejects_unmapped_repo(harness):
    """With a path_map configured it doubles as an allowlist."""
    harness.set_verbs(**{"classify-diff": True})
    resp = harness.call("classify-diff", {
        "repo": "/etc/anything", "range": "main..x", "gates": {}})
    assert resp["ok"] is False
    assert resp["error"]["code"] == "bad-args"
    assert "/opt/disjorn" in resp["error"]["message"]


@pytest.mark.parametrize("bad", [
    {"repo": "relative/path", "range": "main", "gates": {}},
    {"repo": "/x/../etc", "range": "main", "gates": {}},
    {"repo": "/x", "range": "-rf", "gates": {}},
    {"repo": "/x", "range": "main; rm -rf /", "gates": {}},
    {"repo": "/x", "range": "main", "gates": "notadict"},
    {"repo": "/x", "range": "main", "gates": {}, "extra": 1},
])
def test_classify_diff_rejects_hostile_args(harness, bad):
    harness.set_verbs(**{"classify-diff": True})
    resp = harness.call("classify-diff", bad)
    assert resp["ok"] is False
    assert resp["error"]["code"] == "bad-args"
    assert harness.audit_lines()[-1]["allowed"] is False


def test_read_prod_logs_line_cap(harness):
    harness.set_verbs(**{"read-prod-logs": True})
    resp = harness.call("read-prod-logs", {"lines": 5})
    assert resp["ok"] is True
    assert len(resp["result"]["lines"]) == 5
    resp = harness.call("read-prod-logs", {"lines": 9999})
    assert resp["error"]["code"] == "bad-args"
    resp = harness.call("read-prod-logs", {"lines": "5; reboot"})
    assert resp["error"]["code"] == "bad-args"


# ----------------------------------------------------------------- read-own-log

def test_read_own_log_tail_and_grep(harness):
    harness.set_verbs(**{"read-own-log": True})
    resp = harness.call("read-own-log", {"lines": 10})
    assert resp["ok"] is True
    assert len(resp["result"]["lines"]) == 10
    assert resp["result"]["lines"][-1].startswith("line 299")
    resp = harness.call("read-own-log", {"lines": 500, "grep": "ERROR"})
    assert resp["ok"] is True
    assert all("ERROR" in ln for ln in resp["result"]["lines"])
    assert len(resp["result"]["lines"]) == 43  # 300 lines, every 7th marked


def test_read_own_log_path_escape_denied(harness):
    """Path confinement: only the caller's configured log file, ever."""
    harness.set_verbs(**{"read-own-log": True})
    own = harness.broker.residents["res-test"]["log_path"]
    for attempt in [
        "/etc/passwd",
        own + "/../../../etc/passwd",
        os.path.dirname(own) + "/../" + os.path.basename(os.path.dirname(own))
        + "/other.log",  # sibling resident's log via ..
        "../res-test.log",
    ]:
        resp = harness.call("read-own-log", {"path": attempt})
        assert resp["ok"] is False, attempt
        assert resp["error"]["code"] == "bad-args"
        assert harness.audit_lines()[-1]["allowed"] is False
    # the same path spelled exactly (or via a redundant ..) still works
    resp = harness.call("read-own-log", {"path": own, "lines": 1})
    assert resp["ok"] is True


# ---------------------------------------------------------------- file-proposal

def test_file_proposal_payload_and_identity(harness):
    harness.set_verbs(**{"file-proposal": True})
    resp = harness.call("file-proposal", {"text": "the fanout cache looks stale"})
    assert resp["ok"] is True
    assert resp["result"]["posted"] is True
    assert resp["result"]["seq"] == 99
    (p,) = harness.proposals
    assert p["body"] == "[proposal from res-test] the fanout cache looks stale"
    assert p["cfg"]["custodian_channel_id"] == 3


def test_file_proposal_caps_length_and_requires_text(harness):
    harness.set_verbs(**{"file-proposal": True})
    assert harness.call("file-proposal", {})["error"]["code"] == "bad-args"
    assert harness.call("file-proposal", {"text": "x" * 4001})["error"]["code"] == "bad-args"
    assert harness.proposals == []


# -------------------------------------------------------------- query-own-audit

def test_query_own_audit_returns_only_callers_lines(harness):
    harness.set_verbs(**{"query-own-audit": True, "read-metrics": True})
    # Seed the audit log with foreign entries the caller must never see.
    for resident in ["res-other", "uid:999"]:
        harness.broker._audit(resident, "read-metrics", {}, True, "seeded")
    harness.call("read-metrics", {})  # a genuine res-test entry
    today = time.strftime("%Y-%m-%d", time.gmtime())
    resp = harness.call("query-own-audit",
                        {"date_from": today, "date_to": today})
    assert resp["ok"] is True
    assert resp["result"]["count"] >= 1
    assert all(e["resident"] == "res-test" for e in resp["result"]["entries"])
    # Out-of-range window returns nothing.
    resp = harness.call("query-own-audit",
                        {"date_from": "2000-01-01", "date_to": "2000-01-02"})
    assert resp["result"]["entries"] == []
    # Bad dates rejected.
    resp = harness.call("query-own-audit",
                        {"date_from": "not-a-date", "date_to": today})
    assert resp["error"]["code"] == "bad-args"


# ------------------------------------------------------------- no restart-self

def test_restart_self_does_not_exist(harness):
    """plink's ruling #3: residents cannot bring themselves back up."""
    assert "restart-self" not in harness.broker.verbs
    assert not any("self" in v for v in harness.broker.verbs)
    harness.enable_all()  # even with every switch on...
    resp = harness.call("restart-self", {})
    assert resp["ok"] is False
    assert resp["error"]["code"] == "unknown-verb"
    entry = harness.audit_lines()[-1]
    assert entry["verb"] == "restart-self"
    assert entry["allowed"] is False


# ------------------------------------------------------------ audit completeness

def test_every_call_writes_exactly_one_audit_line(harness):
    harness.set_verbs(**{"read-metrics": True, "read-own-log": True})
    calls = [
        ("read-metrics", {}),                      # allowed
        ("read-own-log", {"lines": 1}),            # allowed
        ("restart-disjorn", {}),                   # denied: toggled off
        ("no-such-verb", {}),                      # denied: unknown verb
        ("read-own-log", {"path": "/etc/passwd"}), # denied: bad args
        ("read-metrics", {"bogus": 1}),            # denied: bad args
    ]
    for verb, args in calls:
        harness.call(verb, args)
    lines = harness.audit_lines()
    assert len(lines) == len(calls)
    assert [ln["verb"] for ln in lines] == [v for v, _ in calls]
    assert [ln["allowed"] for ln in lines] == [True, True, False, False, False, False]
    for ln in lines:
        assert set(ln) == {"ts", "resident", "verb", "args", "allowed",
                           "result_summary"}


def test_malformed_json_is_denied_and_audited(harness):
    before = len(harness.audit_lines())
    resp = harness.call(None, raw="this is not json")
    assert resp["ok"] is False
    assert resp["error"]["code"] == "bad-args"
    lines = harness.audit_lines()
    assert len(lines) == before + 1
    assert lines[-1]["allowed"] is False


# ------------------------------------------------------------------- templates

TEMPLATE_DIR = Path(__file__).resolve().parent.parent


def test_verbs_template_all_off_and_matches_verb_table(harness):
    import tomllib
    with open(TEMPLATE_DIR / "verbs.toml", "rb") as fh:
        tmpl = tomllib.load(fh)
    assert set(tmpl) == {"res-claudette", "res-gable"}
    for resident, flags in tmpl.items():
        assert set(flags) == set(harness.broker.verbs), resident
        assert all(v is False for v in flags.values()), (
            f"{resident} has a verb enabled in the TEMPLATE — defaults are OFF")
        assert "restart-self" not in flags


def test_broker_template_parses_and_has_required_sections():
    tmpl = load_config(str(TEMPLATE_DIR / "broker.toml"))
    assert "socket_path" in tmpl["broker"]
    assert "audit_log" in tmpl["broker"]
    assert set(tmpl["residents"]) == {"res-claudette", "res-gable"}
    for cmd in ["restart_disjorn", "run_server_tests", "read_prod_logs",
                "classify_diff"]:
        assert isinstance(tmpl["commands"][cmd], list)
        assert all(isinstance(a, str) for a in tmpl["commands"][cmd])
    assert tmpl["commands"]["restart_disjorn"] == [
        "sudo", "-n", "systemctl", "restart", "disjorn"]
    assert "metrics_json" in tmpl["paths"]
    assert {"url", "api_key_path", "custodian_channel_id"} <= set(tmpl["disjorn"])


def test_no_shell_true_anywhere_in_brokerd():
    src = (TEMPLATE_DIR / "brokerd.py").read_text()
    assert "shell=True" not in src
    assert "os.system" not in src
