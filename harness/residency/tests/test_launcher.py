"""Container-launch contract: argv from config, prompt on stdin, output
parsing, failure/timeout degradation (WP-H9). Uses the real launcher against
the stub script — no podman, no prod."""

import asyncio
import json

from launcher import ContainerLauncher, parse_output
from residency_testlib import make_config


def test_build_argv_is_config_only(tmp_path):
    cfg = make_config(tmp_path, container={
        "command": ["/x/run-resident.sh"], "resident": "gable",
        "session_argv": ["claude", "-p"],
    })
    launcher = ContainerLauncher(cfg.container)
    assert launcher.build_argv() == [
        "/x/run-resident.sh", "gable", "claude", "-p",
    ]


def test_run_records_argv_and_stdin_and_parses(tmp_path, monkeypatch):
    record = tmp_path / "rec.json"
    monkeypatch.setenv("RESIDENCY_STUB_RECORD", str(record))
    cfg = make_config(tmp_path)  # command = [python, stub_launch.py]
    launcher = ContainerLauncher(cfg.container)

    result = asyncio.run(launcher.run("PROMPT-BODY"))

    assert result.ok is True
    assert result.reply == "Hello, this is Gable."
    assert result.action_count == 4          # from num_turns
    assert result.duration_sec >= 0

    rec = json.loads(record.read_text())
    # resident name, then session_argv (empty here) — the prompt is NOT in argv.
    assert rec["argv"] == ["gable"]
    assert rec["stdin"] == "PROMPT-BODY"


def test_nonzero_exit_is_not_ok(tmp_path, monkeypatch):
    monkeypatch.setenv("RESIDENCY_STUB_EXIT", "3")
    monkeypatch.setenv("RESIDENCY_STUB_STDOUT", "")
    cfg = make_config(tmp_path)
    result = asyncio.run(ContainerLauncher(cfg.container).run("x"))
    assert result.ok is False
    assert result.exit_code == 3


def test_timeout_is_not_ok(tmp_path, monkeypatch):
    monkeypatch.setenv("RESIDENCY_STUB_SLEEP", "2")
    cfg = make_config(tmp_path, container={"timeout_sec": 0.2})
    result = asyncio.run(ContainerLauncher(cfg.container).run("x"))
    assert result.ok is False
    assert "timed out" in (result.error or "")


def test_extra_env_reaches_subprocess(tmp_path, monkeypatch):
    record = tmp_path / "rec.json"
    monkeypatch.setenv("RESIDENCY_STUB_RECORD", str(record))
    # The stub echoes an env value into stdout if told to via config env.
    cfg = make_config(tmp_path, container={
        "env": {"RESIDENCY_STUB_STDOUT": json.dumps({"reply": "envd", "actions": 1})},
    })
    result = asyncio.run(ContainerLauncher(cfg.container).run("x"))
    assert result.reply == "envd"
    assert result.action_count == 1


def test_parse_output_variants():
    # (reply, action_count, model) — model absent in these envelopes.
    assert parse_output('{"result": "hi", "num_turns": 2}') == ("hi", 2, None)
    assert parse_output('{"reply": "yo", "action_count": 5}') == ("yo", 5, None)
    assert parse_output("plain text") == ("plain text", None, None)
    assert parse_output("") == ("", None, None)
    # model rides along when the envelope carries it.
    assert parse_output(
        '{"result": "hi", "num_turns": 1, "model": "claude-opus-4-8"}'
    ) == ("hi", 1, "claude-opus-4-8")
