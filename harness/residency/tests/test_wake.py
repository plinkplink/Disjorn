"""The wake lane (SPECS/2026-08-25-agentic-residents.md).

What these tests hold down, in the spec's own terms:

* the wrapper posts, the session does not — every field of a result post is
  something this process observed (exit status, clock, action-log delta,
  gatehouse refs), and a failed session's own words never ship;
* silence is the defect — cap-kill, crash, model-gate refusal and a wake that
  arrived while the runner was down all end in a #custodian post;
* a `wip:` head is detected from the branch, not from anything the session
  said about itself;
* the cap rides on the RECORD (plink's broker config), and the woken session's
  launch contract is otherwise the summon's, unchanged;
* two lines per wake in the house action log, tagged with the wake id.

Sync tests driving the coroutines with `asyncio.run`, like the rest of this
suite: `async def` test functions need this package's own pytest.ini
(`asyncio_mode = auto`), which is not the ini pytest picks when the whole
harness is collected at once — they pass alone and error in the full run.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from launcher import SessionResult
from residency_testlib import FakeClient, FakeLauncher, make_config
from wake import (
    GatehouseWatch,
    WakeAccounting,
    WakeRecord,
    WakeRunner,
    WakeSpool,
)

CUSTODIAN = 4
# Every fixture record is requested at this instant; the clocks below are
# offsets from it, so "inside the window" and "past it" are readable.
REQUESTED_AT = "2026-08-25T12:00:00+00:00"
REQUESTED_EPOCH = 1787659200.0        # datetime.fromisoformat(REQUESTED_AT)


# --------------------------------------------------------------------- builders


def write_record(spool: Path, wake_id: str, *, task: str = "read the digest",
                 resident: str = "res-gable", woken_by: str = "plink",
                 requested_at: str = REQUESTED_AT,
                 cap: int = 120, grace: int = 30, **extra) -> Path:
    """One wake record, exactly as brokerd._write_wake_record writes it."""
    body = {"schema": 1, "wake_id": wake_id, "resident": resident,
            "woken_by": woken_by, "requested_at": requested_at,
            "session_cap_sec": cap, "grace_sec": grace, "task": task}
    body.update(extra)
    path = spool / f"{wake_id}.wake.json"
    path.write_text(json.dumps(body), encoding="utf-8")
    return path


def wake_config(tmp_path: Path, **wake_overrides):
    spool = tmp_path / "spool"
    spool.mkdir(exist_ok=True)
    wake = {"spool_dir": str(spool),
            "state_path": str(tmp_path / "served.json"),
            "action_log": str(tmp_path / "action-log"),
            "poll_interval_sec": 0.01}
    wake.update(wake_overrides)
    return make_config(tmp_path, wake=wake)


def runner_for(tmp_path: Path, *, launcher=None, watch=None, **wake_overrides):
    config = wake_config(tmp_path, **wake_overrides)
    client = FakeClient()
    runner = WakeRunner(
        client, config,
        watch=watch if watch is not None else GatehouseWatch(None),
        launcher_factory=(lambda cap: launcher) if launcher else None)
    return runner, client, config


def posts(client: FakeClient) -> list[str]:
    return [s.content for s in client.sent if s.channel_id == CUSTODIAN]


def action_lines(config) -> list[dict]:
    path = Path(config.wake.action_log)
    if not path.exists():
        return []
    return [json.loads(ln) for ln in path.read_text().splitlines() if ln.strip()]


# ------------------------------------------------------------------- the spool


def test_a_due_wake_is_due_and_a_stale_one_is_missed(tmp_path):
    """The window is cap + grace from the request. Inside it a human is still
    waiting; outside it, starting the session would be worse than saying nobody
    was home."""
    spool_dir = tmp_path / "spool"
    spool_dir.mkdir()
    write_record(spool_dir, "wake-20260825T120000Z-aaaaaa",
                 requested_at="2026-08-25T12:00:00+00:00", cap=120, grace=30)
    spool = WakeSpool(str(spool_dir), str(tmp_path / "served.json"))

    at_100s = REQUESTED_EPOCH + 100
    due, missed = spool.poll("res-gable", at_100s)
    assert [r.wake_id for r in due] == ["wake-20260825T120000Z-aaaaaa"]
    assert missed == []

    due, missed = spool.poll("res-gable", at_100s + 3600)
    assert due == []
    assert [r.wake_id for r in missed] == ["wake-20260825T120000Z-aaaaaa"]


def test_a_served_wake_is_never_served_twice(tmp_path):
    """Marked served BEFORE the session runs: a crash mid-session must not
    leave a wake the next poll starts over."""
    spool_dir = tmp_path / "spool"
    spool_dir.mkdir()
    write_record(spool_dir, "wake-20260825T120000Z-bbbbbb")
    state = tmp_path / "served.json"
    spool = WakeSpool(str(spool_dir), str(state))
    now = REQUESTED_EPOCH + 100
    assert spool.poll("res-gable", now)[0]
    spool.mark_served("wake-20260825T120000Z-bbbbbb")
    assert spool.poll("res-gable", now)[0] == []
    # And across a restart, from disk.
    assert WakeSpool(str(spool_dir), str(state)).poll("res-gable", now)[0] == []


def test_another_seats_wake_is_not_this_seats_work(tmp_path):
    spool_dir = tmp_path / "spool"
    spool_dir.mkdir()
    write_record(spool_dir, "wake-20260825T120000Z-cccccc",
                 resident="res-claudette")
    spool = WakeSpool(str(spool_dir), str(tmp_path / "served.json"))
    assert spool.poll("res-gable", REQUESTED_EPOCH + 100) == ([], [])


def test_a_malformed_record_is_skipped_not_fatal(tmp_path):
    """A record with no cap is a record that cannot say how long to let a
    session run. Skip it and keep serving the good one."""
    spool_dir = tmp_path / "spool"
    spool_dir.mkdir()
    (spool_dir / "wake-20260825T120000Z-dddddd.wake.json").write_text(
        json.dumps({"wake_id": "wake-20260825T120000Z-dddddd",
                    "resident": "res-gable", "woken_by": "plink",
                    "requested_at": REQUESTED_AT,
                    "task": "x"}))
    (spool_dir / "not-json.wake.json").write_text("{oh no")
    write_record(spool_dir, "wake-20260825T120001Z-eeeeee")
    spool = WakeSpool(str(spool_dir), str(tmp_path / "served.json"))
    due, _ = spool.poll("res-gable", REQUESTED_EPOCH + 100)
    assert [r.wake_id for r in due] == ["wake-20260825T120001Z-eeeeee"]


def test_a_record_with_an_unreadable_time_is_never_runnable(tmp_path):
    rec = WakeRecord.from_dict({
        "wake_id": "wake-20260825T120000Z-ffffff", "resident": "res-gable",
        "woken_by": "plink", "requested_at": "whenever", "task": "x",
        "session_cap_sec": 60, "grace_sec": 10})
    assert rec.expired(0.0) is True


# ------------------------------------------------------------------ the session


def test_a_wake_runs_the_task_and_posts_what_the_wrapper_saw(tmp_path):
    launcher = FakeLauncher(result=SessionResult(
        ok=True, reply="Filed a card for the digest anomaly.", action_count=9,
        duration_sec=42.5, exit_code=0))
    runner, client, config = runner_for(tmp_path, launcher=launcher)
    write_record(Path(config.wake.spool_dir), "wake-20260825T120000Z-111111",
                 task="read the digest and open a card")

    outcomes = asyncio.run(runner.poll_once(now=REQUESTED_EPOCH + 100))

    assert [o.outcome for o in outcomes] == ["done"]
    # The task reached the session as DATA, in the marker the PreToolUse hook
    # keys on — it came from plink, but it is still text that must not ride
    # into a broker call.
    assert "read the digest and open a card" in launcher.prompts[0]
    assert "[[CHAT]]" in launcher.prompts[0]
    assert "wake-20260825T120000Z-111111" in launcher.prompts[0]

    post = posts(client)[0]
    assert post.startswith("wake done | wake-20260825T120000Z-111111 |")
    assert "res-gable woken by plink" in post
    assert "42.5s of 120s" in post
    # The session's words ride along LABELLED, under facts the wrapper measured.
    assert "session's own account" in post
    assert "Filed a card for the digest anomaly." in post


def test_the_cap_comes_from_the_record_not_from_this_seats_config(tmp_path):
    """Session caps are plink-owned config on the wake record. The seat's own
    file cannot widen them — it never names one."""
    seen = {}

    def factory(cap):
        seen["cap"] = cap
        return FakeLauncher()

    config = wake_config(tmp_path)
    runner = WakeRunner(FakeClient(), config, watch=GatehouseWatch(None),
                        launcher_factory=factory)
    write_record(Path(config.wake.spool_dir), "wake-20260825T120000Z-222222",
                 cap=1800)
    asyncio.run(runner.poll_once(now=REQUESTED_EPOCH + 100))
    assert seen["cap"] == 1800


def test_the_woken_session_launches_on_the_summon_seats_contract(tmp_path):
    """"The agentic seat gets no verb a summon seat lacks" has a launch-side
    half: same command, same session_argv, same model pin. Only the wall clock
    differs, and it is not this seat's to choose."""
    config = wake_config(tmp_path, spool_dir=str(tmp_path / "spool"))
    runner = WakeRunner(FakeClient(), config, watch=GatehouseWatch(None))
    from launcher import ContainerLauncher

    summon_argv = ContainerLauncher(config.container).build_argv()
    wake_launcher = runner._default_launcher(5400)
    assert wake_launcher.build_argv() == summon_argv
    assert wake_launcher.config.timeout_sec == 5400
    assert config.container.timeout_sec != 5400


