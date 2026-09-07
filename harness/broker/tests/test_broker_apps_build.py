"""apps-build — the handoff verb, its four checks, its reaper and its ledger.
SPECS/2026-09-06-apps-builder-seat.md §B/§E/§H.

The broker runs for real on a scratch socket (SO_PEERCRED, dispatch, audit,
verbs.toml kill switch), talking to a FAKE server (`planroom_api`) and a FAKE
launcher (`apps_spawn`). Nothing here touches /srv, sudo, systemd or the prod
database — the turn directory is a tmp tree the test writes into on cue, which
is exactly the shape of the real contract: everything the broker learns about a
turn it learns by reading result.json.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from broker_testlib import ALL_VERBS, BrokerHarness   # noqa: E402
from brokerd import Broker, VerbError, load_config    # noqa: E402

SEAT = "res-gable"
BOT_ID = 2
APP_ID = "abc234567xyz"
SESSION = 12


# ---------------------------------------------------------------- fake launcher

class FakeAppsProc:
    """The local `sudo … disjorn-apps-launch run …` process.

    In production it BLOCKS for the whole turn (measured: 68s and 192s), so the
    default here is a process that never finishes on its own: a test that wants
    it to end says so. `rc` preset non-None is the launcher that refused before
    any privilege and was gone in milliseconds."""

    def __init__(self, rc=None, err: bytes = b""):
        self.pid = 5150
        self.returncode = rc
        self._err = err
        self.err_fh = None

    def attach_logs(self, out_fh, err_fh):
        self.err_fh = err_fh
        if self._err:
            err_fh.write(self._err)
            err_fh.flush()

    def finish(self, rc: int = 0) -> None:
        self.returncode = rc

    def wait(self, timeout=None):
        if self.returncode is None:
            raise subprocess.TimeoutExpired(cmd="apps-launch", timeout=timeout)
        return self.returncode

    def poll(self):
        return self.returncode


class FakeAppsSpawn:
    """Injectable `_apps_spawn`: records each argv, hands back a proc, and dups
    the broker's output files into it the way a forked child holds its own."""

    def __init__(self, proc_factory=None):
        self._factory = proc_factory or FakeAppsProc
        self.calls: list[list[str]] = []
        self.procs: list[FakeAppsProc] = []

    def __call__(self, argv, *, stdout, stderr):
        self.calls.append(list(argv))
        proc = self._factory()
        proc.attach_logs(os.fdopen(os.dup(stdout.fileno()), "wb"),
                         os.fdopen(os.dup(stderr.fileno()), "wb"))
        self.procs.append(proc)
        return proc


RESULT = {
    "session": SESSION, "turn": 1, "app_id": APP_ID,
    "exit": 0, "halted": None, "no_changes": False,
    "files": ["app.js", "index.html"],
    "commit": "e01a4eb1234", "quarantine": None, "error": None,
    "started_at": "2026-09-07T20:14:26+00:00",
    "ended_at": "2026-09-07T20:17:38+00:00",
    "model": "claude-opus-5", "runner": "claude-code",
    "spool": {"stdout": "/srv/apps-turns/12/1/stdout.log",
              "stderr": "/srv/apps-turns/12/1/stderr.log"},
    "spool_redacted": False,
    "usage": {"input_tokens": 54, "output_tokens": 10485,
              "cache_creation_input_tokens": 29868,
              "cache_read_input_tokens": 877874,
              "total_cost_usd": 0.889, "num_turns": 30,
              "duration_ms": 192449, "is_error": False},
}
RESULT_TOKENS = 54 + 10485 + 29868


# ------------------------------------------------------------------- harness

