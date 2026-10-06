"""The why-trail on Gable's posts: label rules per tool, pairing by id,
the 50-step cut, the trace on reply and error posts, an older SDK, and the
audit line counting what the chip counts."""

import asyncio
import json
import logging
import re

import httpx
import pytest

import run_summon
from adapter import SummonAdapter
from launcher import ContainerLauncher, StreamGate
from residency_testlib import (
    FakeArbiter,
    FakeClient,
    make_config,
    make_event,
    make_stream_events,
)
from tool_trace import MAX_TRACE_CHARS, TraceRecorder, step_for

PIN = "claude-fable-5"
SID = "7e897653-2a00-4c70-b6d6-41f7f72036af"
MAIN = 7
CUSTODIAN = 4


def tool_use(tool_id, name, tool_input, *, parent=None, timestamp=None):
    event = {
        "type": "assistant",
        "message": {
            "id": f"msg_{tool_id}", "type": "message", "role": "assistant",
            "model": PIN,
            "content": [{"type": "tool_use", "id": tool_id, "name": name,
                         "input": tool_input}],
            "stop_reason": None,
            "usage": {"input_tokens": 3, "output_tokens": 40},
        },
        "parent_tool_use_id": parent,
        "session_id": SID, "uuid": f"u-{tool_id}",
    }
    if timestamp:
        event["timestamp"] = timestamp
    return event


def tool_result(tool_id, content, *, is_error=False, parent=None,
                timestamp=None):
    event = {
        "type": "user",
        "message": {"role": "user", "content": [{
            "tool_use_id": tool_id, "type": "tool_result",
            "content": content, "is_error": is_error}]},
        "parent_tool_use_id": parent,
        "session_id": SID, "uuid": f"r-{tool_id}",
        "tool_use_result": {"stdout": content if isinstance(content, str) else "",
                            "stderr": "", "interrupted": False},
    }
    if timestamp:
        event["timestamp"] = timestamp
    return event


def call(tool_id, name, tool_input, output="done", *, is_error=False):
    return [tool_use(tool_id, name, tool_input),
            tool_result(tool_id, output, is_error=is_error)]


def session(tool_events, *, reply="Here is what I found."):
    base = make_stream_events(init_model=PIN, reply=reply)
    return base[:2] + tool_events + base[2:]


def recorded(events):
    rec = TraceRecorder()
    for event in events:
        rec.observe(event)
    return rec.payload()


# ── label rules, one tool at a time ─────────────────────────────────────

