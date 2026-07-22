"""BL-G1 pre-act model gate: read the resolved model off the stream's
system/init event and refuse the session BEFORE it answers.

WP-L5's assert is post-hoc — with `--output-format json` the model is only
knowable from the finished envelope, so the reply ships before the drift alert.
`--output-format stream-json --verbose` emits an init event naming the resolved
model before the turn runs (verified locally against CC 2.1.201), which is what
these tests exercise: gate off = today's behaviour to the letter, gate refuse =
session killed with nothing but the operator line in-channel.

Fakes only — the stub launch script stands in for run-resident.sh, no podman,
no prod, no live server.
"""

import asyncio
import json
import logging

import pytest

from adapter import SummonAdapter
from config import (
    MODEL_GATE_ALERT,
    MODEL_GATE_OFF,
    MODEL_GATE_REFUSE,
    AdapterConfig,
)
from launcher import (
    STAGE_INIT,
    STAGE_INIT_NO_MODEL,
    STAGE_MID_SESSION,
    STAGE_NO_INIT,
    ContainerLauncher,
    SessionResult,
    StreamGate,
    parse_event_model,
)
from summary import format_gate_refusal_alert
from residency_testlib import (
    FakeClient,
    FakeLauncher,
    make_config,
    make_event,
    make_stream_events,
)

PIN = "claude-fable-5"
OTHER = "claude-opus-4-8"


def _run(adapter):
    asyncio.run(adapter.run())


def _stream_config(tmp_path, events, *, gate=MODEL_GATE_OFF, model=PIN, **container):
    """Config whose stub session emits ``events`` as a JSON stream."""
    cn = {
        "model_gate": gate,
        "env": {"RESIDENCY_STUB_STREAM": json.dumps(events)},
        "timeout_sec": 30,
    }
    if model is not None:
        cn["model"] = model
    for k, v in container.items():
        if k == "env":
            cn["env"].update(v)
        else:
            cn[k] = v
    return make_config(tmp_path, container=cn)


# ============================================================ 1. CONFIG: the knob


def test_model_gate_defaults_to_off():
    """The shipped default is today's behaviour: no gate."""
    assert AdapterConfig.from_dict({"container": {}}).container.model_gate == MODEL_GATE_OFF


def test_model_gate_accepts_the_three_states():
    for state in (MODEL_GATE_OFF, MODEL_GATE_ALERT, MODEL_GATE_REFUSE):
        cfg = AdapterConfig.from_dict({"container": {"model_gate": state}})
        assert cfg.container.model_gate == state


def test_model_gate_is_case_and_space_tolerant():
    cfg = AdapterConfig.from_dict({"container": {"model_gate": "  Refuse "}})
    assert cfg.container.model_gate == MODEL_GATE_REFUSE


@pytest.mark.parametrize("bad", ["enforce", "on", "", 1, 0, True, False, ["refuse"]])
def test_unparseable_model_gate_falls_back_to_off_loudly(bad, caplog):
    """Safest-compatible default: an unreadable knob can only ever leave the
    summon path behaving as it does today — never brick summons with a stray
    'refuse', never look enforcing while being off. It says so in the log."""
    with caplog.at_level(logging.WARNING, logger="disjorn.residency.config"):
        cfg = AdapterConfig.from_dict({"container": {"model_gate": bad}})
    assert cfg.container.model_gate == MODEL_GATE_OFF
    assert any("model_gate" in r.getMessage() for r in caplog.records)


def test_model_gate_line_is_configurable():
    cfg = AdapterConfig.from_dict({"text": {"model_gate_line": "nope."}})
    assert cfg.text.model_gate_line == "nope."


# ====================================================== 2. STREAM: event reading


def test_init_event_model_is_read():
    """The shape observed from CC 2.1.201: system/init carries `model`."""
    init = make_stream_events(init_model=PIN)[0]
    assert init["type"] == "system" and init["subtype"] == "init"
    assert parse_event_model(init) == PIN


def test_assistant_event_model_is_read():
    ev = {"type": "assistant", "parent_tool_use_id": None,
          "message": {"model": OTHER}}
    assert parse_event_model(ev) == OTHER


def test_subagent_assistant_model_is_ignored():
    """A subagent may legitimately run another model; it is not the session's."""
    ev = {"type": "assistant", "parent_tool_use_id": "toolu_1",
          "message": {"model": OTHER}}
    assert parse_event_model(ev) is None


def test_result_event_is_not_a_model_source_for_the_gate():
    assert parse_event_model({"type": "result", "modelUsage": {OTHER: {}}}) is None