class AppsHarness(BrokerHarness):
    """A broker with an apps-builder behind it, a fake server in front of it,
    and a turn directory the test writes into."""

    def __init__(self, broker, verbs_path, tmp_path, *, stages, view, spawn,
                 narrations):
        super().__init__(broker, verbs_path, tmp_path / "record.jsonl", [])
        self.tmp_path = tmp_path
        self.stages = stages          # every stage POST, in order
        self.view = view              # what the harness-view endpoint answers
        self.spawn = spawn
        self.narrations = narrations
        self._thread = None

    # -- the turn directory the seat's harvest would write -----------------
    def turn_dir(self, session: int = SESSION, turn: int = 1) -> Path:
        d = self.tmp_path / "apps-turns" / str(session) / str(turn)
        d.mkdir(parents=True, exist_ok=True)
        return d

    def write_scaffolded(self, session: int = SESSION, turn: int = 1) -> None:
        (self.turn_dir(session, turn) / "scaffolded").write_text(
            "2026-09-07T20:14:30+00:00\n")

    def write_result(self, session: int = SESSION, turn: int = 1,
                     raw: str | None = None, **overrides) -> None:
        path = self.turn_dir(session, turn) / "result.json"
        if raw is not None:
            path.write_text(raw)
            return
        record = {**RESULT, "session": session, "turn": turn, **overrides}
        path.write_text(json.dumps(record))

    # -- inspection --------------------------------------------------------
    def prompt(self, name: str = f"{SESSION}-1.md",
               text: str = "build me a thing\n") -> str:
        """Write a prompt where the seat's own path_map says it belongs, and
        return the path AS THE RESIDENT SEES IT."""
        host = self.tmp_path / "gable-home" / "apps-prompts"
        host.mkdir(parents=True, exist_ok=True)
        (host / name).write_text(text)
        return f"/home/resident/apps-prompts/{name}"

    def ledger(self) -> list[dict]:
        path = self.tmp_path / "apps-ledger.jsonl"
        if not path.exists():
            return []
        return [json.loads(ln) for ln in path.read_text().splitlines() if ln.strip()]

    def sidecars(self) -> list[Path]:
        return sorted((self.tmp_path / "apps-logs").glob("*.apps.json"))

    def stage_names(self) -> list[str]:
        return [s["stage"] for s in self.stages]

    def wait_for_stages(self, n: int, timeout: float = 5.0) -> None:
        deadline = time.time() + timeout
        while len(self.stages) < n and time.time() < deadline:
            time.sleep(0.01)

    def last_refusal(self) -> dict:
        return self.audit_lines()[-1]


def _make_bots_db(path: Path, rows) -> None:
    import sqlite3
    db = sqlite3.connect(path)
    db.execute("create table bots (id integer primary key, name text not null)")
    db.executemany("insert into bots (id, name) values (?, ?)", rows)
    db.commit()
    db.close()


def build_apps_harness(tmp_path: Path, *, apps_table: bool = True,
                       seat_bots: str = f"{{ {SEAT} = {BOT_ID} }}",
                       bots=((BOT_ID, "Gable"), (1, "Claudette")),
                       ceiling: int = 10_000_000,
                       enable: bool = True,
                       view: dict | None = None,
                       spawn: FakeAppsSpawn | None = None) -> AppsHarness:
    (tmp_path / "apps-logs").mkdir(exist_ok=True)
    (tmp_path / "gable-home").mkdir(exist_ok=True)
    db = tmp_path / "disjorn.db"
    _make_bots_db(db, bots)

    apps_block = ""
    if apps_table:
        apps_block = textwrap.dedent(f"""\

            [apps]
            runner = "claude-code"
            seat_bots = {seat_bots}
            model = "claude-opus-5"
            build_token_ceiling = {ceiling}
            prompt_max_bytes = 200
            turn_max_sec = 30
            turns_root = "{tmp_path / 'apps-turns'}"
            launch_command = ["/nonexistent/disjorn-apps-launch", "run"]
            unit_state_command = ["/nonexistent/systemctl"]
            ledger_path = "{tmp_path / 'apps-ledger.jsonl'}"
            log_dir = "{tmp_path / 'apps-logs'}"
            poll_sec = 0.02
            result_grace_sec = 0.1
            chat_markers = ["[[CHAT]]", "[[/CHAT]]"]
        """)

    broker_toml = tmp_path / "broker.toml"
    broker_toml.write_text(textwrap.dedent(f"""\
        [broker]
        socket_path = "{tmp_path / 'b.sock'}"
        audit_log = "{tmp_path / 'audit.jsonl'}"
        build_log_dir = "{tmp_path / 'build-logs'}"

        [uids]
        "{os.getuid()}" = "{SEAT}"

        [residents.{SEAT}]
        log_path = "{tmp_path / 'gable.log'}"

        [residents.{SEAT}.path_map]
        "/home/resident" = "{tmp_path / 'gable-home'}"

        [residents.res-claudette]
        log_path = "{tmp_path / 'claudette.log'}"

        [gate]
        message_db = "{db}"

        [paths]
        metrics_json = "{tmp_path / 'metrics.json'}"

        [disjorn]
        url = "http://127.0.0.1:1"
        api_key_path = "{tmp_path / 'no-key'}"
        custodian_channel_id = 4
    """) + apps_block)
    (tmp_path / "build-logs").mkdir(exist_ok=True)

    stages: list = []
    narrations: list = []
    live_view = dict(view if view is not None else {
        "session_id": SESSION, "app_id": APP_ID, "owner_user_id": 7,
        "builder_bot_id": BOT_ID, "channel_id": 41, "stage": None,
        "turns": 0, "tokens_used": 0, "open": True, "lock_lapsed": False,
        "ended_at": None, "locked_until": "2026-09-07T21:00:00Z",
    })

    def stub_api(disjorn_cfg, method, path, payload=None):
        if method == "GET" and path.endswith("/harness-view"):
            if live_view.get("_status") == 404:
                raise VerbError("exec-failure", "no such app session", status=404)
            return {k: v for k, v in live_view.items() if not k.startswith("_")}
        if method == "POST" and path.endswith("/stage"):
            session = int(path.split("/")[3])
            stages.append({"session": session, **payload})
            if live_view.get("_stage_status"):
                raise VerbError("exec-failure", "refused",
                                status=live_view["_stage_status"])
            return {"ok": True}
        raise VerbError("exec-failure", f"unstubbed path {path}")

    def stub_transport(disjorn_cfg, body):
        narrations.append(body)
        return {"seq": 1, "message_id": 1}

    spawn = spawn or FakeAppsSpawn()
    verbs_path = tmp_path / "verbs.toml"
    broker = Broker(load_config(str(broker_toml)), str(verbs_path),
                    transport=stub_transport, planroom_api=stub_api,
                    apps_spawn=spawn)
    h = AppsHarness(broker, verbs_path, tmp_path, stages=stages, view=live_view,
                    spawn=spawn, narrations=narrations)
    h.set_verbs(SEAT, **{"apps-build": enable})
    t = threading.Thread(target=broker.serve_forever, daemon=True)
    t.start()
    deadline = time.time() + 5
    while not os.path.exists(broker.socket_path):
        if time.time() > deadline:
            raise RuntimeError("broker socket never appeared")
        time.sleep(0.01)
    h._thread = t
    return h


