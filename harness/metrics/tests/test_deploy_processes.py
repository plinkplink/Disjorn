"""Per-process running facts behind the deploy badge: start times and hashes
are injected, real git repos sit in tmp_path, systemd is never asked."""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

import metrics as M
from conftest import REAL_DEPLOY_PROCESSES

LANDED = 1787216400
ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "keyboard", "GIT_AUTHOR_EMAIL": "k@example.invalid",
    "GIT_COMMITTER_NAME": "keyboard", "GIT_COMMITTER_EMAIL": "k@example.invalid",
    "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null",
}
SERVER = {"unit": "disjorn", "watch": ["server", ":(exclude,glob)**/tests/**"]}
BROKER = REAL_DEPLOY_PROCESSES["broker"]


def git(cwd, *args, at=LANDED) -> str:
    stamp = f"@{at} +0000"
    env = {**ENV, "GIT_AUTHOR_DATE": stamp, "GIT_COMMITTER_DATE": stamp}
    return subprocess.run(["git", *args], cwd=str(cwd), env=env, check=True,
                          capture_output=True, text=True).stdout.strip()


def commit(repo: Path, rel: str, text="x = 1\n", at=LANDED) -> str:
    p = repo / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)
    git(repo, "add", "-A", at=at)
    git(repo, "commit", "-q", "-m", f"touch {rel}", at=at)
    return git(repo, "rev-parse", "HEAD")


@pytest.fixture()
def prod(tmp_path) -> Path:
    repo = tmp_path / "prod"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    commit(repo, "server/app/main.py")
    commit(repo, "harness/broker/brokerd.py")
    return repo


def started(monkeypatch, **at):
    monkeypatch.setattr(M, "service_started_at",
                        lambda unit: (at[unit], "") if unit in at else (None, "down"))


def probe(spec, tree, name="p"):
    return M.process_state(name, spec, str(tree))


def test_an_uncommitted_edit_under_a_watched_path_is_unknown_never_running(
        prod, monkeypatch):
    started(monkeypatch, disjorn=LANDED + 60)
    (prod / "server" / "app" / "main.py").write_text("x = 2\n")
    run = probe(SERVER, prod)
    assert (run["ok"], run["state"]) == (None, "unknown")
    assert run["detail"] == "uncommitted edits under server/app/main.py"


def test_an_untracked_file_under_a_watched_path_also_blocks_green(prod, monkeypatch):
    started(monkeypatch, disjorn=LANDED + 60)
    (prod / "server" / "app" / "hotfix.py").write_text("x = 1\n")
    assert probe(SERVER, prod)["ok"] is None


def test_an_edit_outside_the_watched_paths_leaves_running_alone(prod, monkeypatch):
    started(monkeypatch, disjorn=LANDED + 60)
    (prod / "client").mkdir()
    (prod / "client" / "x.ts").write_text("1\n")
    (prod / "server" / "tests").mkdir()
    (prod / "server" / "tests" / "test_x.py").write_text("1\n")
    assert probe(SERVER, prod)["ok"] is True


def test_a_metrics_landing_makes_the_broker_stale_and_spares_the_server(
        prod, monkeypatch):
    commit(prod, "harness/metrics/metrics.py", at=LANDED + 3600)
    started(monkeypatch, **{"disjorn": LANDED + 60, "disjorn-broker": LANDED + 60})
    procs = {n: M.process_state(n, s, str(prod))
             for n, s in {"server": SERVER, "broker": BROKER}.items()}
    assert procs["server"]["ok"] is True
    assert procs["broker"]["state"] == "stale"
    assert "harness/metrics/metrics.py changed since" in procs["broker"]["detail"]


@pytest.mark.parametrize("rel,ok", [
    ("harness/keyboard/board.py", False),
    ("harness/planroom/planroom.py", False),
    ("harness/broker/tests/test_x.py", True),
    ("harness/keyboard/other.py", True),
])
def test_the_broker_watches_what_it_imports_and_not_its_tests(
        prod, monkeypatch, rel, ok):
    commit(prod, rel, at=LANDED + 3600)
    started(monkeypatch, **{"disjorn-broker": LANDED + 60})
    assert probe(BROKER, prod)["ok"] is ok


def test_one_failing_probe_never_takes_the_others_down(prod, monkeypatch):
    real = M._probe
    monkeypatch.setattr(M, "_probe", lambda spec, tree: (
        1 / 0 if spec["unit"] == "boom" else real(spec, tree)))
    started(monkeypatch, disjorn=LANDED + 60)
    d = [M.process_state(n, s, str(prod)) for n, s in
         {"boom": {"unit": "boom"}, "server": SERVER}.items()]
    assert d[0]["state"] == "unknown" and "ZeroDivisionError" in d[0]["detail"]
    assert d[1]["ok"] is True


def test_an_unreadable_checkout_is_unknown_with_its_reason(prod, monkeypatch, tmp_path):
    started(monkeypatch, claudette=LANDED + 60)
    run = probe({"unit": "claudette", "repo": str(tmp_path / "gone")}, prod)
    assert run["ok"] is None and "cannot read" in run["detail"]