def test_stream_gate_assembles_reply_and_actions_like_the_json_parse():
    gate = StreamGate(PIN, MODEL_GATE_OFF)
    for ev in make_stream_events(init_model=PIN, reply="Hi there.", num_turns=7):
        assert gate.feed_line(json.dumps(ev)) is None
    assert gate.saw_events is True
    assert gate.reply == "Hi there."          # result -> reply
    assert gate.action_count == 7             # num_turns -> action_count
    assert gate.model == PIN


def test_stream_gate_prefers_init_over_model_usage():
    """modelUsage's first key is an auxiliary model in real output; the init
    event states the answering model outright, so it wins."""
    events = make_stream_events(init_model=PIN)
    assert list(events[-1]["modelUsage"])[0] == "claude-haiku-4-5-20251001"
    gate = StreamGate(PIN, MODEL_GATE_OFF)
    for ev in events:
        gate.feed_line(json.dumps(ev))
    assert gate.model == PIN


def test_stream_gate_ignores_non_json_and_typeless_lines():
    gate = StreamGate(PIN, MODEL_GATE_REFUSE)
    assert gate.feed_line("bootstrap: ok") is None
    assert gate.feed_line('{"result": "hi", "num_turns": 2}') is None  # no "type"
    assert gate.saw_events is False


# ============================================ 3. GATE OFF: today's behaviour holds


def test_gate_off_matching_model_runs_normally(tmp_path):
    cfg = _stream_config(tmp_path, make_stream_events(init_model=PIN), gate=MODEL_GATE_OFF)
    result = asyncio.run(ContainerLauncher(cfg.container).run("PROMPT"))
    assert result.ok is True
    assert result.reply == "Hello from Gable."
    assert result.action_count == 4
    assert result.model == PIN
    assert result.gate_abort is False


def test_gate_off_mismatch_still_replies(tmp_path):
    """OFF = the WP-L5 contract exactly: the reply ships, drift is post-hoc."""
    cfg = _stream_config(tmp_path, make_stream_events(init_model=OTHER), gate=MODEL_GATE_OFF)
    result = asyncio.run(ContainerLauncher(cfg.container).run("PROMPT"))
    assert result.ok is True
    assert result.gate_abort is False
    assert result.reply == "Hello from Gable."
    assert result.model == OTHER          # the adapter's post-hoc drift alert fires


def test_gate_off_never_refuses_a_stream_with_no_init(tmp_path):
    """A stream-parse surprise must not brick summons while the gate is off."""
    cfg = _stream_config(
        tmp_path, make_stream_events(init_model=PIN, include_init=False),
        gate=MODEL_GATE_OFF,
    )
    result = asyncio.run(ContainerLauncher(cfg.container).run("PROMPT"))
    assert result.ok is True and result.gate_abort is False
    assert result.reply == "Hello from Gable."


def test_gate_off_legacy_json_output_unchanged(tmp_path):
    """The single-envelope `--output-format json` path is untouched."""
    cfg = make_config(tmp_path, container={"model": PIN, "model_gate": MODEL_GATE_OFF})
    result = asyncio.run(ContainerLauncher(cfg.container).run("PROMPT"))
    assert result.ok is True
    assert result.reply == "Hello, this is Gable."   # stub's canned json envelope
    assert result.action_count == 4
    assert result.gate_abort is False


# ============================================== 4. GATE REFUSE: the pre-act abort


def test_refuse_matching_model_proceeds(tmp_path):
    cfg = _stream_config(tmp_path, make_stream_events(init_model=PIN), gate=MODEL_GATE_REFUSE)
    result = asyncio.run(ContainerLauncher(cfg.container).run("PROMPT"))
    assert result.ok is True and result.gate_abort is False
    assert result.reply == "Hello from Gable."
    assert result.action_count == 4


def test_refuse_mismatch_aborts_before_the_reply_exists(tmp_path):
    """The load-bearing test: the session is killed between the init event and
    the reply, so no reply text is ever produced — not merely withheld."""
    completed = tmp_path / "completed"
    cfg = _stream_config(
        tmp_path, make_stream_events(init_model=OTHER), gate=MODEL_GATE_REFUSE,
        env={
            "RESIDENCY_STUB_LINE_SLEEP": "0.35",
            "RESIDENCY_STUB_COMPLETED": str(completed),
        },
    )
    result = asyncio.run(ContainerLauncher(cfg.container).run("PROMPT"))
    assert result.ok is False
    assert result.gate_abort is True
    assert result.gate_stage == STAGE_INIT
    assert result.gate_expected == PIN
    assert result.gate_actual == OTHER
    assert result.reply == ""                      # nothing to post
    assert PIN in result.error and OTHER in result.error
    assert not completed.exists()                  # killed mid-stream