@pytest.fixture()
def apps(tmp_path):
    h = build_apps_harness(tmp_path)
    yield h
    h.broker.shutdown()
    h.broker.join_apps(timeout=5)
    h._thread.join(timeout=5)


def _handoff(h: AppsHarness, prompt: str | None = None, **args) -> dict:
    payload = {"session_id": SESSION,
               "prompt_file": prompt if prompt is not None else h.prompt()}
    payload.update(args)
    return h.call("apps-build", payload)


# ---------------------------------------------------------- the kill switch

def test_the_verb_ships_off(tmp_path):
    """Every verb ships OFF for every resident, and this one is no exception —
    the seat map and the launcher exist to be flipped ON deliberately."""
    h = build_apps_harness(tmp_path, enable=False)
    try:
        resp = _handoff(h)
        assert resp["ok"] is False
        assert resp["error"]["code"] == "verb-disabled"
        assert h.spawn.calls == []
    finally:
        h.broker.shutdown()
        h._thread.join(timeout=5)


def test_the_shipped_switch_file_carries_it_off_for_both_seats():
    import tomllib
    path = Path(__file__).resolve().parent.parent / "verbs.toml"
    data = tomllib.loads(path.read_text())
    assert data["res-gable"]["apps-build"] is False
    assert data["res-claudette"]["apps-build"] is False


@pytest.mark.parametrize("args", [
    {"session_id": SESSION, "prompt_file": "/tmp/p.md", "bot_id": 2},
    {"session_id": SESSION},
    {"prompt_file": "/tmp/p.md"},
    {"session_id": 0, "prompt_file": "/tmp/p.md"},
    {"session_id": "12", "prompt_file": "/tmp/p.md"},
    {"session_id": SESSION, "prompt_file": 7},
])
def test_hostile_or_missing_args_are_bad_args(apps, args):
    """A bot id is NEVER a verb argument (§E): the extra key is refused with
    everything else that does not fit the schema."""
    resp = apps.call("apps-build", args)
    assert resp["ok"] is False
    assert resp["error"]["code"] == "bad-args"
    assert apps.spawn.calls == []


# ------------------------------------------------------------ the four checks

def test_an_unknown_session_is_refused_with_a_sentence(apps):
    apps.view["_status"] = 404
    resp = _handoff(apps)
    assert resp["error"]["code"] == "apps-refused"
    assert resp["error"]["message"] == "no such build session"
    entry = apps.last_refusal()
    assert entry["allowed"] is False
    assert "no such build session" in entry["result_summary"]
    assert apps.spawn.calls == []


def test_an_ended_session_is_refused(apps):
    apps.view["open"] = False
    resp = _handoff(apps)
    assert resp["error"]["message"] == "this build session has ended"
    assert apps.spawn.calls == []


def test_a_lapsed_lock_does_not_refuse_the_handoff(apps):
    """Keyboard ruling D-A1. The lock is the USER's chat exclusivity, refreshed
    by the modal's heartbeat; a resident handing off seconds after the user
    closed the modal should still land the turn."""
    apps.view["lock_lapsed"] = True
    resp = _handoff(apps)
    assert resp["ok"] is True
    assert len(apps.spawn.calls) == 1


