"""The `build` verb: a human's `/build` message, read by the broker itself."""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from brokerd import Broker, ConfigError  # noqa: E402
from broker_testlib import harness  # noqa: E402,F401

CHANNEL = 7
SESSION = 12
SEQ = 100
TEXT = "/build fix the login typo on the settings page"
SLUG_STEM = "fix-the-login-typo-on"


def arm(h, *, content: str = TEXT, author: str = "plink",
        author_type: str = "user", flags: str = "{}", seq: int = SEQ,
        message: bool = True):
    """A `server` caller, the verb switched on, and the message in the DB."""
    h.become_server()
    h.set_verbs("server", build=True)
    if message:
        h.add_message(CHANNEL, seq, content, author=author,
                      author_type=author_type, flags=flags)
    return h.use_fake_build()


def call(h, *, seq: int = SEQ, channel: int = CHANNEL, session: int = SESSION):
    return h.call("build", {"seq": seq, "channel_id": channel,
                            "session_id": session})


def git(repo: Path, *args: str) -> None:
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    subprocess.run(["git", "-C", str(repo), *args], check=True, env=env,
                   capture_output=True)


def make_gatehouse(h, *branches: str) -> Path:
    """A real repo at [gate].canonical_repo, with `main` and the given branches."""
    repo = h.gatehouse
    repo.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "-C", str(repo), "init", "-q", "-b", "main"],
                   check=True, capture_output=True)
    (repo / "a.txt").write_text("one\n")
    git(repo, "add", "a.txt")
    git(repo, "commit", "-q", "-m", "init")
    for branch in branches:
        git(repo, "checkout", "-q", "-b", branch)
        (repo / "a.txt").write_text("one\ntwo\n")
        git(repo, "commit", "-q", "-am", branch)
        git(repo, "checkout", "-q", "main")
    return repo


def stages(h) -> list[tuple[str, dict]]:
    return [(c["payload"]["stage"], c["payload"]["detail"])
            for c in h.planroom_calls if c["path"].endswith("/stage")]


# ── identity ─────────────────────────────────────────────────────────────

def test_the_server_principal_is_the_cgroup_and_not_the_uid(harness):
    """Same uid as plink at the keyboard; only the peer's cgroup separates them."""
    uid = os.getuid()
    harness.broker.uid_map[uid] = "plink"
    harness.broker.server_unit = "no-such-unit.service"
    assert harness.broker._caller_identity(uid, os.getpid()) == "plink"
    path = Path(f"/proc/{os.getpid()}/cgroup").read_text().strip().rpartition(":")[2]
    if path in ("", "/"):
        pytest.skip("this process is in the root cgroup")
    harness.broker.server_unit = path
    assert harness.broker._caller_identity(uid, os.getpid()) == "server"


def test_a_resident_uid_is_never_the_server(harness):
    harness.broker.server_unit = "disjorn-test.service"
    harness.broker._read_peer_cgroup = lambda pid: "0::/system.slice/disjorn-test.service\n"
    assert harness.broker._caller_identity(os.getuid(), os.getpid()) == "res-test"


def test_the_unit_must_sit_under_system_slice(harness):
    harness.become_server()
    harness.broker._read_peer_cgroup = lambda pid: "0::/user.slice/disjorn-test.service\n"
    assert harness.broker._caller_identity(os.getuid(), os.getpid()) == "plink"
    harness.broker._read_peer_cgroup = lambda pid: "0::/system.slice/x-disjorn-test.service\n"
    assert harness.broker._caller_identity(os.getuid(), os.getpid()) == "plink"


@pytest.mark.parametrize("text", [
    "/build ../../etc/passwd", "/build -rf / --force", "/build \"quoted\" 'words' here",
    "/build fix/the/thing with spaces", "/build ünïcödé wörds ünd mehr",
    "/build " + "!?*&^%$#@" * 400,
])
def test_free_text_only_ever_yields_a_safe_slug_or_a_refusal(harness, text):
    arm(harness, content=text)
    resp = harness.call("build", {"seq": SEQ, "channel_id": CHANNEL, "session_id": SESSION})
    if resp["ok"]:
        stem = resp["result"]["slug"][11:]
        assert re.fullmatch(r"[0-9a-z][0-9a-z-]*", stem), resp["result"]["slug"]
        assert ".." not in resp["result"]["branch"] and "/" not in stem
    else:
        assert resp["error"]["code"] == "build-refused"