def test_the_two_lanes_cannot_take_each_others_container(tmp_path):
    """run-resident.sh runs with `--replace`, and the summon adapter and the
    wake runner are independent daemons: without a distinct name, whichever
    session starts second kills the first — a summon quietly ending an
    hour-long wake, reported as a crash it never was."""
    config = wake_config(tmp_path)
    runner = WakeRunner(FakeClient(), config, watch=GatehouseWatch(None))
    env = runner._default_launcher(600).config.env
    assert env["RESIDENT_CONTAINER_SUFFIX"] == "wake"
    assert "RESIDENT_CONTAINER_SUFFIX" not in config.container.env

    wrapper = (Path(__file__).resolve().parents[2] / "cc" / "run-resident.sh"
               ).read_text()
    assert 'CONTAINER_NAME="resident-cc-$NAME${RESIDENT_CONTAINER_SUFFIX:+-$RESIDENT_CONTAINER_SUFFIX}"' in wrapper


def test_a_cap_kill_posts_a_failure_naming_the_cap(tmp_path):
    launcher = FakeLauncher(result=SessionResult(
        ok=False, error="session timed out after 120s", duration_sec=120.4,
        timed_out=True))
    runner, client, config = runner_for(tmp_path, launcher=launcher)
    write_record(Path(config.wake.spool_dir), "wake-20260825T120000Z-333333")

    outcomes = asyncio.run(runner.poll_once(now=REQUESTED_EPOCH + 100))

    assert [o.outcome for o in outcomes] == ["cap-kill"]
    post = posts(client)[0]
    assert post.startswith("WAKE FAILED | wake-20260825T120000Z-333333 |")
    assert "wall-clock cap fired at 120s" in post
    assert "120.4s of 120s" in post
    assert "a human should look" in post