def test_an_unmapped_seat_is_refused(tmp_path):
    h = build_apps_harness(tmp_path, seat_bots="{ res-claudette = 1 }")
    try:
        assert h.broker._apps_disabled_reason is None
        resp = _handoff(h)
        assert resp["error"]["message"] == "this seat is not mapped to a builder bot"
        assert h.spawn.calls == []
    finally:
        h.broker.shutdown()
        h._thread.join(timeout=5)


def test_another_builders_session_is_refused(apps):
    apps.view["builder_bot_id"] = 1
    resp = _handoff(apps)
    assert resp["error"]["message"] == "this session belongs to another builder"
    assert apps.spawn.calls == []


def test_the_ceiling_refuses_and_still_tells_the_room(apps):
    """D-1.2b: a ceiling refusal posts a halted event even though nothing ran —
    the room and the bar have to see why nothing is going to happen."""
    apps.view["tokens_used"] = 10_000_000
    apps.view["turns"] = 3
    apps.view["stage"] = "deployed"
    resp = _handoff(apps)
    assert resp["error"]["code"] == "apps-refused"
    assert resp["error"]["message"] == (
        "this build has hit its token ceiling (10000000 of 10000000)")
    assert apps.spawn.calls == []
    (event,) = apps.stages
    assert event["stage"] == "deployed"          # the last-reached stage
    assert event["detail"]["turn"] == 4
    assert event["detail"]["halted"] == "ceiling"
    assert event["detail"]["reason"]
    (line,) = apps.ledger()
    assert line["halted"] == "ceiling"
    assert line["exit"] is None
    assert line["tokens"] == 0
    assert line["ceiling"] == 10_000_000


def test_a_ceiling_refusal_with_no_stage_yet_posts_scoped(apps):
    apps.view["tokens_used"] = 10_000_001
    apps.view["stage"] = None
    _handoff(apps)
    assert apps.stages[0]["stage"] == "scoped"


def test_one_turn_at_a_time_per_session(apps):
    first = _handoff(apps)
    assert first["ok"] is True
    second = _handoff(apps)
    assert second["error"]["code"] == "apps-refused"
    assert second["error"]["message"] == (
        "a turn is already running for this session")
    assert len(apps.spawn.calls) == 1


# ------------------------------------------------------------- the prompt file

def test_a_prompt_outside_the_seats_map_is_refused(apps):
    resp = _handoff(apps, prompt="/etc/passwd")
    assert resp["error"]["code"] == "bad-args"
    assert "mapped root" in resp["error"]["message"]
    assert apps.spawn.calls == []


def test_a_chat_marker_is_refused_in_the_sentence_the_resident_can_repeat(apps):
    """VERBATIM (§E, Claudette #2284): a resident that faithfully quoted a user
    who typed the marker must be able to say WHAT happened, not "something went
    wrong"."""
    prompt = apps.prompt(text="the user said [[CHAT]] hello\n")
    resp = _handoff(apps, prompt=prompt)
    assert resp["error"]["message"] == (
        "The prompt file contains a chat marker the harness cannot pass "
        "through; quote the user's words without it")
    assert apps.spawn.calls == []
    # And the file is untouched: the broker reads a prompt, it never edits one.
    assert "[[CHAT]]" in (apps.tmp_path / "gable-home" / "apps-prompts"
                          / f"{SESSION}-1.md").read_text()


def test_an_oversize_prompt_is_refused_with_its_bound(apps):
    resp = _handoff(apps, prompt=apps.prompt(text="x" * 500))
    assert resp["error"]["message"] == "the prompt file is larger than 200 bytes"
    assert apps.spawn.calls == []


def test_an_empty_prompt_is_refused(apps):
    resp = _handoff(apps, prompt=apps.prompt(text="   \n"))
    assert resp["error"]["message"] == "the prompt file is empty"


def test_an_absent_prompt_is_refused(apps):
    resp = _handoff(apps, prompt="/home/resident/apps-prompts/nope.md")
    assert resp["error"]["message"] == "the prompt file cannot be read"


def test_the_claim_is_released_when_the_prompt_is_refused(apps):
    """A refusal after the claim must not leave the session claimed forever."""
    _handoff(apps, prompt=apps.prompt(text="[[CHAT]]"))
    assert apps.broker._active_apps == {}
    assert apps.sidecars() == []


# --------------------------------------------------------------- the launch

def test_the_launcher_argv_is_config_plus_validated_scalars(apps):
    resp = _handoff(apps)
    assert resp["ok"] is True
    assert resp["result"] == {"turn": 1, "unit": "disjorn-apps-12-1.service",
                              "app_id": APP_ID}
    (argv,) = apps.spawn.calls
    assert argv[:2] == ["/nonexistent/disjorn-apps-launch", "run"]
    assert argv[2:6] == [SEAT, "12", "1", APP_ID]
    # The prompt reaches the launcher as the HOST path, resolved through the
    # seat's own path_map — never the container path the resident typed.
    assert argv[6] == str(apps.tmp_path / "gable-home" / "apps-prompts"
                          / f"{SESSION}-1.md")
    assert len(argv) == 7