def test_refuse_never_falls_back_to_the_other_model(tmp_path):
    """No retry, no substitution: one refused session, one refusal."""
    record = tmp_path / "rec.json"
    cfg = _stream_config(
        tmp_path, make_stream_events(init_model=OTHER), gate=MODEL_GATE_REFUSE,
        env={"RESIDENCY_STUB_RECORD": str(record)},
    )
    launcher = ContainerLauncher(cfg.container)
    result = asyncio.run(launcher.run("PROMPT"))
    assert result.gate_abort is True
    # The pin still went into argv verbatim — nothing rewrote it.
    assert launcher.build_argv()[-2:] == ["--model", PIN]


def test_refuse_without_init_event_is_a_refusal_not_a_pass(tmp_path):
    """Documented rule: no init event = the gate never verified anything, which
    is a failure to verify. The message names the fix (stream-json)."""
    cfg = _stream_config(
        tmp_path, make_stream_events(init_model=PIN, include_init=False),
        gate=MODEL_GATE_REFUSE,
    )
    result = asyncio.run(ContainerLauncher(cfg.container).run("PROMPT"))
    assert result.ok is False and result.gate_abort is True
    assert result.gate_stage == STAGE_NO_INIT
    assert "stream-json" in result.error


def test_refuse_with_malformed_init_is_a_refusal(tmp_path):
    """An init event that arrives without a model id proves nothing."""
    cfg = _stream_config(
        tmp_path, make_stream_events(init_model=None, turn_models=[PIN]),
        gate=MODEL_GATE_REFUSE,
    )
    result = asyncio.run(ContainerLauncher(cfg.container).run("PROMPT"))
    assert result.ok is False and result.gate_abort is True
    assert result.gate_stage == STAGE_INIT_NO_MODEL
    assert result.gate_actual is None


def test_refuse_ignores_a_subagent_running_another_model(tmp_path):
    cfg = _stream_config(
        tmp_path,
        make_stream_events(init_model=PIN, subagent_model=OTHER),
        gate=MODEL_GATE_REFUSE,
    )
    result = asyncio.run(ContainerLauncher(cfg.container).run("PROMPT"))
    assert result.ok is True and result.gate_abort is False


def test_refuse_catches_a_mid_session_switch(tmp_path):
    """The sticky-switch shape: init says the pin, a later turn says otherwise.
    Nothing had been posted yet, so this abort is still pre-publication."""
    cfg = _stream_config(
        tmp_path,
        make_stream_events(init_model=PIN, turn_models=[PIN, OTHER]),
        gate=MODEL_GATE_REFUSE,
    )
    result = asyncio.run(ContainerLauncher(cfg.container).run("PROMPT"))
    assert result.ok is False and result.gate_abort is True
    assert result.gate_stage == STAGE_MID_SESSION
    assert result.gate_expected == PIN and result.gate_actual == OTHER


def test_gate_off_reports_a_mid_session_switch_without_refusing(tmp_path):
    cfg = _stream_config(
        tmp_path,
        make_stream_events(init_model=PIN, turn_models=[PIN, OTHER]),
        gate=MODEL_GATE_OFF,
    )
    result = asyncio.run(ContainerLauncher(cfg.container).run("PROMPT"))
    assert result.ok is True and result.gate_abort is False
    assert result.models_seen == [PIN, OTHER]


def test_unpinned_deployment_is_never_gated(tmp_path):
    """No pin, nothing to enforce — even with the knob turned up."""
    cfg = _stream_config(
        tmp_path, make_stream_events(init_model=OTHER), gate=MODEL_GATE_REFUSE,
        model=None,
    )
    result = asyncio.run(ContainerLauncher(cfg.container).run("PROMPT"))
    assert result.ok is True and result.gate_abort is False


# ================================================= 5. GATE ALERT: detect, don't stop


def test_alert_mismatch_detects_early_but_still_replies(tmp_path, caplog):
    with caplog.at_level(logging.ERROR, logger="disjorn.residency.launcher"):
        cfg = _stream_config(
            tmp_path, make_stream_events(init_model=OTHER), gate=MODEL_GATE_ALERT
        )
        result = asyncio.run(ContainerLauncher(cfg.container).run("PROMPT"))
    assert result.ok is True and result.gate_abort is False
    assert result.reply == "Hello from Gable."
    assert result.model == OTHER
    assert any("model gate" in r.getMessage() for r in caplog.records)


