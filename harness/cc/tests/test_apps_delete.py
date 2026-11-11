"""The launcher's `delete` mode (SPECS/2026-11-11-apps-delete.md): root's half
is an argv and touches no file; the seat's half removes one app's trees."""

from __future__ import annotations

import importlib.util
import json
import os
import re
import subprocess
import sys
from importlib.machinery import SourceFileLoader
from pathlib import Path

import pytest

CC_DIR = Path(__file__).resolve().parent.parent
LAUNCH = CC_DIR / "apps" / "disjorn-apps-launch"
SUDOERS = CC_DIR.parent / "keyboard" / "92-disjorn-apps.sudoers"
SELF = "/usr/local/lib/disjorn/disjorn-apps-launch"

APP_A = "abc234567xyz"
APP_B = "zzz234567abc"
BAD_IDS = ["", "aaaaaaaaaaa", "aaaaaaaaaaaaa", "ABCDEFGHIJKL", "abcdefghijk0",
           "abcdefghij-k", "../../etc/pa", "aaaaaaaaaaa/", "aaaa aaaaaaa",
           "aaaaaaaaaaa\n", "--uid=0", "aaaaaaaaaaaa;id", ".staging"]


def load(path: Path, name: str):
    loader = SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def launcher():
    return load(LAUNCH, "disjorn_apps_launch_delete")


def plant_turn(turns: Path, session: int, turn: int, app_id: str | None,
               record: str = "result.json") -> Path:
    turn_dir = turns / str(session) / str(turn)
    turn_dir.mkdir(parents=True)
    (turn_dir / "prompt.md").write_text("build it\n", encoding="utf-8")
    if app_id is not None:
        (turn_dir / record).write_text(json.dumps({"app_id": app_id}),
                                       encoding="utf-8")
    return turn_dir