def test_the_turn_number_follows_the_servers_count(apps):
    apps.view["turns"] = 4
    resp = _handoff(apps)
    assert resp["result"]["turn"] == 5
    assert resp["result"]["unit"] == "disjorn-apps-12-5.service"


def test_the_spawn_posts_scoped_and_audits_the_facts(apps):
    _handoff(apps)
    assert apps.stages[0] == {"session": SESSION, "stage": "scoped",
                              "detail": {"turn": 1, "model": "claude-opus-5"}}
    entry = apps.last_refusal()
    assert entry["allowed"] is True
    assert entry["session"] == SESSION and entry["turn"] == 1
    assert entry["unit"] == "disjorn-apps-12-1.service"
    assert entry["app_id"] == APP_ID


def test_a_launcher_refusal_reaches_the_caller_and_posts_nothing(apps):
    """Exit 64 = refused before any privilege, and it comes back in
    milliseconds. The resident hears it in its own turn; the room hears nothing,
    because nothing happened."""
    apps.broker._apps_spawn = FakeAppsSpawn(
        lambda: FakeAppsProc(rc=64, err=b"noise\nprompt is not a regular file\n"))
    resp = _handoff(apps)
    assert resp["error"]["code"] == "apps-refused"
    assert resp["error"]["message"] == (
        "the launcher refused the turn: prompt is not a regular file")
    assert apps.stages == []
    assert apps.ledger() == []
    assert apps.broker._active_apps == {}
    assert apps.sidecars() == []


def test_a_launcher_that_could_not_start_at_all_is_distinguished(apps):
    """Any other non-zero is systemd-run failing, not a validation refusal, and
    it says so rather than borrowing exit 64's sentence."""
    apps.broker._apps_spawn = FakeAppsSpawn(lambda: FakeAppsProc(rc=1, err=b"boom\n"))
    resp = _handoff(apps)
    assert resp["error"]["message"] == "the turn could not be launched (exit 1): boom"


def test_a_sidecar_is_written_before_the_launch(apps):
    _handoff(apps)
    (path,) = apps.sidecars()
    assert oct(path.stat().st_mode)[-3:] == "600"
    rec = json.loads(path.read_text())
    assert rec["schema"] == 1
    assert rec["session"] == SESSION and rec["turn"] == 1
    assert rec["app_id"] == APP_ID and rec["caller"] == SEAT
    assert rec["unit"] == "disjorn-apps-12-1.service"
    assert rec["tokens_before"] == 0
    assert "pid" not in rec          # the durable handle is the unit name


# ------------------------------------------------------------ the happy path

def test_a_finished_turn_posts_four_stages_in_order_with_the_agreed_details(apps):
    apps.write_scaffolded()
    apps.write_result()
    _handoff(apps)
    apps.wait_for_stages(4)
    assert apps.stage_names() == ["scoped", "scaffolded", "files_written",
                                 "deployed"]
    assert apps.stages[1]["detail"] == {"turn": 1}
    assert apps.stages[2]["detail"] == {
        "turn": 1, "files": ["app.js", "index.html"],
        "tokens": RESULT_TOKENS, "model": "claude-opus-5", "no_changes": False}
    assert apps.stages[3]["detail"] == {"turn": 1}


def test_a_summary_rides_the_files_written_detail(apps):
    apps.write_result(summary="Added a scoreboard and wired it to the timer.")
    _handoff(apps)
    apps.wait_for_stages(3)
    assert apps.stages[-2]["detail"]["summary"] == (
        "Added a scoreboard and wired it to the timer.")


def test_an_empty_diff_reports_no_changes_and_never_deploys(apps):
    apps.write_result(no_changes=True, files=[], commit=None)
    _handoff(apps)
    apps.wait_for_stages(2)
    apps.broker.join_apps(timeout=5)
    assert apps.stage_names() == ["scoped", "files_written"]
    assert apps.stages[-1]["detail"]["no_changes"] is True


def test_a_turn_that_committed_nothing_never_deploys(apps):
    apps.write_result(commit=None)
    _handoff(apps)
    apps.broker.join_apps(timeout=5)
    assert "deployed" not in apps.stage_names()


