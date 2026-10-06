"""The first summon after the adapter starts says so; later ones do not."""

import asyncio
import os
from datetime import datetime, timezone

from adapter import SummonAdapter
from launcher import SessionResult
from prompt import restart_note
from residency_testlib import FakeArbiter, FakeClient, FakeLauncher, make_config, make_event

CUSTODIAN = 4


def test_the_note_names_the_restart_and_the_last_chat_seen(tmp_path):
    cursor = tmp_path / "cursor.json"
    cursor.write_text("{}")
    os.utime(cursor, (1_790_000_000, 1_790_000_000))
    note = restart_note(cursor, datetime(2026, 10, 6, 3, 0, tzinfo=timezone.utc))
    assert "restarted at 2026-10-06 03:00 UTC" in note
    assert "last saw chat at 2026-09-21" in note


def test_a_fresh_adapter_with_no_saved_state_says_that(tmp_path):
    note = restart_note(tmp_path / "missing.json", datetime(2026, 10, 6, tzinfo=timezone.utc))
    assert "no saved state" in note


def test_only_the_first_summon_after_start_carries_the_note(tmp_path):
    config = make_config(tmp_path)
    client = FakeClient(events=[
        make_event(channel_id=CUSTODIAN, seq=50, msg_id=1, author_name="plink",
                   context={"awake_users": []}, content="@gable one"),
        make_event(channel_id=CUSTODIAN, seq=52, msg_id=2, author_name="plink",
                   context={"awake_users": []}, content="@gable two"),
    ])
    launcher = FakeLauncher(SessionResult(ok=True, reply="ok", action_count=1,
                                          duration_sec=0.1))
    asyncio.run(SummonAdapter(client, config, launcher=launcher,
                              hops=FakeArbiter()).run())
    assert len(launcher.prompts) == 2
    assert "Your summon adapter" in launcher.prompts[0]
    assert "Your summon adapter" not in launcher.prompts[1]