# -- a clone that must equal its upstream branch -------------------------------

@pytest.fixture()
def clone(tmp_path):
    up = tmp_path / "upstream"
    up.mkdir()
    git(up, "init", "-q", "-b", "disjorn-port")
    commit(up, "core.py")
    mine = tmp_path / "clone"
    git(tmp_path, "clone", "-q", str(up), str(mine), at=LANDED + 600)
    spec = {"unit": "resident-cc", "user": "res-x", "repo": str(mine),
            "upstream": str(up), "ref": "disjorn-port",
            "watch": REAL_DEPLOY_PROCESSES["custodian-adapter"]["watch"]}
    return up, mine, spec


def user_started(monkeypatch, at):
    monkeypatch.setattr(M, "user_unit_started_at",
                        lambda user, unit: (at, "") if at else (None, "hidden"))


def test_a_clone_at_its_upstream_and_started_after_its_pull_is_running(
        clone, monkeypatch, prod):
    user_started(monkeypatch, LANDED + 900)
    assert probe(clone[2], prod)["ok"] is True


def test_a_clone_behind_its_upstream_reads_differs(clone, monkeypatch, prod):
    up, _, spec = clone
    commit(up, "core.py", "x = 2\n", at=LANDED + 700)
    user_started(monkeypatch, LANDED + 900)
    run = probe(spec, prod)
    assert (run["ok"], run["state"]) == (False, "differs")
    assert "disjorn-port is at" in run["detail"]


def test_a_spine_note_pulled_after_the_start_needs_no_restart(clone, monkeypatch, prod):
    up, mine, spec = clone
    commit(up, "spine/note.md", "n\n", at=LANDED + 700)
    git(mine, "pull", "-q", "--ff-only", at=LANDED + 1200)
    user_started(monkeypatch, LANDED + 900)
    assert probe(spec, prod)["ok"] is True


def test_a_clone_whose_start_cannot_be_read_is_unknown(clone, monkeypatch, prod):
    user_started(monkeypatch, None)
    run = probe(clone[2], prod)
    assert (run["ok"], run["detail"]) == (None, "hidden")


# -- a deployed copy outside the repo -----------------------------------------

@pytest.fixture()
def copy(prod, tmp_path):
    commit(prod, "harness/residency/adapter.py", "a = 1\n")
    dest = tmp_path / "deployed"
    shutil.copytree(prod / "harness" / "residency", dest)
    spec = {"unit": "gable-summon", "user": "res-x", "copy": str(dest),
            "source": "harness/residency",
            "watch": ["harness/residency", ":(exclude,glob)**/tests/**"]}
    return dest, spec


def test_a_copy_equal_to_the_repo_and_older_than_the_start_is_running(
        copy, prod, monkeypatch):
    user_started(monkeypatch, int(time.time()) + 60)
    assert probe(copy[1], prod)["ok"] is True


def test_a_copy_installed_after_the_start_is_a_restart_pending(copy, prod, monkeypatch):
    user_started(monkeypatch, LANDED)
    run = probe(copy[1], prod)
    assert (run["ok"], run["state"]) == (False, "stale")


def test_a_copy_that_differs_from_the_repo_is_its_own_state(copy, prod, monkeypatch):
    (copy[0] / "adapter.py").write_text("a = 2\n")
    user_started(monkeypatch, int(time.time()) + 60)
    run = probe(copy[1], prod)
    assert (run["ok"], run["state"]) == (False, "differs")
    assert run["detail"] == "deployed copy differs from repo at adapter.py"


def test_tests_and_caches_in_the_copy_are_not_differences(copy, prod, monkeypatch):
    for junk in ("tests/test_a.py", "__pycache__/adapter.cpython-313.pyc",
                 ".pytest_cache/v"):
        (copy[0] / junk).parent.mkdir(parents=True, exist_ok=True)
        (copy[0] / junk).write_text("junk\n")
    user_started(monkeypatch, int(time.time()) + 60)
    assert probe(copy[1], prod)["ok"] is True


def test_a_missing_copy_is_unknown(copy, prod, monkeypatch):
    shutil.rmtree(copy[0])
    user_started(monkeypatch, int(time.time()) + 60)
    run = probe(copy[1], prod)
    assert run["ok"] is None and "cannot list" in run["detail"]


# -- another account's user unit, from its cgroup and /proc --------------------

def fake_host(tmp_path, monkeypatch, procs: dict, unit="gable-summon"):
    cg = (tmp_path / "cg" / "user.slice" / "user-996.slice" / "user@996.service"
          / "app.slice" / f"{unit}.service")
    cg.mkdir(parents=True)
    (cg / "cgroup.procs").write_text("".join(f"{p}\n" for p in procs))
    proc = tmp_path / "proc"
    proc.mkdir()
    (proc / "stat").write_text("cpu 1 2 3\nbtime 1790000000\n")
    for pid, ticks in procs.items():
        if ticks is not None:
            (proc / str(pid)).mkdir()
            fields = " ".join(["S"] + ["0"] * 18 + [str(ticks)] + ["0"] * 10)
            (proc / str(pid) / "stat").write_text(f"{pid} (py (x)) {fields}\n")
    monkeypatch.setattr(M, "CGROUP_ROOT", str(tmp_path / "cg"))
    monkeypatch.setattr(M, "PROC_ROOT", str(proc))
    monkeypatch.setattr(os, "sysconf", lambda name: 100)
    import pwd
    monkeypatch.setattr(pwd, "getpwnam", lambda u: (
        type("P", (), {"pw_uid": 996}) if u == "res-gable" else
        (_ for _ in ()).throw(KeyError(u))))