def test_alert_without_init_does_not_refuse(tmp_path):
    cfg = _stream_config(
        tmp_path, make_stream_events(init_model=PIN, include_init=False),
        gate=MODEL_GATE_ALERT,
    )
    result = asyncio.run(ContainerLauncher(cfg.container).run("PROMPT"))
    assert result.ok is True and result.gate_abort is False


# ================================================ 6. ADAPTER: what reaches a channel


def _gate_abort_result():
    return SessionResult(
        ok=False, reply="", duration_sec=1.0, error="model gate refused",
        gate_abort=True, gate_stage=STAGE_INIT, gate_expected=PIN,
        gate_actual=OTHER,
    )


def test_gate_abort_posts_only_the_operator_line_in_channel(tmp_path):
    config = make_config(
        tmp_path,
        container={"model": PIN, "model_gate": MODEL_GATE_REFUSE},
        text={"model_gate_line": "I stopped before answering."},
    )
    client = FakeClient(events=[
        make_event(channel_id=7, seq=50, msg_id=1234, author_name="alice",
                   context={"awake_users": []}),
    ])
    _run(SummonAdapter(client, config, launcher=FakeLauncher(_gate_abort_result())))

    posts = client.replies_to(7)
    assert len(posts) == 1
    assert posts[0].content == "I stopped before answering."
    # Nothing the session produced, and no identity suffix vouching for a model.
    assert "·" not in posts[0].content
    assert OTHER not in posts[0].content


def test_gate_abort_alerts_custodian_naming_expected_and_actual(tmp_path):
    config = make_config(
        tmp_path, container={"model": PIN, "model_gate": MODEL_GATE_REFUSE}
    )
    client = FakeClient(events=[
        make_event(channel_id=7, seq=50, author_name="alice",
                   context={"awake_users": []}),
    ])
    _run(SummonAdapter(client, config, launcher=FakeLauncher(_gate_abort_result())))

    custodian = [s.content for s in client.replies_to(4)]
    alert = next(c for c in custodian if "MODEL GATE REFUSED" in c)
    assert PIN in alert and OTHER in alert
    assert "nothing it produced was posted" in alert
    # The normal audit line is still there, marked as a failure.
    assert any(c.startswith("summon | alice") and "| error |" in c for c in custodian)
    # A refusal is NOT drift — the two must not read the same.
    assert not any("MODEL DRIFT" in c for c in custodian)


def test_gate_abort_line_defaults_when_unconfigured(tmp_path):
    config = make_config(
        tmp_path, container={"model": PIN, "model_gate": MODEL_GATE_REFUSE}
    )
    client = FakeClient(events=[
        make_event(channel_id=7, seq=50, author_name="alice",
                   context={"awake_users": []}),
    ])
    _run(SummonAdapter(client, config, launcher=FakeLauncher(_gate_abort_result())))
    assert "pinned model" in client.replies_to(7)[0].content


def test_gate_abort_alert_shape():
    line = format_gate_refusal_alert(
        expected=PIN, actual=None, stage=STAGE_NO_INIT,
        summoner="alice", where="#custodian",
    )
    assert "MODEL GATE REFUSED" in line and PIN in line and "no model id" in line


# ============================================== 7. END TO END through the adapter


def test_refuse_end_to_end_with_a_real_stream_session(tmp_path):
    """Adapter + real launcher + stubbed streaming session: the wrong model is
    caught at init and the channel sees only the operator line."""
    config = _stream_config(
        tmp_path, make_stream_events(init_model=OTHER, reply="Wrong-model answer."),
        gate=MODEL_GATE_REFUSE,
    )
    client = FakeClient(events=[
        make_event(channel_id=7, seq=50, author_name="alice",
                   context={"awake_users": []}),
    ])
    _run(SummonAdapter(client, config))

    assert len(client.replies_to(7)) == 1
    assert "Wrong-model answer." not in client.replies_to(7)[0].content
    assert any("MODEL GATE REFUSED" in s.content for s in client.replies_to(4))


def test_off_end_to_end_with_a_real_stream_session(tmp_path):
    """Same session with the gate off: today's behaviour — the reply ships and
    #custodian gets the post-hoc drift alert."""
    config = _stream_config(
        tmp_path, make_stream_events(init_model=OTHER, reply="Wrong-model answer."),
        gate=MODEL_GATE_OFF,
    )
    client = FakeClient(events=[
        make_event(channel_id=7, seq=50, author_name="alice",
                   context={"awake_users": []}),
    ])
    _run(SummonAdapter(client, config))

    reply = client.replies_to(7)[0].content
    assert reply.startswith("Wrong-model answer.")
    assert f"— gable · {OTHER}" in reply
    assert any("MODEL DRIFT" in s.content for s in client.replies_to(4))