def plant_app(srv: dict, app_id: str) -> None:
    for root in ("preview", "live", "live.prev"):
        tree = srv["www"] / app_id / root
        tree.mkdir(parents=True)
        (tree / "index.html").write_text(f"<h1>{app_id}</h1>\n", encoding="utf-8")
    staged = srv["www"] / ".staging" / app_id / "live.tmp.1"
    staged.mkdir(parents=True)
    (staged / "index.html").write_text("half\n", encoding="utf-8")
    work = srv["apps"] / app_id
    (work / "src").mkdir(parents=True)
    (work / "src" / "app.js").write_text("1\n", encoding="utf-8")
    (work / "link").symlink_to(srv["outside"])
    git_dir = srv["git"] / f"{app_id}.git"
    (git_dir / "objects").mkdir(parents=True)
    (git_dir / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    quarantined = srv["quarantine"] / app_id / "1"
    quarantined.mkdir(parents=True)
    (quarantined / "evidence").write_text("kept\n", encoding="utf-8")


@pytest.fixture()
def srv(tmp_path):
    """A scratch /srv: two apps, their turns, and a tree outside every root."""
    roots = {name: tmp_path / f"apps-{name}" for name in
             ("www", "git", "turns", "quarantine")}
    roots["apps"] = tmp_path / "apps"
    roots["outside"] = tmp_path / "outside"
    for path in roots.values():
        path.mkdir()
    (roots["outside"] / "keep.txt").write_text("not an app's\n", encoding="utf-8")
    plant_app(roots, APP_A)
    plant_app(roots, APP_B)
    plant_turn(roots["turns"], 12, 1, APP_A)
    plant_turn(roots["turns"], 12, 2, APP_A, record=".harvest-input.json")
    plant_turn(roots["turns"], 13, 1, APP_B)
    plant_turn(roots["turns"], 14, 1, None)
    roots["idle"] = fake_systemctl(tmp_path, "idle", None)
    roots["turn of a"] = fake_systemctl(
        tmp_path, "busy-a", f"Disjorn apps turn 12/3 ({APP_A})")
    roots["turn of b"] = fake_systemctl(
        tmp_path, "busy-b", f"Disjorn apps turn 12/3 ({APP_B})")
    return roots


def fake_systemctl(tmp_path: Path, name: str, description: str | None) -> Path:
    """A systemctl that reports no apps unit, or one with this Description."""
    path = tmp_path / f"systemctl-{name}"
    listing = ('echo "disjorn-apps-12-3.service loaded active running x"'
               if description else "true")
    path.write_text(
        "#!/bin/sh\n"
        'case "$1" in\n'
        f"  list-units) {listing};;\n"
        f"  show) echo {(description or '')!r};;\n"
        "esac\n", encoding="utf-8")
    path.chmod(0o755)
    return path


def delete(launcher, srv, app_id=APP_A, *, systemctl="idle", **kw):
    return launcher.delete_app(srv["apps"], srv["www"], app_id,
                               git_root=srv["git"], turns_root=srv["turns"],
                               systemctl_bin=str(srv[systemctl]), **kw)


def snapshot(root: Path) -> dict:
    out = {}
    for path in sorted(root.rglob("*")):
        rel = str(path.relative_to(root))
        if path.is_symlink():
            out[rel] = ("link", os.readlink(path))
        elif path.is_file():
            out[rel] = ("file", path.read_bytes())
        else:
            out[rel] = ("dir", None)
    return out


def app_paths(srv: dict, app_id: str) -> list[Path]:
    return [srv["www"] / app_id, srv["www"] / ".staging" / app_id,
            srv["apps"] / app_id, srv["git"] / f"{app_id}.git"]


def program(srv, *args):
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "DISJORN_APPS_LAUNCH_DRY_RUN": "1",
        "DISJORN_APPS_LAUNCH_FAKE_SEAT": "997:/home/res-appsbuilding:res-appsbuilding",
        "DISJORN_APPS_LAUNCH_SELF": SELF,
        "DISJORN_APPS_LAUNCH_SYSTEMCTL": str(srv["idle"]),
        "DISJORN_APPS_LAUNCH_WWW_ROOT": str(srv["www"]),
        "DISJORN_APPS_LAUNCH_APPS_ROOT": str(srv["apps"]),
        "DISJORN_APPS_LAUNCH_GIT_ROOT": str(srv["git"]),
        "DISJORN_APPS_LAUNCH_TURNS_ROOT": str(srv["turns"]),
    }
    return subprocess.run([sys.executable, str(LAUNCH), *args],
                          capture_output=True, text=True, env=env)


# ---- root's half ------------------------------------------------------------

def test_delete_argv_byte_for_byte_and_root_removes_nothing(srv, tmp_path):
    before = snapshot(tmp_path)
    proc = program(srv, "delete", APP_A)
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout) == [
        "/usr/bin/systemd-run",
        f"--unit=disjorn-apps-delete-{APP_A}",
        f"--description=Disjorn apps delete {APP_A}",
        "--uid=res-appsbuilding",
        "--gid=res-appsbuilding",
        "--collect", "--wait", "--pipe", "--quiet",
        "--property=RuntimeMaxSec=100",
        "--property=MemoryMax=1G",
        "--property=LimitFSIZE=268435456",
        "--property=LimitCORE=0",
        "--property=TasksMax=64",
        "--setenv=HOME=/home/res-appsbuilding",
        "--",
        SELF, "delete-as-seat", APP_A,
    ]
    assert snapshot(tmp_path) == before


@pytest.mark.parametrize("mode", ["delete", "delete-as-seat"])
@pytest.mark.parametrize("args", [[bad] for bad in BAD_IDS] + [
    [], [APP_A, APP_B], [APP_A, "12"], [APP_A, "--uid=0"], [f"{APP_A} {APP_B}"],
    [APP_A, "/srv/apps-quarantine"],
])
def test_a_bad_id_or_a_second_argument_is_refused_before_anything_moves(
        srv, tmp_path, mode, args):
    before = snapshot(tmp_path)
    proc = program(srv, mode, *args)
    assert proc.returncode == 64, proc.stderr + proc.stdout
    assert proc.stderr.startswith("disjorn-apps-launch: REFUSED:")
    assert proc.stdout.strip() == ""
    assert snapshot(tmp_path) == before