REAL_USER_UNIT_STARTED_AT = M.user_unit_started_at


def test_a_user_unit_started_when_its_earliest_process_did(tmp_path, monkeypatch):
    fake_host(tmp_path, monkeypatch, {"41": 50000, "7": 30000, "99": None})
    assert REAL_USER_UNIT_STARTED_AT("res-gable", "gable-summon") == (1790000300, "")


@pytest.mark.parametrize("user,unit,why", [
    ("res-nobody", "gable-summon", "no account res-nobody"),
    ("res-gable", "resident-cc", "resident-cc is not running under res-gable"),
])
def test_a_user_unit_that_cannot_be_found_says_why(tmp_path, monkeypatch, user, unit, why):
    fake_host(tmp_path, monkeypatch, {"41": 50000})
    assert REAL_USER_UNIT_STARTED_AT(user, unit) == (None, why)


def test_a_user_unit_whose_processes_cannot_be_read_is_unknown(tmp_path, monkeypatch):
    fake_host(tmp_path, monkeypatch, {"41": None})
    assert REAL_USER_UNIT_STARTED_AT("res-gable", "gable-summon")[0] is None


# -- the table, the summary, the digest ----------------------------------------

def test_the_default_table_measures_five_processes():
    assert {n: (s["unit"], s.get("user")) for n, s in REAL_DEPLOY_PROCESSES.items()} == {
        "server": ("disjorn", None), "broker": ("disjorn-broker", None),
        "gable-summon": ("gable-summon", "res-gable"),
        "custodian-adapter": ("resident-cc", "res-claudette"),
        "custodian-discord": ("claudette", None)}


def test_the_discord_side_bot_is_labelled_as_one_wherever_it_is_named(monkeypatch):
    monkeypatch.setattr(M, "_probe", lambda spec, tree: {"state": "stale"})
    p = M.process_state("custodian-discord",
                        REAL_DEPLOY_PROCESSES["custodian-discord"], "/nonexistent")
    assert p["label"] == "custodian-discord (Discord bot)"
    assert M.running_summary([p])["detail"] == (
        "running: restart pending: custodian-discord (Discord bot)")
    assert M.process_state("broker", {"unit": "x"}, "/nonexistent")["label"] == "broker"


def test_broker_toml_adds_overrides_and_drops_processes_by_name(monkeypatch):
    monkeypatch.setattr(M, "DEPLOY_PROCESSES", REAL_DEPLOY_PROCESSES)
    table = M.deploy_processes({"deploy": {"processes": {
        "broker": {"unit": "broker-staging"},
        "gable-summon": {"enabled": False},
        "apps-gate": {"unit": "disjorn-apps-gate", "watch": ["harness/cc/apps"]}}}},
        service="disjorn-staging")
    assert table["server"]["unit"] == "disjorn-staging"
    assert table["broker"]["unit"] == "broker-staging"
    assert table["broker"]["watch"] == BROKER["watch"]
    assert "gable-summon" not in table and "apps-gate" in table


def fact(name, state):
    return {"name": name, "state": state}


@pytest.mark.parametrize("states,ok,detail", [
    ([("server", "current"), ("broker", "current")], True, "running: all 2 current"),
    ([("server", "current"), ("broker", "stale"), ("gable", "differs"),
      ("adapter", "unknown")], False,
     "running: restart pending: broker; copy differs: gable; unknown: adapter"),
    ([("server", "current"), ("adapter", "unknown")], None, "running: unknown: adapter"),
    ([], None, "running: unknown"),
])
def test_green_needs_every_process_current_and_the_rest_are_named(states, ok, detail):
    s = M.running_summary([fact(n, st) for n, st in states])
    assert (s["ok"], s["detail"]) == (ok, detail)


def test_deploy_state_measures_every_configured_process(prod, tmp_path, monkeypatch):
    mirror = tmp_path / "mirror"
    git(tmp_path, "clone", "-q", "--bare", str(prod), str(mirror))
    commit(prod, "harness/metrics/metrics.py", at=LANDED + 3600)
    git(mirror, "fetch", "-q", str(prod), "main:main")
    started(monkeypatch, **{"disjorn": LANDED + 60, "disjorn-broker": LANDED + 60})
    d = M.deploy_state(mirror=str(mirror), deploy_tree=str(prod),
                       processes={"server": SERVER, "broker": BROKER})
    assert [(p["name"], p["ok"]) for p in d["processes"]] == [
        ("server", True), ("broker", False)]
    assert d["running"]["stale"] == ["broker"]