def test_the_ledger_line_is_the_record_of_the_turn(apps):
    apps.write_result()
    _handoff(apps)
    apps.broker.join_apps(timeout=5)
    (line,) = apps.ledger()
    assert line["session"] == SESSION and line["turn"] == 1
    assert line["app"] == APP_ID and line["caller"] == SEAT
    assert line["unit"] == "disjorn-apps-12-1.service"
    assert line["exit"] == 0 and line["halted"] is None
    assert line["no_changes"] is False
    assert line["commit"] == "e01a4eb1234"
    assert line["files"] == 2
    assert line["model"] == "claude-opus-5" and line["runner"] == "claude-code"
    assert line["usage"] == {"input": 54, "output": 10485,
                             "cache_read": 877874, "cache_creation": 29868,
                             "cost_usd": 0.889}
    # Parent Round 6: the trip log record names the column it summed, and cache
    # READS are not in it.
    assert line["ceiling_column"] == "input+output+cache_creation"
    assert line["tokens"] == RESULT_TOKENS
    assert line["tokens_after"] == RESULT_TOKENS
    assert line["ceiling"] == 10_000_000
    assert line["synthesized"] is False
    assert line["seconds"] == 192.0          # from the turn's own clock


def test_a_finished_turn_releases_the_session_and_tears_up_its_ticket(apps):
    apps.write_result()
    _handoff(apps)
    apps.broker.join_apps(timeout=5)
    assert apps.broker._active_apps == {}
    assert apps.sidecars() == []
    # ... and the next handoff is accepted.
    apps.view["turns"] = 1
    assert _handoff(apps, prompt=apps.prompt(name=f"{SESSION}-2.md"))["ok"] is True


# ------------------------------------------------------------------ the halts

def test_a_halted_turn_reposts_the_last_stage_it_reached(apps):
    apps.write_scaffolded()
    apps.write_result(halted="timeout", exit=1, commit=None, files=[],
                      no_changes=False)
    _handoff(apps)
    apps.broker.join_apps(timeout=5)
    assert apps.stage_names() == ["scoped", "scaffolded", "scaffolded"]
    detail = apps.stages[-1]["detail"]
    assert detail["halted"] == "timeout"
    assert detail["turn"] == 1
    assert apps.ledger()[0]["halted"] == "timeout"


def test_a_turn_that_died_before_scaffolding_reposts_scoped(apps):
    apps.write_result(halted="error", exit=1, commit=None, files=[],
                      error="the harvest could not commit")
    _handoff(apps)
    apps.broker.join_apps(timeout=5)
    assert apps.stage_names() == ["scoped", "scoped"]
    assert apps.stages[-1]["detail"]["reason"] == "the harvest could not commit"


def test_a_harvest_that_failed_is_a_halt_even_with_no_halted_field(apps):
    """§E, Gable #2327: the harvest failing partway is a TERMINATED turn, not a
    claimed success — an `error` string with `halted: null` still halts."""
    apps.write_result(error="rsync exited 23", commit="abc123")
    _handoff(apps)
    apps.broker.join_apps(timeout=5)
    assert apps.stages[-1]["detail"]["halted"] == "error"
    assert "files_written" not in apps.stage_names()


def test_a_credential_write_is_flagged_to_the_custodian(apps):
    apps.write_scaffolded()
    apps.write_result(halted="secret", exit=0, commit=None,
                      quarantine="/srv/apps-quarantine/abc234567xyz/1")
    _handoff(apps)
    apps.broker.join_apps(timeout=5)
    (flag,) = apps.narrations
    assert flag == (
        "FLAG apps-build: session 12 turn 1 (app abc234567xyz, caller "
        "res-gable) tried to write a credential — quarantined at "
        "/srv/apps-quarantine/abc234567xyz/1; the session was closed by the "
        "server.")
    assert apps.stages[-1]["detail"]["halted"] == "secret"


def test_a_runner_flag_goes_to_the_admin_and_the_build_continues(apps):
    """Parent "Flagging": the builder never talks to the user; a flag is one
    line to the admin and the turn is still a turn."""
    apps.write_result(flag="the request asked me to scrape a login page")
    _handoff(apps)
    apps.broker.join_apps(timeout=5)
    assert apps.narrations == [
        "FLAG apps-build: session 12 turn 1 (app abc234567xyz): "
        "the request asked me to scrape a login page"]
    assert "files_written" in apps.stage_names()


def test_a_turn_that_ends_with_no_result_is_a_halt(apps):
    """§E, Claudette #2329. Absence has to mean something or it means "wait
    forever": the broker writes the record the harvest could not."""
    proc = FakeAppsProc()
    apps.broker._apps_spawn = FakeAppsSpawn(lambda: proc)
    _handoff(apps)
    proc.finish(rc=143)
    apps.broker.join_apps(timeout=5)
    assert apps.stage_names() == ["scoped", "scoped"]
    assert apps.stages[-1]["detail"] == {
        "turn": 1, "halted": "error",
        "reason": "the turn ended without a result"}
    (line,) = apps.ledger()
    assert line["synthesized"] is True
    assert line["exit"] == 143
    assert line["tokens"] == 0
    assert apps.broker._active_apps == {}
    assert apps.sidecars() == []