def test_a_crash_posts_the_exit_status_in_place_of_the_cap_line(tmp_path):
    launcher = FakeLauncher(result=SessionResult(
        ok=False, exit_code=137, duration_sec=3.2,
        error="session exit 137: Killed"))
    runner, client, config = runner_for(tmp_path, launcher=launcher)
    write_record(Path(config.wake.spool_dir), "wake-20260825T120000Z-444444")

    outcomes = asyncio.run(runner.poll_once(now=REQUESTED_EPOCH + 100))

    assert [o.outcome for o in outcomes] == ["crash"]
    post = posts(client)[0]
    assert "the session exited 137" in post
    assert "cap fired" not in post


def test_a_session_killed_on_a_signal_says_which_signal(tmp_path):
    launcher = FakeLauncher(result=SessionResult(
        ok=False, exit_code=-9, duration_sec=1.0, error="session exit -9: "))
    runner, client, config = runner_for(tmp_path, launcher=launcher)
    write_record(Path(config.wake.spool_dir), "wake-20260825T120000Z-555555")
    asyncio.run(runner.poll_once(now=REQUESTED_EPOCH + 100))
    assert "died on signal 9" in posts(client)[0]


def test_a_failed_session_never_gets_to_narrate_itself(tmp_path):
    """A session we could not let finish is a session whose account of itself
    we cannot use. The branch and the clock say how far it got."""
    launcher = FakeLauncher(result=SessionResult(
        ok=False, reply="I finished everything, honest.", exit_code=1,
        duration_sec=5.0, error="session exit 1: boom"))
    runner, client, config = runner_for(tmp_path, launcher=launcher)
    write_record(Path(config.wake.spool_dir), "wake-20260825T120000Z-666666")
    asyncio.run(runner.poll_once(now=REQUESTED_EPOCH + 100))
    assert "honest" not in posts(client)[0]