LABELS = [
    ("Bash", {"command": 'cd /home/resident/disjorn && git log --oneline -5 '
                         '| grep -i "hunter2|token"',
              "description": "Search the log for hunter2"},
     "shell", "bash: cd, git, grep"),
    ("Bash", {"command": "API_TOKEN=hunter2 python3 probe.py --secret x; ls"},
     "shell", "bash: python3, ls"),
    ("Bash", {"command": "cat > /tmp/note <<'EOF'\nhunter2 lives here\n"
                         "rm -rf hunter2\nEOF\nwc -l /tmp/note"},
     "shell", "bash: cat, wc"),
    ("Bash", {"command": 'grep "hunter2;rm|x" server/app || echo "hunter2"'},
     "shell", "bash: grep, echo"),
    ("Read", {"file_path": "/home/resident/disjorn/server/app/ws.py",
              "offset": 10, "limit": 40},
     "read", "read server/app/ws.py"),
    ("Read", {"file_path": "/opt/disjorn/harness/residency/launcher.py"},
     "read", "read harness/residency/launcher.py"),
    ("Read", {"file_path": "/home/resident/memory/backup-drill.md"},
     "memory", "read ~/memory/backup-drill"),
    ("Write", {"file_path": "/home/resident/.claude/projects/-home-resident/"
                            "memory/hunter2-notes.md",
               "content": "the password is hunter2"},
     "memory", "write ~/memory/hunter2-notes"),
    ("Read", {"file_path": "/home/resident/logs/gable.log"},
     "read", "read ~/logs/…"),
    ("Edit", {"file_path": "/home/resident/disjorn/server/app/db.py",
              "old_string": "hunter2", "new_string": "hunter3"},
     "write", "edit server/app/db.py"),
    ("Write", {"file_path": "/home/resident/disjorn/notes.txt",
               "content": "hunter2"},
     "write", "write notes.txt"),
    ("MultiEdit", {"file_path": "/home/resident/bots/fable/spine/kernel.md",
                   "edits": [{"old_string": "hunter2", "new_string": "y"}]},
     "write", "multiedit fable/spine/kernel.md"),
    ("Grep", {"pattern": "hunter2", "path": "/home/resident/disjorn/server",
              "output_mode": "content"},
     "search", "grep server"),
    ("Glob", {"pattern": "server/**/*.py", "path": "/home/resident/disjorn"},
     "search", "glob server/**/*.py"),
    ("Agent", {"description": "Map the ws module",
               "prompt": "hunter2: read everything", "subagent_type": "Explore"},
     "agent", "agent Map the ws module"),
    ("Task", {"description": "Check the tests", "prompt": "hunter2"},
     "agent", "agent Check the tests"),
    ("Bash", {"command": "broker changed-files --repo /home/resident/disjorn "
                         "--range main...loop/2026-10-06-why-trail"},
     "broker", "broker changed-files main...loop/2026-10-06-why-trail"),
    ("Bash", {"command": 'broker file-proposal --text "hunter2 is the plan"'},
     "broker", "broker file-proposal"),
    ("Bash", {"command": "broker backlog-file --text \"$(cat <<'EOF'\n"
                         "hunter2 backlog body\nEOF\n)\""},
     "broker", "broker backlog-file"),
    ("Bash", {"command": 'broker board-comment --slug 2026-10-06-why-trail '
                         '--text "hunter2" | jq .ok'},
     "broker", "broker board-comment 2026-10-06-why-trail"),
    ("Bash", {"command": "broker approval-act --seq 3241 --remarks hunter2"},
     "broker", "broker approval-act 3241"),
    ("mcp__disjorn-broker__board_card", {"slug": "2026-10-06-why-trail",
                                         "text": "hunter2"},
     "broker", "broker board-card 2026-10-06-why-trail"),
    ("WebFetch", {"url": "https://example.com/hunter2", "prompt": "hunter2"},
     "web", "WebFetch"),
    ("TodoWrite", {"todos": [{"content": "hunter2", "status": "pending"}]},
     "other", "TodoWrite"),
]


@pytest.mark.parametrize("name,tool_input,kind,label", LABELS)
def test_each_tool_is_labelled_by_its_target_and_nothing_else(
        name, tool_input, kind, label):
    assert step_for(name, tool_input) == (kind, label)
    assert "hunter2" not in label.replace("hunter2-notes", "")


def test_grep_with_no_path_names_the_working_directory_only():
    assert step_for("Grep", {"pattern": "hunter2"}) == ("search", "grep ~")
    assert step_for("Grep", {"pattern": "x", "path": "server"},
                    "/home/resident/disjorn") == ("search", "grep server")


def test_a_label_is_clipped_to_the_server_limit():
    _, label = step_for("Agent", {"description": "x" * 400})
    assert len(label) == 160


def test_no_step_carries_tool_output_or_error_text():
    trace = recorded(
        call("t1", "Bash", {"command": "cat /config/env"},
             "Exit code 1\ncat: hunter2: Permission denied", is_error=True)
        + call("t2", "Read", {"file_path": "/home/resident/disjorn/a.py"},
               "1\thunter2 = 'secret'"))
    assert "hunter2" not in json.dumps(trace)
    assert trace["steps"][0] == {"kind": "shell", "label": "bash: cat",
                                 "outcome": "error", "ms": 0}


# ── pairing ─────────────────────────────────────────────────────────────

def test_results_pair_by_id_and_outcome_comes_from_is_error():
    events = [
        tool_use("a", "Read", {"file_path": "/opt/disjorn/a.py"}),
        tool_use("b", "Read", {"file_path": "/opt/disjorn/b.py"}),
        tool_result("b", "error: this text is not an error flag"),
        tool_result("a", "file missing", is_error=True),
    ]
    steps = recorded(events)["steps"]
    assert [(s["label"], s["outcome"]) for s in steps] == [
        ("read a.py", "error"), ("read b.py", "ok")]
    assert all("reason" not in s for s in steps)