def test_text_with_no_usable_word_is_refused_not_minted(harness):
    arm(harness, content="/build !!! ??? ...")
    resp = call(harness)
    assert resp["error"]["code"] == "build-refused"
    assert "at least one word" in resp["error"]["message"]


def test_the_privacy_vocabulary_matches_the_servers(harness):
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "server_privacy", Path(__file__).resolve().parents[3] / "server" / "app" / "privacy.py")
    privacy = importlib.util.module_from_spec(spec); spec.loader.exec_module(privacy)
    import brokerd
    assert tuple(brokerd.BOT_HIDDEN_FLAGS) == tuple(privacy.BOT_HIDDEN_FLAGS)
    for flag in privacy.BOT_HIDDEN_FLAGS:
        assert brokerd.hidden_from_bots({flag: True}) == privacy.hidden_from_bots({flag: True}) is True
    assert brokerd.hidden_from_bots({"other": True}) == privacy.hidden_from_bots({"other": True}) is False


def test_a_deleted_message_is_refused(harness):
    harness.become_server()
    harness.set_verbs("server", build=True)
    harness.add_message(CHANNEL, SEQ, TEXT, deleted="2026-01-01T00:00:00Z")
    harness.use_fake_build()
    resp = call(harness)
    assert resp["error"]["code"] == "build-refused"
    assert "deleted" in resp["error"]["message"]


# ── the ten steps, refusal by refusal ────────────────────────────────────

def test_a_bot_authored_message_is_refused(harness):
    arm(harness, author="gable", author_type="bot")
    resp = call(harness)
    assert resp["error"]["code"] == "build-refused"
    assert "only a person" in resp["error"]["message"]


def test_a_privacy_flagged_message_is_refused(harness):
    arm(harness, flags='{"off_the_record": true}')
    resp = call(harness)
    assert resp["error"]["code"] == "build-refused"
    assert "private" in resp["error"]["message"]


def test_an_author_not_on_the_human_list_is_refused(harness):
    arm(harness, author="stranger")
    resp = call(harness)
    assert resp["error"]["code"] == "build-refused"
    assert "human list" in resp["error"]["message"]


def test_an_unknown_seq_is_refused(harness):
    arm(harness, message=False)
    resp = call(harness, seq=999)
    assert resp["error"]["code"] == "build-refused"
    assert "no message 999" in resp["error"]["message"]


def test_a_message_with_no_request_in_it_is_refused(harness):
    arm(harness, content="/build")
    resp = call(harness)
    assert resp["error"]["code"] == "build-refused"
    assert "something to build" in resp["error"]["message"]


def test_over_long_text_is_refused(harness):
    arm(harness, content="/build " + "x" * 4001)
    assert call(harness)["error"]["code"] == "build-refused"


@pytest.mark.parametrize("args", [
    {"seq": SEQ, "channel_id": CHANNEL},
    {"seq": SEQ, "channel_id": CHANNEL, "session_id": SESSION, "tier": 0},
    {"seq": 0, "channel_id": CHANNEL, "session_id": SESSION},
])
def test_the_arg_schema_is_exactly_three_positive_ints(harness, args):
    arm(harness)
    assert harness.call("build", args)["error"]["code"] == "bad-args"


def test_every_denial_is_audited(harness):
    arm(harness, author="stranger")
    call(harness)
    entry = harness.audit_lines()[-1]
    assert entry["verb"] == "build" and entry["allowed"] is False
    assert entry["result_summary"].startswith("denied: ")
    assert entry["resident"] == "server"


def test_a_refused_build_never_spawns(harness):
    spawn = arm(harness, author="stranger")
    call(harness)
    assert spawn.calls == []