def test_the_model_gate_refusal_is_its_own_post_and_still_ends_the_wake(
        tmp_path):
    launcher = FakeLauncher(result=SessionResult(
        ok=False, gate_abort=True, gate_expected="claude-fable-5",
        gate_actual="claude-opus-4-8", gate_stage="init", duration_sec=0.9,
        error="model gate refused"))
    runner, client, config = runner_for(tmp_path, launcher=launcher)
    write_record(Path(config.wake.spool_dir), "wake-20260825T120000Z-777777")

    outcomes = asyncio.run(runner.poll_once(now=REQUESTED_EPOCH + 100))

    assert [o.outcome for o in outcomes] == ["model-gate"]
    both = posts(client)
    assert any(p.startswith("MODEL GATE REFUSED") for p in both)
    assert any(p.startswith("WAKE FAILED") for p in both)


def test_a_launcher_that_raises_still_ends_in_a_post(tmp_path):
    class Exploding:
        async def run(self, prompt):
            raise RuntimeError("podman is gone")

    runner, client, config = runner_for(tmp_path, launcher=Exploding())
    write_record(Path(config.wake.spool_dir), "wake-20260825T120000Z-888888")
    outcomes = asyncio.run(runner.poll_once(now=REQUESTED_EPOCH + 100))
    assert [o.outcome for o in outcomes] == ["crash"]
    assert "podman is gone" in posts(client)[0]


def test_a_wake_the_runner_was_down_for_is_posted_not_run(tmp_path):
    launcher = FakeLauncher()
    runner, client, config = runner_for(tmp_path, launcher=launcher)
    write_record(Path(config.wake.spool_dir), "wake-20260825T120000Z-999999")

    outcomes = asyncio.run(runner.poll_once(now=REQUESTED_EPOCH + 86400))

    assert [o.outcome for o in outcomes] == ["missed"]
    assert launcher.prompts == []
    assert posts(client)[0].startswith("WAKE MISSED")


# --------------------------------------------------------------- the substrate


def test_the_banner_names_the_substrate_that_ran(tmp_path):
    """Spec decision 3: anything speaking under a resident's name stays on that
    resident's pinned model, substrate named in the banner."""
    launcher = FakeLauncher(result=SessionResult(
        ok=True, reply="done", duration_sec=1.0, exit_code=0,
        model="claude-fable-5"))
    config = wake_config(tmp_path)
    config.container.model = "claude-fable-5"
    client = FakeClient()
    runner = WakeRunner(client, config, watch=GatehouseWatch(None),
                        launcher_factory=lambda cap: launcher)
    write_record(Path(config.wake.spool_dir), "wake-20260825T120000Z-eee111")

    asyncio.run(runner.poll_once(now=REQUESTED_EPOCH + 100))

    assert "| claude-fable-5 |" in posts(client)[0]