def test_the_sudoers_alias_admits_one_app_id_and_nothing_else():
    text = SUDOERS.read_text(encoding="utf-8")
    rule = re.search(r"^Cmnd_Alias DISJORN_APPS_DELETE = \\\n\s*(\S.*)$", text,
                     re.M).group(1)
    program_path, pattern = rule.split(" ", 1)
    assert program_path == SELF
    assert pattern == "^delete [a-z2-7]{12}$"
    admits = re.compile(pattern)
    assert admits.search(f"delete {APP_A}")
    for joined in (f"delete {APP_A} 12", f"delete {APP_A} {APP_B}",
                   f"delete-as-seat {APP_A}", f"delete {APP_A} ", "delete",
                   f"delete ../{APP_A[:9]}", f"delete {APP_A.upper()}",
                   f"publish {APP_A} delete {APP_A}"):
        assert not admits.search(joined), joined
    assert "delete-as-seat" not in text.split("Cmnd_Alias")[-1]


@pytest.mark.skipif(not os.path.exists("/usr/sbin/visudo"),
                    reason="visudo not installed")
def test_the_sudoers_file_still_parses():
    proc = subprocess.run(["/usr/sbin/visudo", "-cf", str(SUDOERS)],
                          capture_output=True, text=True)
    assert proc.returncode == 0, proc.stdout + proc.stderr


# ---- the seat's half --------------------------------------------------------

def test_delete_removes_exactly_one_apps_paths(launcher, srv):
    untouched = {name: snapshot(srv[name]) for name in ("quarantine", "outside")}
    b_before = [snapshot(p) for p in app_paths(srv, APP_B)]

    out = delete(launcher, srv)

    turns = srv["turns"]
    assert out == {"mode": "delete", "app": APP_A, "removed": [
        str(p) for p in app_paths(srv, APP_A)
        + [turns / "12" / "1", turns / "12" / "2"]]}
    for path in app_paths(srv, APP_A):
        assert not os.path.lexists(path)
    assert not (turns / "12").exists()
    assert (srv["www"] / ".staging").is_dir()
    assert [snapshot(p) for p in app_paths(srv, APP_B)] == b_before
    assert (turns / "13" / "1" / "result.json").is_file()
    assert (turns / "14" / "1" / "prompt.md").is_file()
    assert {name: snapshot(srv[name]) for name in untouched} == untouched


def test_delete_is_idempotent_and_an_unknown_app_is_already_deleted(
        launcher, srv, tmp_path):
    delete(launcher, srv)
    after_first = snapshot(tmp_path)
    assert delete(launcher, srv)["removed"] == []
    assert delete(launcher, srv, app_id="aaaaaaaaaaaa")["removed"] == []
    assert snapshot(tmp_path) == after_first


def test_a_session_keeps_the_turns_another_app_recorded(launcher, srv):
    plant_turn(srv["turns"], 12, 3, APP_B)
    delete(launcher, srv)
    assert sorted(p.name for p in (srv["turns"] / "12").iterdir()) == ["3"]


@pytest.mark.parametrize("which", [0, 1, 2, 3])
def test_a_symlink_where_a_tree_belongs_is_left_and_never_followed(
        launcher, srv, which, capsys):
    path = app_paths(srv, APP_A)[which]
    launcher.shutil.rmtree(path)
    path.symlink_to(srv["outside"], target_is_directory=True)
    outside = snapshot(srv["outside"])

    with pytest.raises(SystemExit) as exc:
        delete(launcher, srv)

    assert exc.value.code == 1
    assert str(path) in capsys.readouterr().err
    assert path.is_symlink() and snapshot(srv["outside"]) == outside
    for other in app_paths(srv, APP_A):
        assert other == path or not os.path.lexists(other)