def test_a_result_that_never_becomes_json_is_the_same_halt(apps):
    proc = FakeAppsProc()
    apps.broker._apps_spawn = FakeAppsSpawn(lambda: proc)
    apps.write_result(raw="{not json")
    _handoff(apps)
    proc.finish(rc=0)
    apps.broker.join_apps(timeout=5)
    assert apps.stages[-1]["detail"]["halted"] == "error"
    assert apps.ledger()[0]["synthesized"] is True


def test_a_turn_past_its_deadline_is_halted_even_while_the_unit_lives(apps):
    proc = FakeAppsProc()
    apps.broker._apps_spawn = FakeAppsSpawn(lambda: proc)
    _handoff(apps)
    (path,) = apps.sidecars()
    rec = json.loads(path.read_text())
    # Re-adopt the same ticket with a deadline that has already passed: the
    # live turn's own reaper is still watching a process that never ends.
    rec["deadline"] = time.time() - 1
    apps.broker._apps_release(SESSION)
    apps.broker._reap_apps(rec, None)
    assert apps.stages[-1]["detail"]["reason"] == "the turn passed its deadline"


# ------------------------------------------------------------ the 2000 bound

def test_a_two_hundred_file_turn_still_fits_the_servers_detail_bound(apps):
    files = [f"src/components/some/deep/path/Component{i:03d}.tsx"
             for i in range(200)]
    apps.write_result(files=files, summary="S" * 900)
    _handoff(apps)
    apps.broker.join_apps(timeout=5)
    detail = apps.stages[-2]["detail"]
    assert len(json.dumps(detail)) <= 2000
    assert detail["turn"] == 1
    assert detail["files"][-1].endswith(" more")
    # Every file that is not listed is counted in the marker, exactly once.
    listed = [f for f in detail["files"] if not f.endswith(" more")]
    dropped = int(detail["files"][-1].split()[0].lstrip("+"))
    assert len(listed) + dropped == 200


def test_a_long_summary_is_cut_before_the_file_list_is(apps):
    apps.write_result(files=["a.js", "b.js"], summary="S" * 900)
    _handoff(apps)
    apps.broker.join_apps(timeout=5)
    detail = apps.stages[-2]["detail"]
    assert detail["files"] == ["a.js", "b.js"]
    assert len(detail["summary"]) == 300


def test_a_summary_is_stripped_of_control_characters(apps):
    apps.write_result(summary="line one\nline two\x07")
    _handoff(apps)
    apps.broker.join_apps(timeout=5)
    assert apps.stages[-2]["detail"]["summary"] == "line oneline two"


def test_an_older_result_without_summary_or_flag_still_publishes(apps):
    record = {k: v for k, v in RESULT.items()}
    apps.write_result(raw=json.dumps(record))
    _handoff(apps)
    apps.broker.join_apps(timeout=5)
    assert "files_written" in apps.stage_names()
    assert "summary" not in apps.stages[-2]["detail"]
    assert apps.narrations == []


# ---------------------------------------------------------- server failures

def test_a_stage_post_failure_is_audited_and_the_ledger_still_lands(apps):
    apps.view["_stage_status"] = 500
    apps.write_result()
    _handoff(apps)
    apps.broker.join_apps(timeout=5)
    assert len(apps.ledger()) == 1
    summaries = [e["result_summary"] for e in apps.audit_lines()]
    assert any(s.startswith("stage post failed:") for s in summaries)


def test_a_post_into_an_ended_session_is_not_retried(apps):
    apps.view["_stage_status"] = 410
    apps.write_result()
    _handoff(apps)
    apps.broker.join_apps(timeout=5)
    # scoped, files_written, deployed — each attempted exactly once.
    assert len(apps.stages) == 3


# ------------------------------------------------------------- the boot check

def test_a_seat_mapped_to_the_wrong_bot_disables_the_verb(tmp_path):
    """§B, Claudette #2293: a renumbering must never hand one resident's session
    to the other. Loud, scoped, and NOT a boot failure."""
    h = build_apps_harness(tmp_path, bots=((BOT_ID, "Claudette"),))
    try:
        assert "not 'gable'" in h.broker._apps_disabled_reason
        resp = _handoff(h)
        assert resp["error"]["code"] == "apps-refused"
        assert resp["error"]["message"].startswith(
            "apps-build is disabled: the seat map failed its boot check — ")
        assert h.spawn.calls == []
        # One audit line at boot, before any request.
        boot = h.audit_lines()[0]
        assert boot["resident"] == "broker" and boot["verb"] == "apps-build"
        assert boot["allowed"] is False
    finally:
        h.broker.shutdown()
        h._thread.join(timeout=5)