def test_an_unconfirmed_pin_is_marked_not_advertised(tmp_path):
    """A session that reported no model id proves nothing about the pin. Saying
    the pin ran anyway would invert the point of naming it."""
    launcher = FakeLauncher(result=SessionResult(
        ok=False, timed_out=True, duration_sec=120.0, error="timed out"))
    config = wake_config(tmp_path)
    config.container.model = "claude-fable-5"
    client = FakeClient()
    runner = WakeRunner(client, config, watch=GatehouseWatch(None),
                        launcher_factory=lambda cap: launcher)
    write_record(Path(config.wake.spool_dir), "wake-20260825T120000Z-eee222")

    asyncio.run(runner.poll_once(now=REQUESTED_EPOCH + 100))

    assert "claude-fable-5 (pinned; actual unverified)" in posts(client)[0]


def test_model_drift_on_a_wake_is_its_own_loud_post(tmp_path):
    launcher = FakeLauncher(result=SessionResult(
        ok=True, reply="done", duration_sec=1.0, exit_code=0,
        model="claude-opus-4-8"))
    config = wake_config(tmp_path)
    config.container.model = "claude-fable-5"
    client = FakeClient()
    runner = WakeRunner(client, config, watch=GatehouseWatch(None),
                        launcher_factory=lambda cap: launcher)
    write_record(Path(config.wake.spool_dir), "wake-20260825T120000Z-eee333")

    asyncio.run(runner.poll_once(now=REQUESTED_EPOCH + 100))

    assert any(p.startswith("MODEL DRIFT") for p in posts(client))
    assert "| claude-opus-4-8 |" in posts(client)[-1]


# ----------------------------------------------------------------- accounting


def test_a_wake_writes_a_start_and_an_end_to_the_house_action_log(tmp_path):
    runner, client, config = runner_for(tmp_path, launcher=FakeLauncher())
    write_record(Path(config.wake.spool_dir), "wake-20260825T120000Z-aaa111")

    asyncio.run(runner.poll_once(now=REQUESTED_EPOCH + 100))

    lines = action_lines(config)
    assert [ln["event"] for ln in lines] == ["wake-start", "wake-end"]
    assert {ln["wake_id"] for ln in lines} == {"wake-20260825T120000Z-aaa111"}
    assert lines[1]["outcome"] == "done"
    assert lines[0]["cap_sec"] == 120
    assert isinstance(lines[1]["duration_sec"], float)


def test_a_wake_that_never_ends_leaves_a_start_with_no_end(tmp_path):
    """The incident shape the spec names: a start and no end is what a human
    checks after cap+grace with no post. So the start line must be written
    before the session, and it must survive the daemon dying with it."""
    class Vanishing:
        async def run(self, prompt):
            raise KeyboardInterrupt  # stands in for the daemon being killed

    runner, client, config = runner_for(tmp_path, launcher=Vanishing())
    write_record(Path(config.wake.spool_dir), "wake-20260825T120000Z-aaa222")
    with pytest.raises(KeyboardInterrupt):
        asyncio.run(runner.poll_once(now=REQUESTED_EPOCH + 100))
    lines = action_lines(config)
    assert [ln["event"] for ln in lines] == ["wake-start"]


def test_the_action_count_is_the_wrappers_measurement(tmp_path):
    """Not the session's `num_turns`: the lines the container appended to the
    house action log while it ran. The wake-start line is written first and is
    not counted."""
    log = tmp_path / "action-log"

    class Counting(FakeLauncher):
        async def run(self, prompt):
            with open(log, "a", encoding="utf-8") as fh:
                for i in range(5):
                    fh.write(json.dumps({"ts": "2026-08-25T12:00:0%dZ" % i,
                                         "tool_name": "Bash", "ok": True}) + "\n")
            return await super().run(prompt)

    launcher = Counting(result=SessionResult(
        ok=True, reply="done", action_count=2, duration_sec=1.0, exit_code=0))
    runner, client, config = runner_for(tmp_path, launcher=launcher)
    write_record(Path(config.wake.spool_dir), "wake-20260825T120000Z-aaa333")

    outcomes = asyncio.run(runner.poll_once(now=REQUESTED_EPOCH + 100))

    assert outcomes[0].action_count == 5
    assert "5 actions" in posts(client)[0]
    assert action_lines(config)[-1]["action_count"] == 5


