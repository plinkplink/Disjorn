"""WP-L5 model integrity: pin -> argv, assert actual vs pin, visible suffix,
audit line, drift alert. No fallback anywhere — a pinned session that can't run
the pin fails loud.

Covers the five model-integrity items end to end with fakes + the real bash
forwarding path (no podman, no prod)."""

import asyncio
import json
import shutil

import pytest

from adapter import SummonAdapter
from config import AdapterConfig
from launcher import ContainerLauncher, SessionResult, parse_model, parse_output
from summary import format_drift_alert, format_reply_suffix, format_summary
from residency_testlib import FakeClient, FakeLauncher, make_config, make_event

PIN = "claude-opus-4-8"


def _run(adapter):
    asyncio.run(adapter.run())


# ------------------------------------------------------------------ 1. PIN: parse


def test_model_pin_parses_when_present():
    cfg = AdapterConfig.from_dict({"container": {"model": PIN}})
    assert cfg.container.model == PIN


def test_model_pin_absent_is_none():
    cfg = AdapterConfig.from_dict({"container": {}})
    assert cfg.container.model is None


def test_model_pin_whitespace_stripped():
    cfg = AdapterConfig.from_dict({"container": {"model": "  claude-opus-4-8 "}})
    assert cfg.container.model == PIN


def test_model_pin_empty_string_fails_loud():
    with pytest.raises(ValueError):
        AdapterConfig.from_dict({"container": {"model": ""}})


def test_model_pin_blank_string_fails_loud():
    with pytest.raises(ValueError):
        AdapterConfig.from_dict({"container": {"model": "   "}})


def test_model_pin_non_string_fails_loud():
    with pytest.raises(ValueError):
        AdapterConfig.from_dict({"container": {"model": 42}})


# ------------------------------------------------------------ 1. PIN: into argv


def test_build_argv_appends_model_flag_when_pinned(tmp_path):
    cfg = make_config(tmp_path, container={
        "command": ["/x/run-resident.sh"], "resident": "gable",
        "session_argv": ["claude", "-p"], "model": PIN,
    })
    assert ContainerLauncher(cfg.container).build_argv() == [
        "/x/run-resident.sh", "gable", "claude", "-p", "--model", PIN,
    ]


def test_build_argv_omits_model_flag_when_unpinned(tmp_path):
    cfg = make_config(tmp_path, container={
        "command": ["/x/run-resident.sh"], "resident": "gable",
        "session_argv": ["claude", "-p"],
    })
    argv = ContainerLauncher(cfg.container).build_argv()
    assert "--model" not in argv


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash required")
def test_pinned_model_reaches_inner_command_via_wrapper(tmp_path):
    """The prod-shaped bash wrapper (`... exec CMD "$@"` + argv0 placeholder)
    forwards the appended `--model <id>` to the inner command — the load-bearing
    template contract from summon.toml.template. Uses `printf` in place of
    claude and captures its argv."""
    bash = shutil.which("bash")
    # Mirror prod: the launch wrapper drops the resident name (run-resident.sh:
    # `NAME=$1; shift`) then runs the rest, which is the bash -lc session_argv
    # with the "$@"-forwarding script + argv0 placeholder, plus the appended
    # `--model <id>`. `printf` stands in for claude and echoes its argv.
    cfg = make_config(tmp_path, container={
        "command": [bash, "-c", 'shift; exec "$@"', "wrapper"],
        "resident": "gable",
        "session_argv": [
            "bash", "-lc",
            'exec printf "GOT:%s\\n" "$@"', "cc-session",
        ],
        "model": PIN,
    })
    result = asyncio.run(ContainerLauncher(cfg.container).run(""))
    assert result.ok, result.error
    assert "GOT:--model" in result.reply
    assert f"GOT:{PIN}" in result.reply


# ------------------------------------------------- 2. ASSERT: parse actual model


def test_parse_model_explicit_field():
    assert parse_model({"model": PIN}) == PIN


def test_parse_model_from_model_usage_single_key():
    assert parse_model({"modelUsage": {PIN: {"cost_usd": 0.01}}}) == PIN


def test_parse_model_from_model_usage_first_key_when_multiple():
    # Subagent used another model too; primary (first) key is taken and the
    # adapter's membership check still catches a pin mismatch.
    got = parse_model({"modelUsage": {PIN: {}, "claude-haiku-x": {}}})
    assert got == PIN


def test_parse_model_absent_is_none():
    assert parse_model({"result": "hi", "num_turns": 1}) is None


def test_parse_output_carries_model_from_usage():
    reply, actions, model = parse_output(
        json.dumps({"result": "hi", "num_turns": 2,
                    "modelUsage": {PIN: {"cost_usd": 0.02}}})
    )
    assert (reply, actions, model) == ("hi", 2, PIN)


