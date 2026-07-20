"""End-to-end adapter behaviour with fakes (WP-H9): reply posting, summary
line, typing keepalive, budget enforcement + persistence, reconnect-from-seq
handoff, cursor persistence, graceful error path."""

import asyncio
import json

from adapter import SummonAdapter
from launcher import SessionResult
from residency_testlib import (
    FakeClient,
    FakeLauncher,
    make_config,
    make_event,
    make_ready,
)


def _run(adapter):
    asyncio.run(adapter.run())


def test_summon_posts_reply_and_summary(tmp_path):
    config = make_config(tmp_path)
    client = FakeClient(events=[
        make_ready(),
        make_event(channel_id=7, seq=50, msg_id=1234, author_name="alice",
                   context={"awake_users": []}),
    ])
    launcher = FakeLauncher(SessionResult(
        ok=True, reply="Hello from Gable.", action_count=3, duration_sec=2.0,
    ))
    _run(SummonAdapter(client, config, launcher=launcher))

    # reply posted to the summoning channel, as a reply to the trigger
    replies = client.replies_to(7)
    assert len(replies) == 1
    assert replies[0].content == "Hello from Gable."
    assert replies[0].reply_to == 1234

    # one-line summary to #custodian (channel 4)
    custodian = client.replies_to(4)
    assert len(custodian) == 1
    line = custodian[0].content
    assert line.startswith("summon | alice in channel 7 | ok |")
    assert "3 actions" in line

    # budget spent once, persisted
    assert json.loads((tmp_path / "budget.json").read_text())["count"] == 1


def test_non_summon_message_is_ignored(tmp_path):
    config = make_config(tmp_path)
    client = FakeClient(events=[
        make_event(channel_id=7, seq=50, context=None, content="idle chatter"),
    ])
    launcher = FakeLauncher()
    _run(SummonAdapter(client, config, launcher=launcher))
    assert launcher.prompts == []
    assert client.sent == []


def test_typing_keepalive_runs_during_session(tmp_path):
    config = make_config(tmp_path, summon={"typing_interval_sec": 0.01})
    client = FakeClient(events=[
        make_event(channel_id=7, context={"awake_users": []}),
    ])
    launcher = FakeLauncher(delay=0.06)  # session busy long enough to re-ping
    _run(SummonAdapter(client, config, launcher=launcher))
    # immediate ping + at least one more during the delay
    assert len(client.typing_calls) >= 2
    assert set(client.typing_calls) == {7}


def test_typing_failure_does_not_crash(tmp_path):
    config = make_config(tmp_path, summon={"typing_interval_sec": 0.01})
    client = FakeClient(events=[
        make_event(channel_id=7, context={"awake_users": []}),
    ])
    client.typing_fails = True  # e.g. no live WS
    launcher = FakeLauncher(delay=0.03)
    _run(SummonAdapter(client, config, launcher=launcher))
    # reply still posted despite typing errors
    assert len(client.replies_to(7)) == 1


def test_budget_exhaustion_refuses_politely(tmp_path):
    config = make_config(tmp_path, budget={"daily_session_cap": 1})
    client = FakeClient(events=[
        make_event(channel_id=7, seq=50, author_name="alice",
                   context={"awake_users": []}),
        make_event(channel_id=7, seq=51, author_name="bob",
                   context={"awake_users": []}),
    ])
    launcher = FakeLauncher()
    _run(SummonAdapter(client, config, launcher=launcher))

    # only the first summon ran a session
    assert len(launcher.prompts) == 1
    replies = client.replies_to(7)
    # first: real reply; second: refusal line
    assert replies[0].content == "Hello from Gable."
    assert replies[1].content == "budget reached, ask a human."
    # #custodian sees a served summary then a refusal summary
    custodian = [s.content for s in client.replies_to(4)]
    assert any(c.startswith("summon | alice") for c in custodian)
    assert any("summon refused | bob" in c for c in custodian)


def test_budget_persists_across_restart(tmp_path):
    # Pre-load the budget file at the cap: a fresh daemon must still refuse.
    (tmp_path / "budget.json").write_text(
        json.dumps({"date": __import__("datetime").date.today().isoformat(),
                    "count": 1})
    )
    config = make_config(tmp_path, budget={"daily_session_cap": 1})
    client = FakeClient(events=[
        make_event(channel_id=7, context={"awake_users": []}),
    ])
    launcher = FakeLauncher()
    _run(SummonAdapter(client, config, launcher=launcher))
    assert launcher.prompts == []  # over budget from the persisted counter
    assert client.replies_to(7)[0].content == "budget reached, ask a human."


def test_reconnect_from_seq_handoff_reseeds_on_boot(tmp_path):
    # A cursor persisted by a previous run must be re-seeded so the SDK's first
    # reconnect backfills exactly the gap.
    (tmp_path / "cursor.json").write_text(json.dumps({"7": 55, "4": 10}))
    config = make_config(tmp_path)
    client = FakeClient(events=[make_ready(reconnected=True)])
    _run(SummonAdapter(client, config))
    assert set(client.seeded) == {(7, 55), (4, 10)}
    assert client.last_seen_seq == {7: 55, 4: 10}


def test_cursor_persisted_after_handling(tmp_path):
    config = make_config(tmp_path)
    client = FakeClient(events=[
        make_event(channel_id=7, seq=60, context=None, content="chatter"),
    ])
    _run(SummonAdapter(client, config))
    saved = json.loads((tmp_path / "cursor.json").read_text())
    assert saved == {"7": 60}


def test_session_error_falls_back_to_error_line(tmp_path):
    config = make_config(tmp_path)
    client = FakeClient(events=[
        make_event(channel_id=7, context={"awake_users": []}),
    ])
    launcher = FakeLauncher(SessionResult(ok=False, error="boom", duration_sec=0.1))
    _run(SummonAdapter(client, config, launcher=launcher))
    assert client.replies_to(7)[0].content == "something broke on my end."
    # summary still posted, marked error
    assert any("| error |" in s.content for s in client.replies_to(4))
