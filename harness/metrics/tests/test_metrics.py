"""Tests for the WP-H12 metrics producer + daily #custodian line.

Pure file I/O against tmp paths: broker audit log, house_memory retrieval log,
WP-H5 action-log, spine dir. No network (the daily-line transport is stubbed),
no chromadb (retrieval logs are parsed as plain JSON-lines).
"""

from __future__ import annotations

import datetime as _dt
import json
from pathlib import Path

import pytest

import metrics as M

NOW = _dt.datetime(2026, 7, 20, 12, 0, 0, tzinfo=_dt.timezone.utc)
TODAY = "2026-07-20"
YESTERDAY = "2026-07-19"


def _jsonl(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")


def _audit(ts: str, resident: str, verb: str, allowed: bool) -> dict:
    return {"ts": ts, "resident": resident, "verb": verb, "args": {},
            "allowed": allowed, "result_summary": "x"}


@pytest.fixture()
def cfg(tmp_path: Path) -> dict:
    audit = tmp_path / "audit.jsonl"
    metrics_out = tmp_path / "metrics.json"
    _jsonl(audit, [
        _audit(f"{TODAY}T09:00:00Z", "res-claudette", "read-metrics", True),
        _audit(f"{TODAY}T09:05:00Z", "res-claudette", "read-metrics", True),
        _audit(f"{TODAY}T09:06:00Z", "res-claudette", "restart-disjorn", False),
        _audit(f"{YESTERDAY}T09:00:00Z", "res-claudette", "read-own-log", True),
        _audit(f"{TODAY}T10:00:00Z", "res-gable", "classify-diff", True),
        _audit(f"{TODAY}T10:01:00Z", "uid:1234", "read-metrics", False),  # unknown caller
    ])
    return {
        "broker": {"audit_log": str(audit)},
        "paths": {"metrics_json": str(metrics_out)},
        "residents": {
            "res-claudette": {"log_path": "/x"},
            "res-gable": {"log_path": "/y"},
        },
        "budgets": {"res-claudette": {"daily_action_cap": 100}},
        "disjorn": {"custodian_channel_id": 4},
    }


# ------------------------------------------------------------ broker actions

def test_broker_actions_counts_allowed_and_denied(cfg):
    out = M.aggregate_broker_actions(Path(cfg["broker"]["audit_log"]), cfg, now=NOW)
    c = out["res-claudette"]
    assert c["total"] == 4 and c["allowed"] == 3 and c["denied"] == 1
    assert c["by_date"][TODAY] == {"total": 3, "allowed": 2, "denied": 1}
    assert c["by_date"][YESTERDAY] == {"total": 1, "allowed": 1, "denied": 0}
    assert c["by_verb"]["read-metrics"]["allowed"] == 2
    assert c["today"] == {"total": 3, "allowed": 2, "denied": 1}
    # Budget: today's ALLOWED actions (2) against the configured cap.
    assert c["budget"] == {"daily_action_cap": 100, "used_today": 2, "remaining": 98}


def test_quiet_resident_reports_zeros(cfg):
    out = M.aggregate_broker_actions(Path(cfg["broker"]["audit_log"]), cfg, now=NOW)
    # res-gable has activity; a fully-silent resident still appears with zeros.
    cfg["residents"]["res-silent"] = {"log_path": "/z"}
    out = M.aggregate_broker_actions(Path(cfg["broker"]["audit_log"]), cfg, now=NOW)
    assert out["res-silent"]["total"] == 0
    assert out["res-silent"]["budget"]["daily_action_cap"] is None
    # Unknown callers are surfaced too (not silently dropped).
    assert out["uid:1234"]["denied"] == 1


def test_missing_audit_file_is_not_fatal(cfg):
    out = M.aggregate_broker_actions(Path("/no/such/audit.jsonl"), cfg, now=NOW)
    assert out["res-claudette"]["total"] == 0


# --------------------------------------------------------------- retrieval

def test_retrieval_stats_and_window(tmp_path):
    rlog = tmp_path / "retr.jsonl"
    _jsonl(rlog, [
        {"ts": f"{TODAY}T09:00:00+00:00", "resident": "gable", "query": "auth flow",
         "returned_ids": ["m1", "m2"]},
        {"ts": f"{TODAY}T09:10:00+00:00", "resident": "gable", "query": "auth flow",
         "returned_ids": ["m1"]},                       # repeat query, m1 again
        {"ts": "2026-06-01T09:00:00+00:00", "resident": "gable", "query": "old one",
         "returned_ids": ["m9"]},                        # outside 7-day window
        "{ broken json",                                  # tolerated, skipped
    ])
    # last line is not valid json; write it raw
    with open(rlog, "a", encoding="utf-8") as fh:
        fh.write("{ not json\n")
    config = {"residents": {"res-gable": {"retrieval_log": str(rlog)}}}
    out = M.aggregate_retrieval(config, window_days=7, now=NOW)
    g = out["res-gable"]
    assert g["total_recalls"] == 3          # 3 valid records
    assert g["recalls_in_window"] == 2      # the June one is out of window
    assert g["unique_queries"] == 2
    assert g["distinct_returned_ids"] == 3  # m1, m2, m9
    # Reference counts are window-only: m1 twice, m2 once; m9 excluded.
    assert g["top_referenced"] == [["m1", 2], ["m2", 1]]


def test_retrieval_absent_when_unconfigured(tmp_path):
    config = {"residents": {"res-gable": {"log_path": "/y"}}}
    assert M.aggregate_retrieval(config, window_days=7, now=NOW) == {}


# ------------------------------------------------------------------- spine

def test_spine_counts_entries_and_kernel(tmp_path):
    sd = tmp_path / "spine"
    sd.mkdir()
    (sd / "10-core.md").write_text("---\nname: core\nkernel: true\n---\nbody\n")
    (sd / "20-notes.md").write_text("---\nname: notes\nkernel: false\n---\nbody\n")
    (sd / "30-plain.md").write_text("no frontmatter here\n")
    config = {"residents": {"res-gable": {"spine_dir": str(sd)}}}
    out = M.aggregate_spine(config)
    assert out["res-gable"] == {"entries": 3, "kernel_entries": 1}


# ------------------------------------------------------------- tool actions

def test_tool_actions_from_action_log_and_budget(tmp_path):
    alog = tmp_path / ".action-log"
    _jsonl(alog, [
        {"ts": f"{TODAY}T09:00:00Z", "session_id": "s1", "tool_name": "Bash", "ok": True},
        {"ts": f"{TODAY}T09:01:00Z", "session_id": "s1", "tool_name": "Read", "ok": True},
        {"ts": f"{TODAY}T09:02:00Z", "session_id": "s2", "tool_name": "Edit", "ok": False},
        {"ts": f"{YESTERDAY}T09:00:00Z", "session_id": "s0", "tool_name": "Bash", "ok": True},
    ])
    budget = tmp_path / "budget.json"
    budget.write_text(json.dumps({"daily_action_cap": 500, "wall_clock_cap_min": 240}))
    config = {"residents": {"res-gable": {
        "action_log": str(alog), "budget_json": str(budget)}}}
    out = M.aggregate_tool_actions(config, now=NOW)
    g = out["res-gable"]
    assert g["total"] == 4 and g["ok"] == 3 and g["failed"] == 1
    assert g["today"] == 3
    assert g["distinct_sessions"] == 3
    assert g["wp5_budget"] == {"daily_action_cap": 500, "wall_clock_cap_min": 240}


def test_tool_actions_skipped_without_config(tmp_path):
    config = {"residents": {"res-gable": {"log_path": "/y"}}}
    assert M.aggregate_tool_actions(config, now=NOW) == {}


# ------------------------------------------------------ build + atomic write

def test_build_and_write_roundtrip(cfg):
    doc = M.build_metrics(cfg, window_days=7, now=NOW)
    assert set(doc) == {"generated_at", "window_days", "broker_actions",
                        "tool_actions", "retrieval", "spine"}
    path = M.write_metrics(cfg, doc)
    reread = json.loads(Path(path).read_text())
    assert reread["broker_actions"]["by_resident"]["res-claudette"]["allowed"] == 3
    # No stray temp files left behind by the atomic write.
    leftovers = list(Path(path).parent.glob(".metrics-*"))
    assert leftovers == []


def test_write_requires_metrics_json_path():
    with pytest.raises(SystemExit):
        M.write_metrics({"paths": {}}, {"x": 1})


# ---------------------------------------------------------- daily custodian line

def test_compose_daily_line_format(cfg):
    doc = M.build_metrics(cfg, window_days=7, now=NOW)
    line = M.compose_daily_line(doc, cfg, TODAY)
    assert line.startswith(f"[custodian daily {TODAY}] action counts")
    # res-claudette today: 3 verbs, 1 denied, budget 2/100.
    assert "res-claudette: 3 broker verbs (1 denied), budget 2/100" in line
    # res-gable has no cap configured -> no budget clause.
    assert "res-gable: 1 broker verbs (0 denied)" in line
    assert "res-gable:" in line and "budget" not in line.split("res-gable:")[1]


def test_post_daily_line_uses_injected_transport(cfg):
    doc = M.build_metrics(cfg, window_days=7, now=NOW)
    sent = {}

    def stub(disjorn_cfg, body):
        sent["cfg"] = disjorn_cfg
        sent["body"] = body
        return {"seq": 7, "message_id": 77}

    result = M.post_daily_line(cfg, doc, date=TODAY, transport=stub)
    assert result == {"seq": 7, "message_id": 77}
    assert sent["cfg"]["custodian_channel_id"] == 4
    assert sent["body"].startswith(f"[custodian daily {TODAY}]")


# ------------------------------------------------------------------- CLI

def test_cli_build_writes_file(cfg, tmp_path):
    config_toml = tmp_path / "broker.toml"
    _write_toml(config_toml, cfg)
    rc = M.main(["--config", str(config_toml), "build"])
    assert rc == 0
    doc = json.loads(Path(cfg["paths"]["metrics_json"]).read_text())
    assert doc["broker_actions"]["by_resident"]["res-gable"]["allowed"] == 1


def test_cli_post_daily_stubbed(cfg, tmp_path, monkeypatch):
    config_toml = tmp_path / "broker.toml"
    _write_toml(config_toml, cfg)
    posted = {}
    monkeypatch.setattr(M, "_default_transport",
                        lambda: (lambda dc, body: posted.setdefault("body", body) or {"seq": 1}))
    rc = M.main(["--config", str(config_toml), "post-daily", "--date", TODAY])
    assert rc == 0
    assert posted["body"].startswith(f"[custodian daily {TODAY}]")


# --------------------------------------------------------------- toml helper

def _write_toml(path: Path, cfg: dict) -> None:
    """Minimal broker.toml writer for the CLI tests (stdlib has no toml
    writer). Only emits the keys these tests use."""
    b = cfg["broker"]
    lines = [
        "[broker]",
        f'audit_log = "{b["audit_log"]}"',
        "",
        "[paths]",
        f'metrics_json = "{cfg["paths"]["metrics_json"]}"',
        "",
        "[disjorn]",
        f'custodian_channel_id = {cfg["disjorn"]["custodian_channel_id"]}',
        "",
    ]
    for name, rc in cfg["residents"].items():
        lines.append(f"[residents.{name}]")
        for k, v in rc.items():
            lines.append(f'{k} = "{v}"')
        lines.append("")
    for name, bud in cfg.get("budgets", {}).items():
        if isinstance(bud, dict):
            lines.append(f"[budgets.{name}]")
            for k, v in bud.items():
                lines.append(f"{k} = {v}")
            lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")