# --------------------------------------------- 3/4. VISIBLE suffix + AUDIT line


def test_reply_carries_model_suffix_and_summary_records_model(tmp_path):
    config = make_config(tmp_path, container={"model": PIN})
    client = FakeClient(events=[
        make_event(channel_id=7, seq=50, msg_id=1234, author_name="alice",
                   context={"awake_users": []}),
    ])
    launcher = FakeLauncher(SessionResult(
        ok=True, reply="Hello from Gable.", action_count=3, duration_sec=2.0,
        model=PIN,
    ))
    _run(SummonAdapter(client, config, launcher=launcher))

    reply = client.replies_to(7)[0].content
    assert reply.startswith("Hello from Gable.")
    assert f"— gable · {PIN}" in reply          # VISIBLE identity suffix

    line = client.replies_to(4)[0].content
    assert line.endswith(f"| {PIN}")            # AUDIT line carries the model
    assert "MODEL DRIFT" not in line


def test_unpinned_deployment_keeps_bare_reply_and_summary(tmp_path):
    """Backward compat: no pin -> no suffix, no model on the audit line."""
    config = make_config(tmp_path)  # no container.model
    client = FakeClient(events=[
        make_event(channel_id=7, seq=50, author_name="alice",
                   context={"awake_users": []}),
    ])
    launcher = FakeLauncher(SessionResult(
        ok=True, reply="Hello from Gable.", action_count=3, duration_sec=2.0,
    ))
    _run(SummonAdapter(client, config, launcher=launcher))
    assert client.replies_to(7)[0].content == "Hello from Gable."
    assert "·" not in client.replies_to(4)[0].content


def test_suffix_shows_pin_when_actual_unknown(tmp_path):
    """Output carried no model id: assert at the strongest available level (the
    pin) and show the pin — never fabricate an 'actual'."""
    config = make_config(tmp_path, container={"model": PIN})
    client = FakeClient(events=[
        make_event(channel_id=7, seq=50, author_name="alice",
                   context={"awake_users": []}),
    ])
    launcher = FakeLauncher(SessionResult(
        ok=True, reply="Hi.", action_count=1, duration_sec=1.0, model=None,
    ))
    _run(SummonAdapter(client, config, launcher=launcher))
    assert f"— gable · {PIN}" in client.replies_to(7)[0].content
    custodian = [s.content for s in client.replies_to(4)]
    assert any(c.endswith(f"| {PIN}") for c in custodian)
    assert not any("MODEL DRIFT" in c for c in custodian)  # unknown != drift


# ----------------------------------------------------------- 5. DRIFT ALERT


def test_drift_posts_alert_and_still_replies(tmp_path):
    config = make_config(tmp_path, container={"model": PIN})
    client = FakeClient(events=[
        make_event(channel_id=7, seq=50, msg_id=1234, author_name="alice",
                   context={"awake_users": []}),
    ])
    launcher = FakeLauncher(SessionResult(
        ok=True, reply="Hello from Gable.", action_count=3, duration_sec=2.0,
        model="claude-sonnet-9-wrong",
    ))
    _run(SummonAdapter(client, config, launcher=launcher))

    # The reply STILL goes out (alert, don't swallow the reply)...
    reply = client.replies_to(7)[0].content
    assert reply.startswith("Hello from Gable.")
    # ...and shows what's actually running, not the pin.
    assert "claude-sonnet-9-wrong" in reply

    # A loud #custodian alert names expected vs actual.
    custodian = [s.content for s in client.replies_to(4)]
    alert = next(c for c in custodian if "MODEL DRIFT" in c)
    assert PIN in alert and "claude-sonnet-9-wrong" in alert
    # The normal audit summary is still posted too.
    assert any(c.startswith("summon | alice") for c in custodian)


def test_no_drift_alert_when_actual_matches_pin(tmp_path):
    config = make_config(tmp_path, container={"model": PIN})
    client = FakeClient(events=[
        make_event(channel_id=7, seq=50, author_name="alice",
                   context={"awake_users": []}),
    ])
    launcher = FakeLauncher(SessionResult(
        ok=True, reply="ok", action_count=1, duration_sec=1.0, model=PIN,
    ))
    _run(SummonAdapter(client, config, launcher=launcher))
    assert not any("MODEL DRIFT" in s.content for s in client.replies_to(4))


# ------------------------------------------------------------- format helpers


def test_format_helpers_shape():
    assert format_reply_suffix("gable", PIN) == f"— gable · {PIN}"
    assert format_summary(
        summoner="alice", where="channel 7", action_count=3,
        duration_sec=2.0, ok=True, model=PIN,
    ).endswith(f"| {PIN}")
    alert = format_drift_alert(
        expected=PIN, actual="other", summoner="alice", where="#custodian",
    )
    assert "MODEL DRIFT" in alert and PIN in alert and "other" in alert