def test_duration_is_the_gap_between_use_and_result_timestamps():
    events = [
        tool_use("a", "Bash", {"command": "pytest -q"},
                 timestamp="2026-10-06T10:00:00.000Z"),
        tool_result("a", "ok", timestamp="2026-10-06T10:00:01.500Z"),
    ]
    assert recorded(events)["steps"][0]["ms"] == 1500


def test_without_timestamps_the_gate_uses_line_arrival_and_otherwise_zero():
    gate = StreamGate(pin=None)
    gate.feed_line(json.dumps(tool_use("a", "Bash", {"command": "ls"})), 10.0)
    gate.feed_line(json.dumps(tool_result("a", "x")), 10.25)
    assert gate.trace.payload()["steps"][0]["ms"] == 250
    assert recorded(call("b", "Bash", {"command": "ls"}))["steps"][0]["ms"] == 0


def test_a_call_with_no_result_is_an_error():
    steps = recorded([tool_use("a", "Bash", {"command": "sleep 900"})])["steps"]
    assert steps[0]["outcome"] == "error"


def test_a_subagent_is_one_row_and_its_own_calls_are_not_counted():
    events = [
        tool_use("ag", "Agent", {"description": "Survey", "prompt": "p"}),
        tool_use("in1", "Read", {"file_path": "/opt/disjorn/x.py"}, parent="ag"),
        tool_result("in1", "x", parent="ag"),
        tool_use("in2", "Grep", {"pattern": "y"}, parent="ag"),
        tool_result("in2", "y", parent="ag"),
        tool_result("ag", [{"type": "text", "text": "summary"}]),
    ]
    trace = recorded(events)
    assert trace["total"] == 1
    assert trace["steps"] == [{"kind": "agent", "label": "agent Survey",
                               "outcome": "ok", "ms": 0}]


def test_a_repeated_tool_use_line_is_one_step():
    use = tool_use("a", "Bash", {"command": "ls"})
    assert recorded([use, use, tool_result("a", "x")])["total"] == 1


@pytest.mark.parametrize("output,outcome,reason", [
    ('Exit code 12\n{"ok": false, "error": {"code": "verb-disabled"}}',
     "refused", "refused-by-broker"),
    ('Exit code 20\n{"ok": false, "error": {"code": "broker-disabled-local"}}',
     "refused", "refused-by-broker"),
    ('Exit code 14\n{"ok": false, "error": {"code": "exec-failure"}}',
     "error", None),
])
def test_a_broker_refusal_maps_to_the_fixed_reason(output, outcome, reason):
    step = recorded(call("a", "Bash", {"command": "broker restart-disjorn"},
                         output, is_error=True))["steps"][0]
    assert step["outcome"] == outcome
    assert step.get("reason") == reason


# ── the cut ─────────────────────────────────────────────────────────────

def test_over_fifty_steps_keeps_the_first_fifty_and_the_true_total():
    events = []
    for i in range(60):
        events += call(f"t{i}", "Read", {"file_path": f"/opt/disjorn/f{i}.py"})
    trace = recorded(events)
    assert trace["total"] == 60
    assert [s["label"] for s in trace["steps"]] == [
        f"read f{i}.py" for i in range(50)]


def test_long_labels_cut_further_to_fit_the_server_budget():
    events = []
    for i in range(50):
        events += call(f"t{i}", "Agent", {"description": "é" * 200})
    trace = recorded(events)
    assert trace["total"] == 50
    assert 0 < len(trace["steps"]) < 50
    sized = [{**s, "reason": None} for s in trace["steps"]]
    assert len(json.dumps({"steps": sized, "total": 50})) <= MAX_TRACE_CHARS


# ── end to end: the posts ───────────────────────────────────────────────

def _summon(tmp_path, events, *, client=None, **container):
    env = {"RESIDENCY_STUB_STREAM": json.dumps(events)}
    env.update(container.pop("env", {}))
    config = make_config(tmp_path, container={"model": PIN, "env": env,
                                              **container})
    client = client or FakeClient()
    client._events = [make_event(channel_id=MAIN, seq=50, author_name="alice",
                                 context={"awake_users": []})]
    asyncio.run(SummonAdapter(client, config, hops=FakeArbiter()).run())
    return client.replies_to(MAIN)[0], client.replies_to(CUSTODIAN)[-1].content