def test_a_seat_mapped_to_a_bot_that_does_not_exist_disables_the_verb(tmp_path):
    h = build_apps_harness(tmp_path, bots=((1, "Claudette"),))
    try:
        assert "does not exist on this server" in h.broker._apps_disabled_reason
    finally:
        h.broker.shutdown()
        h._thread.join(timeout=5)


def test_a_non_seat_key_in_the_map_disables_the_verb(tmp_path):
    h = build_apps_harness(tmp_path, seat_bots='{ plink = 2 }')
    try:
        assert "not a res-<name> resident seat" in h.broker._apps_disabled_reason
    finally:
        h.broker.shutdown()
        h._thread.join(timeout=5)


def test_a_passing_boot_check_says_nothing(apps):
    assert apps.broker._apps_disabled_reason is None
    assert apps.audit_lines() == []


def test_no_apps_table_means_the_verb_says_so_and_the_broker_still_serves(tmp_path):
    h = build_apps_harness(tmp_path, apps_table=False)
    try:
        resp = _handoff(h)
        assert resp["error"]["code"] == "apps-refused"
        assert resp["error"]["message"] == (
            "apps-build is not configured on this broker")
        # Every other verb is unaffected: an absent apps-builder is not an
        # absent broker.
        h.set_verbs(SEAT, **{v: True for v in ALL_VERBS})
        other = h.call("read-metrics", {})
        assert other["ok"] is False           # no metrics file in this scratch
        assert other["error"]["code"] == "exec-failure"
    finally:
        h.broker.shutdown()
        h._thread.join(timeout=5)


def test_the_writer_and_the_detector_resolve_the_same_database(apps):
    """PINNED ON BOTH SIDES, like the local coverage log: `[gate].message_db`,
    else <deploy_tree>/server/data/disjorn.db. Two rules for one deployment's
    database is how a boot check ends up reading a file nobody writes."""
    import importlib.util
    path = Path(__file__).resolve().parents[2] / "metrics" / "metrics.py"
    spec = importlib.util.spec_from_file_location("_metrics_for_apps_pin", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    for gate in ({"message_db": "/tmp/x.db"},
                 {"deploy_tree": "/srv/disjorn"},
                 {"canonical_repo": "/srv/g.git", "deploy_tree": "/srv/disjorn"}):
        apps.broker.config["gate"] = gate
        assert apps.broker._apps_message_db() == mod.gate_paths(
            {"gate": {**gate, "mirror": "/nowhere"}})["message_db"]


# ------------------------------------------------------ adoption on restart

def test_a_turn_in_flight_is_re_adopted_after_a_restart(apps):
    """The unit lives outside this daemon's cgroup, so a restart no longer kills
    a turn — but its reaper died with the old process. Without adoption the turn
    finishes into a result.json nobody reads."""
    proc = FakeAppsProc()
    apps.broker._apps_spawn = FakeAppsSpawn(lambda: proc)
    _handoff(apps)
    assert apps.sidecars()
    # The old process goes away mid-turn: its claim and its reaper with it.
    apps.broker._apps_release(SESSION)
    apps.write_result()
    adopted = apps.broker.adopt_inflight_apps()
    assert adopted == ["disjorn-apps-12-1.service"]
    apps.broker.join_apps(timeout=5)
    assert "files_written" in apps.stage_names()
    assert apps.ledger()[0]["turn"] == 1
    assert apps.sidecars() == []
    assert apps.broker._active_apps == {}


def test_adoption_re_claims_the_session_so_a_duplicate_handoff_is_refused(apps):
    proc = FakeAppsProc()
    apps.broker._apps_spawn = FakeAppsSpawn(lambda: proc)
    _handoff(apps)
    apps.broker._apps_release(SESSION)
    apps.broker.adopt_inflight_apps()
    assert _handoff(apps)["error"]["message"] == (
        "a turn is already running for this session")


def test_a_ticket_this_process_already_owns_is_left_alone(apps):
    proc = FakeAppsProc()
    apps.broker._apps_spawn = FakeAppsSpawn(lambda: proc)
    _handoff(apps)
    assert apps.broker.adopt_inflight_apps() == []
    assert apps.broker._active_apps == {SESSION: 1}


def test_an_unreadable_ticket_is_swept(apps):
    junk = apps.tmp_path / "apps-logs" / "9-9.apps.json"
    junk.write_text("{not json")
    mislabelled = apps.tmp_path / "apps-logs" / "7-1.apps.json"
    mislabelled.write_text(json.dumps({"session": 8, "turn": 1}))
    assert apps.broker.adopt_inflight_apps() == []
    assert not junk.exists() and not mislabelled.exists()
