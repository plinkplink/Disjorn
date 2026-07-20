"""Posting: dry-run prints & posts nothing; real run drives the broker CLI."""

import io
import json
import os
import subprocess
import sys
from pathlib import Path

from consolidation import build_proposals, post_report
from consolidation_testlib import (
    FIXED_NOW,
    add_memory,
    append_log,
    make_config,
    write_spine_entry,
)

FAKE_CLI = str(Path(__file__).resolve().parent / "fake_broker_cli.py")


def _report_with_proposals(store, spine, spine_dir, log, log_path):
    add_memory(store, "hot promotable pattern", mid="m-hot")
    for _ in range(4):
        append_log(log_path, returned_ids=["m-hot"], days_ago=1)
    write_spine_entry(spine_dir, "20-plain.md", "Unreferenced plain fact.", name="plain")
    cfg = make_config(store=store, spine_dir=spine_dir, log_path=log_path, broker_cli=FAKE_CLI)
    report = build_proposals(cfg, now=FIXED_NOW, store=store, spine=spine, log=log)
    assert report.proposals  # at least a promote + an evict
    return cfg, report


def test_dry_run_prints_and_posts_nothing(store, spine, spine_dir, log, log_path):
    cfg, report = _report_with_proposals(store, spine, spine_dir, log, log_path)
    calls = []
    buf = io.StringIO()

    outcome = post_report(report, cfg, dry_run=True, runner=lambda argv: calls.append(argv), out=buf)

    assert outcome.dry_run is True
    assert calls == []  # broker never invoked
    printed = buf.getvalue()
    assert "consolidation run for claudette" in printed
    assert "PROPOSE PROMOTE" in printed
    assert "PROPOSE EVICT" in printed


def test_real_run_invokes_broker_once_per_proposal(store, spine, spine_dir, log, log_path):
    cfg, report = _report_with_proposals(store, spine, spine_dir, log, log_path)
    calls = []

    class _Result:
        returncode = 0
        stdout = ""
        stderr = ""

    def runner(argv):
        calls.append(argv)
        return _Result()

    outcome = post_report(report, cfg, dry_run=False, runner=runner)

    assert outcome.dry_run is False
    assert outcome.posted == len(report.proposals)
    assert outcome.failed == 0
    for argv in calls:
        assert argv[0] == FAKE_CLI
        assert argv[1] == "file-proposal"
        assert argv[2] == "--text"
        assert "consolidation run for claudette" in argv[3]  # batch header on each


def test_real_run_through_subprocess_records_calls(tmp_path, store, spine, spine_dir, log, log_path):
    cfg, report = _report_with_proposals(store, spine, spine_dir, log, log_path)
    record = tmp_path / "broker_calls.jsonl"

    env = dict(os.environ, FAKE_BROKER_RECORD=str(record))
    # wrap the fake CLI so `cfg.broker_cli` is executable via the default runner
    def runner(argv):
        return subprocess.run([sys.executable] + argv, capture_output=True, text=True, env=env)

    outcome = post_report(report, cfg, dry_run=False, runner=runner)
    assert outcome.ok
    lines = record.read_text().strip().splitlines()
    assert len(lines) == len(report.proposals)
    for line in lines:
        rec = json.loads(line)
        assert rec["verb"] == "file-proposal"
        assert "PROPOSE" in rec["text"]


def test_broker_failure_is_reported_not_raised(store, spine, spine_dir, log, log_path):
    cfg, report = _report_with_proposals(store, spine, spine_dir, log, log_path)

    class _Fail:
        returncode = 12
        stdout = ""
        stderr = "verb-disabled"

    outcome = post_report(report, cfg, dry_run=False, runner=lambda argv: _Fail())
    assert outcome.ok is False
    assert outcome.failed == len(report.proposals)
    assert outcome.posted == 0
    assert all("broker exit 12" in e for e in outcome.errors)


def test_runner_exception_is_caught(store, spine, spine_dir, log, log_path):
    cfg, report = _report_with_proposals(store, spine, spine_dir, log, log_path)

    def boom(argv):
        raise FileNotFoundError("no such broker CLI")

    outcome = post_report(report, cfg, dry_run=False, runner=boom)
    assert outcome.ok is False
    assert outcome.failed == len(report.proposals)
    assert any("FileNotFoundError" in e for e in outcome.errors)