def test_an_unreadable_action_log_is_reported_as_unknown_never_zero(tmp_path):
    runner, client, config = runner_for(tmp_path, launcher=FakeLauncher(),
                                        action_log=None)
    write_record(Path(config.wake.spool_dir), "wake-20260825T120000Z-aaa444")
    outcomes = asyncio.run(runner.poll_once(now=REQUESTED_EPOCH + 100))
    assert outcomes[0].action_count is None
    assert "actions n/a" in posts(client)[0]


# ------------------------------------------------------------------ gatehouse


def git(repo: Path, *args: str) -> str:
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    return subprocess.run(["git", "-C", str(repo), *args], check=True, env=env,
                          capture_output=True, text=True).stdout


@pytest.fixture()
def gatehouse(tmp_path):
    """A bare gatehouse repo plus a worktree that pushes loop branches into it
    — the transport a woken session actually uses."""
    house = tmp_path / "gatehouse"
    house.mkdir()
    bare = house / "disjorn.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(bare)],
                   check=True, capture_output=True)
    work = tmp_path / "work"
    subprocess.run(["git", "init", "-q", "-b", "main", str(work)], check=True,
                   capture_output=True)
    (work / "seed").write_text("seed\n")
    git(work, "add", "-A")
    git(work, "commit", "-q", "-m", "seed")
    git(work, "remote", "add", "origin", str(bare))
    git(work, "push", "-q", "origin", "main")
    return house, work


def push_loop_branch(work: Path, branch: str, subject: str) -> None:
    git(work, "checkout", "-q", "-B", branch)
    (work / f"{branch.replace('/', '-')}.txt").write_text(subject + "\n")
    git(work, "add", "-A")
    git(work, "commit", "-q", "-m", subject)
    git(work, "push", "-q", "origin", branch)


def test_a_wip_head_is_read_off_the_branch_and_quoted(tmp_path, gatehouse):
    """The spec's `wip:` rule, from the wrapper's side: partial work says so in
    the branch, and the failure post quotes the head subject. No memory, no
    chat, nothing the session claims."""
    house, work = gatehouse

    class Pushing(FakeLauncher):
        async def run(self, prompt):
            push_loop_branch(work, "loop/2026-08-25-thing", "wip: half of it")
            return await super().run(prompt)

    launcher = Pushing(result=SessionResult(
        ok=False, timed_out=True, duration_sec=120.1,
        error="session timed out after 120s"))
    runner, client, config = runner_for(
        tmp_path, launcher=launcher, watch=GatehouseWatch(str(house)))
    write_record(Path(config.wake.spool_dir), "wake-20260825T120000Z-bbb111")

    outcomes = asyncio.run(runner.poll_once(now=REQUESTED_EPOCH + 100))

    (branch,) = outcomes[0].branches
    assert branch["ref"] == "loop/2026-08-25-thing"
    assert branch["wip"] is True and branch["created"] is True
    post = posts(client)[0]
    assert "disjorn.git:loop/2026-08-25-thing" in post
    assert "(wip)" in post and '"wip: half of it"' in post
    assert branch["sha"][:12] in post


def test_a_finishing_commit_drops_the_wip_and_the_post_shows_it(
        tmp_path, gatehouse):
    house, work = gatehouse

    class Pushing(FakeLauncher):
        async def run(self, prompt):
            push_loop_branch(work, "loop/2026-08-25-thing", "the whole thing")
            return await super().run(prompt)

    runner, client, config = runner_for(
        tmp_path, launcher=Pushing(), watch=GatehouseWatch(str(house)))
    write_record(Path(config.wake.spool_dir), "wake-20260825T120000Z-bbb222")

    outcomes = asyncio.run(runner.poll_once(now=REQUESTED_EPOCH + 100))

    assert outcomes[0].branches[0]["wip"] is False
    assert "(new)" in posts(client)[0]


def test_a_branch_that_did_not_move_is_not_reported_as_work(
        tmp_path, gatehouse):
    house, work = gatehouse
    push_loop_branch(work, "loop/2026-08-24-old", "landed yesterday")
    runner, client, config = runner_for(
        tmp_path, launcher=FakeLauncher(), watch=GatehouseWatch(str(house)))
    write_record(Path(config.wake.spool_dir), "wake-20260825T120000Z-bbb333")

    outcomes = asyncio.run(runner.poll_once(now=REQUESTED_EPOCH + 100))

    assert outcomes[0].branches == []
    assert "branches: none moved" in posts(client)[0]