# ── the success path ─────────────────────────────────────────────────────

def test_a_chat_build_launches_through_the_start_build_path(harness):
    spawn = arm(harness)
    resp = call(harness)
    assert resp["ok"] is True
    result = resp["result"]
    assert result["started"] is True
    assert result["session_id"] == SESSION
    assert result["slug"].endswith(SLUG_STEM)
    assert result["branch"] == f"loop/{result['slug']}"
    assert result["pid"] == spawn.procs[0].pid
    # Same argv shape as a spec build: the SEAT, the slug, the model pin.
    argv = spawn.calls[0]
    assert argv[3:5] == ["test", result["slug"]]
    assert argv[-2:] == ["--model", "claude-opus-4-8"]
    harness.broker.join_builds()


def test_the_prompt_on_stdin_names_the_branch_and_has_no_spec(harness):
    spawn = arm(harness)
    resp = call(harness)
    harness.broker.join_builds()
    prompt = spawn.procs[0].stdin_written.decode()
    assert resp["result"]["branch"] in prompt
    assert "NO SPEC FILE" in prompt
    assert "fix the login typo" in prompt


def test_the_slug_takes_five_words_and_today(harness):
    arm(harness)
    import datetime as dt
    today = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")
    assert call(harness)["result"]["slug"] == f"{today}-{SLUG_STEM}"
    harness.broker.join_builds()


def test_a_slug_already_in_the_gatehouse_gets_a_suffix(harness):
    import datetime as dt
    today = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")
    make_gatehouse(harness, f"loop/{today}-{SLUG_STEM}")
    arm(harness)
    assert call(harness)["result"]["slug"] == f"{today}-{SLUG_STEM}-2"
    harness.broker.join_builds()


def test_the_ledger_records_who_asked_for_what(harness):
    arm(harness)
    result = call(harness)["result"]
    harness.broker.join_builds()
    line = harness.build_ledger_lines()[-1]
    assert line["seq"] == SEQ and line["channel_id"] == CHANNEL
    assert line["author"] == "plink" and line["session_id"] == SESSION
    assert line["slug"] == result["slug"]
    assert len(line["text_sha256"]) == 64
    assert line["ts"]


def test_the_stages_go_to_the_session_and_the_banner_to_the_channel(harness):
    make_gatehouse(harness)
    harness.stub_gates()
    arm(harness)
    result = call(harness)["result"]
    harness.broker.join_builds()
    posted = stages(harness)
    assert posted[0][0] == "scoped" and posted[0][1]["turn"] == 1
    assert posted[0][1]["model"] == "claude-opus-4-8"
    assert [p[0] for p in posted[1:]] == ["files_written", "deployed"]
    assert posted[2][1]["branch"] == result["branch"]
    assert posted[2][1]["sha"]
    assert {c["path"] for c in harness.planroom_calls} == {
        f"/apps/sessions/{SESSION}/stage",
        f"/apps/sessions/{SESSION}/harness-view"}

    assert len(harness.channel_posts) == 1
    post = harness.channel_posts[0]
    assert post["channel_id"] == CHANNEL
    lines = post["body"].splitlines()
    assert len(lines) == 4
    assert lines[0].startswith("tests: ")
    assert lines[1].startswith("tier: ")
    assert lines[2] == "diffstat: no commits"
    assert lines[3].startswith("next: ")


def test_a_seq_started_build_posts_nothing_in_custodian(harness):
    arm(harness)
    call(harness)
    harness.broker.join_builds()
    assert harness.proposals == []


def test_the_diffstat_is_read_out_of_the_gatehouse(harness):
    make_gatehouse(harness, "loop/landed")
    assert "1 file changed" in harness.broker._gatehouse_diffstat("loop/landed")
    assert harness.broker._gatehouse_diffstat("loop/never") == "no commits"


def test_a_failed_build_halts_the_session(harness):
    from broker_testlib import FakeBuildProc, build_out
    arm(harness)
    harness.use_fake_build(
        lambda: FakeBuildProc(out=build_out(publish=""), rc=1, err=b"boom"))
    call(harness)
    harness.broker.join_builds()
    halted = [d for s, d in stages(harness) if d.get("halted")]
    assert halted and halted[0]["turn"] == 1 and halted[0]["reason"]
    assert len(harness.channel_posts) == 1