THREE_CALLS = (
    call("a", "Read", {"file_path": "/home/resident/disjorn/server/app/ws.py"})
    + call("b", "Bash", {"command": "git status --short"})
    + call("c", "Grep", {"pattern": "hunter2", "path": "/opt/disjorn/server"},
           "no matches", is_error=True)
)


def test_the_reply_post_carries_the_trace(tmp_path):
    posted, _ = _summon(tmp_path, session(THREE_CALLS))
    assert posted.content == "Here is what I found."
    trace = posted.kwargs["trace"]
    assert trace["total"] == 3
    assert [(s["kind"], s["label"], s["outcome"]) for s in trace["steps"]] == [
        ("read", "read server/app/ws.py", "ok"),
        ("shell", "bash: git", "ok"),
        ("search", "grep server", "error"),
    ]


def test_the_error_post_carries_the_trace_too(tmp_path):
    posted, audit = _summon(tmp_path, session(THREE_CALLS),
                            env={"RESIDENCY_STUB_EXIT": "1"})
    assert posted.content == "something broke on my end."
    assert posted.kwargs["trace"]["total"] == 3
    assert " | error | " in audit


def test_a_refusal_post_carries_no_trace(tmp_path):
    config = make_config(tmp_path, budget={"daily_session_cap": 0})
    client = FakeClient(events=[make_event(channel_id=MAIN, seq=50,
                                           context={"awake_users": []})])
    asyncio.run(SummonAdapter(client, config).run())
    assert "trace" not in client.replies_to(MAIN)[0].kwargs


def test_a_session_with_no_tool_calls_sends_no_trace(tmp_path):
    posted, audit = _summon(tmp_path, session([]))
    assert "trace" not in posted.kwargs
    assert "| 0 actions |" in audit


@pytest.mark.parametrize("calls", [3, 60])
def test_the_chip_count_equals_the_audit_line_action_count(tmp_path, calls):
    events = []
    for i in range(calls):
        events += call(f"t{i}", "Bash", {"command": "ls"})
    posted, audit = _summon(tmp_path, session(events))
    counted = int(re.search(r"\| (\d+) actions \|", audit).group(1))
    assert counted == posted.kwargs["trace"]["total"] == calls


def test_the_timeout_path_keeps_the_steps_it_saw(tmp_path):
    events = session(THREE_CALLS)
    config = make_config(tmp_path, container={
        "timeout_sec": 0.6,
        "env": {"RESIDENCY_STUB_STREAM": json.dumps(events + events),
                "RESIDENCY_STUB_LINE_SLEEP": "0.05"}})
    result = asyncio.run(ContainerLauncher(config.container).run("x"))
    assert result.timed_out
    assert result.trace["total"] >= 1


# ── an older SDK ────────────────────────────────────────────────────────

class OlderSdkClient(FakeClient):
    async def send(self, channel_id, content, *, reply_to=None,
                   attribution=None):
        return await super().send(channel_id, content, reply_to=reply_to,
                                  attribution=attribution)


def test_an_sdk_without_trace_still_posts_the_reply_without_it(tmp_path, caplog):
    with caplog.at_level(logging.WARNING, logger="disjorn.residency"):
        posted, audit = _summon(tmp_path, session(THREE_CALLS),
                                client=OlderSdkClient())
    assert posted.content == "Here is what I found."
    assert "trace" not in posted.kwargs
    assert posted.kwargs["attribution"]["model"] == PIN
    assert "| 3 actions |" in audit
    assert any("takes no trace" in r.getMessage() for r in caplog.records)


def test_the_daemon_starts_on_an_sdk_without_trace():
    assert run_summon.sdk_refusal(OlderSdkClient) is None


class StricterServerClient(FakeClient):
    async def send(self, channel_id, content, *, reply_to=None, **kw):
        if "trace" in kw:
            request = httpx.Request("POST", "http://disjorn.test/m")
            raise httpx.HTTPStatusError(
                "422", request=request,
                response=httpx.Response(422, request=request))
        return await super().send(channel_id, content, reply_to=reply_to, **kw)


def test_a_trace_the_server_refuses_never_costs_the_reply(tmp_path):
    posted, audit = _summon(tmp_path, session(THREE_CALLS),
                            client=StricterServerClient())
    assert posted.content == "Here is what I found."
    assert "trace" not in posted.kwargs
    assert "posted #101" in audit