def test_no_gatehouse_reads_as_unobserved_never_as_no_branch(tmp_path):
    runner, client, config = runner_for(tmp_path, launcher=FakeLauncher())
    write_record(Path(config.wake.spool_dir), "wake-20260825T120000Z-bbb444")
    outcomes = asyncio.run(runner.poll_once(now=REQUESTED_EPOCH + 100))
    assert outcomes[0].branches is None
    assert "branches: not observed" in posts(client)[0]


# --------------------------------------------------------------- end to end


def test_the_cap_really_kills_a_real_session(tmp_path):
    """Not a fake: the stub launch script sleeps past the record's cap, and the
    launcher the runner built from that cap kills it."""
    record_file = tmp_path / "stub-record.json"
    config = wake_config(tmp_path, poll_interval_sec=0.01)
    config.container.env.update({
        "RESIDENCY_STUB_RECORD": str(record_file),
        "RESIDENCY_STUB_SLEEP": "10",
    })
    runner = WakeRunner(FakeClient(), config, watch=GatehouseWatch(None))
    write_record(Path(config.wake.spool_dir), "wake-20260825T120000Z-ccc111",
                 cap=1, grace=600, task="something slow")

    outcomes = asyncio.run(runner.poll_once(now=REQUESTED_EPOCH + 100))

    assert [o.outcome for o in outcomes] == ["cap-kill"]
    assert "wall-clock cap fired at 1s" in outcomes[0].post
    written = json.loads(record_file.read_text())
    assert "something slow" in written["stdin"]


def test_a_real_session_that_answers_is_posted_as_done(tmp_path):
    record_file = tmp_path / "stub-record.json"
    config = wake_config(tmp_path)
    config.container.env.update({
        "RESIDENCY_STUB_RECORD": str(record_file),
        "RESIDENCY_STUB_STDOUT": json.dumps(
            {"result": "Opened the card.", "num_turns": 6}),
    })
    client = FakeClient()
    runner = WakeRunner(client, config, watch=GatehouseWatch(None))
    write_record(Path(config.wake.spool_dir), "wake-20260825T120000Z-ccc222")

    outcomes = asyncio.run(runner.poll_once(now=REQUESTED_EPOCH + 100))

    assert [o.outcome for o in outcomes] == ["done"]
    assert "Opened the card." in posts(client)[0]
    assert json.loads(record_file.read_text())["argv"] == ["gable"]


def test_the_runner_refuses_to_exist_without_a_spool(tmp_path):
    """A wake runner that polls nothing is a daemon that looks like a lane."""
    config = make_config(tmp_path)
    with pytest.raises(ValueError, match="spool_dir"):
        WakeRunner(FakeClient(), config)


def test_the_accounting_is_a_noop_without_a_log_and_says_so(tmp_path, caplog):
    acc = WakeAccounting(None)
    assert acc.configured is False
    assert acc.line_count() is None
    rec = WakeRecord.from_dict({
        "wake_id": "wake-20260825T120000Z-ddd111", "resident": "res-gable",
        "woken_by": "plink", "requested_at": REQUESTED_AT,
        "task": "x", "session_cap_sec": 60, "grace_sec": 10})
    with caplog.at_level("WARNING"):
        acc.start(rec)
    assert "action_log" in caplog.text


def test_the_seat_name_is_converted_in_exactly_one_place(tmp_path):
    config = wake_config(tmp_path)
    runner = WakeRunner(FakeClient(), config, watch=GatehouseWatch(None))
    assert config.container.resident == "gable"
    assert runner.seat == "res-gable"


@pytest.mark.skipif(sys.platform != "linux", reason="stub launch is POSIX")
def test_the_stub_launch_script_exists():
    """Guards the end-to-end tests above from silently degrading into fakes."""
    from residency_testlib import STUB_LAUNCH

    assert Path(STUB_LAUNCH).exists()