def test_a_chat_build_spends_the_servers_daily_budget(harness):
    arm(harness)
    harness.add_message(CHANNEL, SEQ + 1, "/build second thing")
    harness.add_message(CHANNEL, SEQ + 2, "/build third thing")
    assert call(harness)["ok"] is True
    assert call(harness, seq=SEQ + 1)["ok"] is True
    resp = call(harness, seq=SEQ + 2)
    assert resp["error"]["code"] == "build-refused"
    assert resp["error"]["message"] == "today's chat build budget (2) is spent"
    harness.broker.join_builds()


# ── boot validation ──────────────────────────────────────────────────────

def fresh(harness, **build_cfg) -> Broker:
    config = {**harness.broker.config,
              "build": {**harness.broker.config["build"], **build_cfg}}
    return Broker(config, str(harness.verbs_path), transport=lambda cfg, b: {})


@pytest.mark.parametrize("cfg,fragment", [
    ({"humans": ["plink", "res-gable"]}, "resident seat"),
    ({"humans": "plink"}, "build.humans"),
    ({"seat": "res-test"}, "WITHOUT"),
    ({"seat": "nobody"}, "not a resident"),
    ({"ledger": ""}, "build.ledger"),
])
def test_a_bad_build_block_refuses_to_start(harness, cfg, fragment):
    with pytest.raises(ConfigError) as ei:
        fresh(harness, **cfg)
    assert fragment in str(ei.value)


def test_an_empty_human_list_boots_and_refuses_everything(harness):
    broker = fresh(harness, humans=[])
    assert broker.build_humans == frozenset()


def test_an_empty_server_unit_refuses_to_start(harness):
    config = {**harness.broker.config, "server": {"unit": "  "}}
    with pytest.raises(ConfigError) as ei:
        Broker(config, str(harness.verbs_path), transport=lambda cfg, b: {})
    assert "[server].unit" in str(ei.value)


# ── the session is the server's word, not the caller's ───────────────────

def view(h, **fields) -> None:
    h.planroom_state["sessions"][SESSION] = {
        **{"open": True, "mode": "repo", "owner_username": "plink"}, **fields}


def test_the_session_is_checked_before_anything_launches(harness):
    spawn = arm(harness)
    view(harness, owner_username="someone-else")
    resp = call(harness)
    assert resp["error"]["code"] == "build-refused"
    assert resp["error"]["message"] == f"session {SESSION} does not belong to plink"
    assert spawn.calls == []
    assert harness.build_ledger_lines() == []


def test_a_closed_session_is_refused(harness):
    arm(harness)
    view(harness, open=False)
    assert "has ended" in call(harness)["error"]["message"]


def test_an_app_mode_session_is_refused(harness):
    arm(harness)
    view(harness, mode="app")
    assert "is not a repo build" in call(harness)["error"]["message"]


def test_a_session_the_server_does_not_know_is_refused(harness):
    arm(harness)
    harness.planroom_state["sessions"][SESSION] = False
    resp = call(harness)
    assert resp["error"]["code"] == "build-refused"
    assert "cannot be read" in resp["error"]["message"]


def test_an_owned_open_repo_session_launches(harness):
    arm(harness)
    view(harness)
    assert call(harness)["ok"] is True
    harness.broker.join_builds()


def test_a_chat_build_leaves_the_build_seats_own_allowance_alone(harness):
    """The seat's [start_build] cap is 2 and it is not what a chat build spends."""
    arm(harness)
    call(harness)
    harness.broker.join_builds()
    today = __import__("datetime").datetime.now(
        __import__("datetime").timezone.utc).strftime("%Y-%m-%d")
    assert harness.broker._count_builds_today("res-test", today) == 0
    assert harness.broker._count_builds_today("server", today) == 1
    assert harness.audit_lines()[-1]["build_seat"] == "res-test"