@pytest.mark.parametrize("level", ["session", "turn"])
def test_a_symlinked_turn_dir_is_skipped_without_being_entered(
        launcher, srv, tmp_path, level):
    elsewhere = tmp_path / "elsewhere"
    real = plant_turn(elsewhere, 20, 1, APP_A)
    link = srv["turns"] / "20" if level == "session" else srv["turns"] / "14" / "2"
    link.symlink_to(real.parent if level == "session" else real,
                    target_is_directory=True)

    delete(launcher, srv)

    assert link.is_symlink() and (real / "result.json").is_file()


def test_a_tree_the_seat_does_not_own_and_a_file_at_the_name_are_left(
        launcher, srv, tmp_path, capsys):
    before = snapshot(tmp_path)
    with pytest.raises(SystemExit) as exc:
        delete(launcher, srv, uid=os.geteuid() + 1)
    assert exc.value.code == 1 and snapshot(tmp_path) == before
    capsys.readouterr()

    launcher.shutil.rmtree(srv["apps"] / APP_A)
    (srv["apps"] / APP_A).write_text("not a tree\n", encoding="utf-8")
    with pytest.raises(SystemExit) as exc:
        delete(launcher, srv)
    assert exc.value.code == 1
    assert (srv["apps"] / APP_A).read_text(encoding="utf-8") == "not a tree\n"


def test_a_running_turn_costs_the_served_roots_and_nothing_else(
        launcher, srv, capsys):
    www, staging, work, git_dir = app_paths(srv, APP_A)
    kept = [snapshot(p) for p in (staging, work, git_dir, srv["turns"])]

    with pytest.raises(SystemExit) as exc:
        delete(launcher, srv, systemctl="turn of a")

    assert exc.value.code == 64
    assert "has a turn running (disjorn-apps-12-3.service)" in capsys.readouterr().err
    assert not www.exists()
    assert [snapshot(p) for p in (staging, work, git_dir, srv["turns"])] == kept


def test_a_turn_of_another_app_does_not_hold_a_delete(launcher, srv):
    delete(launcher, srv, systemctl="turn of b")
    assert not (srv["apps"] / APP_A).exists()


@pytest.mark.skipif(os.geteuid() == 0, reason="the seat half refuses root")
def test_the_seat_half_is_wired_end_to_end(srv):
    proc = program(srv, "delete-as-seat", APP_A)
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout)
    assert out["mode"] == "delete" and out["app"] == APP_A
    assert str(srv["turns"] / "12" / "1") in out["removed"]
    assert not any(os.path.lexists(p) for p in app_paths(srv, APP_A))
    assert (srv["quarantine"] / APP_A / "1" / "evidence").is_file()


def test_the_turn_records_are_the_ones_the_seat_writes(launcher, srv):
    harvest = load(CC_DIR / "apps" / "apps_harvest.py", "apps_harvest_delete_pin")
    assert launcher.TURN_RECORDS[0] == harvest.RESULT_NAME
    wrapper = (CC_DIR / "apps" / "run-apps.sh").read_text(encoding="utf-8")
    assert f'_spec="$RESULT_DIR/{launcher.TURN_RECORDS[1]}"' in wrapper
    assert '"app_id": e["H_APP"],' in wrapper

    turn_dir = srv["turns"] / "30" / "1"
    harvest.refuse(turn_dir, "no repository", session=30, turn=1, app_id=APP_A,
                   started_at="2026-01-01T00:00:00+00:00")
    assert turn_dir in launcher._turn_dirs_of(srv["turns"], APP_A)
    assert turn_dir not in launcher._turn_dirs_of(srv["turns"], APP_B)
