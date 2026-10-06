"""snapshot.sh -> drill.sh -> freshness.sh end to end, against stub restic/sqlite3 and a tmp house."""

import json
import os
import pwd
import shlex
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from conftest import BACKUP, HOUSE_MEMORY
from house_memory import Memory, MemoryStore, StubEmbedder

GIT_ENV = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@t", "GIT_CONFIG_GLOBAL": "/dev/null"}


def _git_repo(path: Path, env: dict) -> None:
    path.mkdir(parents=True, exist_ok=True)
    run = lambda *a: subprocess.run(["git", "-C", str(path), *a], env=env, check=True,
                                    capture_output=True)
    run("init", "-q", "-b", "main")
    (path / "f").write_text("one\n")
    run("add", "f")
    run("commit", "-qm", "one")
    run("branch", "side")


def _db(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(path)
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY, body TEXT)")
    c.executemany("INSERT INTO messages (body) VALUES (?)", [("a",), ("b",), ("c",)])
    c.execute("CREATE TABLE schema_migrations (filename TEXT, applied_at TEXT)")
    c.executemany("INSERT INTO schema_migrations VALUES (?, 'x')",
                  [("001_init.sql",), ("010_backlog_status_verbs.sql",)])
    c.commit()
    c.close()


@pytest.fixture
def house(tmp_path):
    env = dict(os.environ, **GIT_ENV)
    bin_ = tmp_path / "bin"
    bin_.mkdir()
    for name in ("restic", "sqlite3"):
        w = bin_ / name
        w.write_text(f'#!/bin/sh\nexec {sys.executable} {BACKUP / "tests" / f"fake_{name}.py"} "$@"\n')
        w.chmod(0o755)

    disjorn = tmp_path / "disjorn"
    _git_repo(disjorn, env)
    _db(disjorn / "server/data/disjorn.db")
    (disjorn / "server/data/uploads").mkdir()
    (disjorn / "server/data/uploads/a.png").write_bytes(b"png")
    (disjorn / "server/.env").write_text("SECRET=1\n")
    _git_repo(tmp_path / "claudette-repo", env)
    _git_repo(tmp_path / "spine", env)

    c_home = tmp_path / "res-claudette"
    chroma = c_home / "chroma"
    store = MemoryStore(chroma, "claudette_memory", StubEmbedder(dim=64))
    mem = lambda text: Memory(content=text, subject="plink", source_author="plink")
    old = store.remember(mem("plink lives in oslo"))[0]
    store.remember(mem("the drill runs weekly"))
    store.forget(old.id, supersede_with=mem("plink lives in bergen"))
    (c_home / "memory_retrieval.jsonl").write_text('{"q": 1}\n')

    g_home = tmp_path / "res-gable"
    g_mem = g_home / ".claude/projects/-home-resident/memory"
    g_mem.mkdir(parents=True)
    (g_mem / "MEMORY.md").write_text("- [one](one.md)\n")
    (g_mem / "one.md").write_text("one\n")
    (g_mem / "sub").mkdir()
    (g_mem / "sub/two.md").write_text("two\n")

    stage = tmp_path / "state/stage"
    (tmp_path / "t").mkdir()
    env.update({
        "PATH": f"{bin_}:{env['PATH']}",
        "FAKE_RESTIC_REPO": str(tmp_path / "repo"),
        "DISJORN": str(disjorn),
        "CLAUDETTE_REPO": str(tmp_path / "claudette-repo"),
        "SPINE_REPO": str(tmp_path / "spine"),
        "CLAUDETTE_HOME": str(c_home),
        "GABLE_HOME": str(g_home),
        "CLAUDETTE_MEMORY_DIR": str(chroma),
        "CLAUDETTE_RETRIEVAL_LOG": str(c_home / "memory_retrieval.jsonl"),
        "CLAUDETTE_PY": sys.executable,
        "PYTHONPATH": str(HOUSE_MEMORY),
        "STAGE": str(stage),
        "BACKUP_PATHS": "\n".join(map(str, [stage, disjorn / "server/data/uploads",
                                            disjorn / "server/.env", c_home, g_home])),
        "BACKUP_POST_CMD": f"cat >> {tmp_path / 'posts.txt'}; echo --- >> {tmp_path / 'posts.txt'}",
        "TMPDIR": str(tmp_path / "t"),
    })
    return {"env": env, "tmp": tmp_path, "stage": stage, "g_mem": g_mem}


def run(house, script):
    return subprocess.run(["bash", str(BACKUP / script)], env=house["env"],
                          capture_output=True, text=True)


def posts(house):
    p = house["tmp"] / "posts.txt"
    return [x.strip() for x in p.read_text().split("---\n") if x.strip()] if p.exists() else []


def only_snapshot(house) -> Path:
    snaps = [p for p in (house["tmp"] / "repo/snaps").iterdir() if p.is_dir()]
    assert len(snaps) == 1
    return snaps[0]


def restored(house, snap: Path, p: Path) -> Path:
    return snap / p.relative_to("/")


def test_snapshot_writes_manifest_and_prunes_with_the_retention_policy(house):
    r = run(house, "snapshot.sh")
    assert r.returncode == 0, r.stderr
    assert not house["stage"].exists()
    snap = only_snapshot(house)
    m = json.loads(restored(house, snap, house["stage"] / "manifest.json").read_text())
    assert m["messages"] == 3 and m["migration"] == 10
    assert m["claudette"]["count"] == 3 and m["claudette"]["store_count"] == 3
    assert m["claudette_discord"] is None
    assert m["gable"]["memory_files"] == 3
    assert set(m["bundles"]) == {"claudette", "spine", "disjorn"}
    assert {"refs/heads/main", "refs/heads/side"} <= set(m["bundles"]["spine"])
    assert not restored(house, snap, house["stage"] / "claudette/chroma-copy").exists()
    calls = (house["tmp"] / "repo/calls.log").read_text()
    assert "forget --retry-lock 30m --tag disjorn-nightly --prune --keep-daily 7 --keep-weekly 4 --keep-monthly 12" in calls


def test_the_live_databases_are_backed_up_as_their_owner(house):
    """Run as root, sqlite may leave root-owned -wal/-shm beside a live
    database and lock its owner out; the backup runs as the owner instead."""
    bin_ = house["tmp"] / "bin"
    log = house["tmp"] / "runuser.log"
    (bin_ / "id").write_text('#!/bin/sh\n[ "$1" = "-u" ] && echo 0 && exit 0\nexec /usr/bin/id "$@"\n')
    (bin_ / "runuser").write_text(
        f'#!/bin/sh\necho "$@" >> {log}\nwhile [ "$1" != "--" ]; do shift; done\nshift\nexec "$@"\n')
    for name in ("id", "runuser"):
        (bin_ / name).chmod(0o755)
    r = run(house, "snapshot.sh")
    assert r.returncode == 0, r.stderr
    me = pwd.getpwuid(os.getuid()).pw_name
    calls = log.read_text().splitlines()
    assert any(c.startswith(f"-u {me} -- sqlite3 ") and "disjorn.db" in c for c in calls), calls
    assert any(c.startswith(f"-u {me} -- sqlite3 ") and "chroma.sqlite3" in c for c in calls), calls
    snap = only_snapshot(house)
    assert restored(house, snap, house["stage"] / "disjorn.db").exists()


def test_a_failed_owner_backup_leaves_no_temp_copy(house):
    bin_ = house["tmp"] / "bin"
    (bin_ / "id").write_text('#!/bin/sh\n[ "$1" = "-u" ] && echo 0 && exit 0\nexec /usr/bin/id "$@"\n')
    (bin_ / "runuser").write_text("#!/bin/sh\nexit 1\n")
    for name in ("id", "runuser"):
        (bin_ / name).chmod(0o755)
    r = run(house, "snapshot.sh")
    assert r.returncode != 0
    assert list((house["tmp"] / "t").iterdir()) == []


def test_drill_passes_on_an_untouched_snapshot(house):
    assert run(house, "snapshot.sh").returncode == 0
    r = run(house, "drill.sh")
    assert r.returncode == 0, r.stderr + "\n".join(posts(house))
    [post] = posts(house)
    assert post.startswith("backup drill PASS")
    for bit in ("3 messages", "migration 10", "claudette 3 memories", "gable 3 memory files",
                "3 bundles verified"):
        assert bit in post
    assert list((house["tmp"] / "t").iterdir()) == []


def test_drill_goes_red_on_every_tampered_surface(house):
    assert run(house, "snapshot.sh").returncode == 0
    snap = only_snapshot(house)
    stage = restored(house, snap, house["stage"])
    c = sqlite3.connect(stage / "disjorn.db")
    c.execute("DELETE FROM messages WHERE id = 1")
    c.commit()
    c.close()
    export = stage / "claudette/memory-export.json"
    export.write_text(export.read_text().replace("oslo", "paris"))
    (restored(house, snap, house["g_mem"]) / "sub/two.md").unlink()
    (stage / "bundles/spine.bundle").write_bytes(b"not a bundle")

    r = run(house, "drill.sh")
    assert r.returncode == 1
    [post] = posts(house)
    assert post.startswith("backup drill RED")
    for bit in ("db messages 2 != manifest 3", "claudette: export sha", "gable memory files 2",
                "bundle spine: verify failed"):
        assert bit in post, post


def test_the_discord_side_store_is_snapshotted_and_drilled(house):
    discord = house["tmp"] / "discord-chroma"
    store = MemoryStore(discord, "claudette_memory", StubEmbedder(dim=64))
    for text in ("plink plays bass", "the server is called disjorn"):
        store.remember(Memory(content=text, subject="plink", source_author="plink"))
    house["env"]["CLAUDETTE_DISCORD_MEMORY_DIR"] = str(discord)
    assert run(house, "snapshot.sh").returncode == 0
    snap = only_snapshot(house)
    stage = restored(house, snap, house["stage"])
    m = json.loads((stage / "manifest.json").read_text())
    assert m["claudette_discord"]["count"] == 2 == m["claudette_discord"]["store_count"]

    r = run(house, "drill.sh")
    assert r.returncode == 0, r.stderr + "\n".join(posts(house))
    assert "claudette 3 memories round-trip, discord-side 2;" in posts(house)[0]

    export = stage / "claudette/discord-memory-export.json"
    export.write_text(export.read_text().replace("bass", "drums"))
    assert run(house, "drill.sh").returncode == 1
    assert "claudette_discord: export sha" in posts(house)[1]


def test_drill_red_when_the_export_dropped_records_the_source_store_held(house):
    assert run(house, "snapshot.sh").returncode == 0
    mf = restored(house, only_snapshot(house), house["stage"]) / "manifest.json"
    m = json.loads(mf.read_text())
    m["claudette"]["store_count"] = 4
    mf.write_text(json.dumps(m))
    assert run(house, "drill.sh").returncode == 1
    assert "source store held 4 records but 3 were exported" in posts(house)[0]


def test_drill_red_when_there_is_no_snapshot(house):
    r = run(house, "drill.sh")
    assert r.returncode == 1
    assert posts(house) == ["backup drill RED: no disjorn-nightly snapshot in the repo"]


def test_freshness_is_silent_when_fresh_and_red_when_stale_or_missing(house):
    r = run(house, "freshness.sh")
    assert r.returncode == 1 and "no disjorn-nightly snapshot" in posts(house)[0]

    assert run(house, "snapshot.sh").returncode == 0
    r = run(house, "freshness.sh")
    assert r.returncode == 0, r.stderr
    assert len(posts(house)) == 1

    meta = next((house["tmp"] / "repo/snaps").glob("*.json"))
    d = json.loads(meta.read_text())
    d["time"] = "2020-01-01T00:00:00.5+00:00"
    meta.write_text(json.dumps(d))
    r = run(house, "freshness.sh")
    assert r.returncode == 1
    assert "backup freshness RED" in posts(house)[1] and "limit 26h" in posts(house)[1]


def _unit(name):
    return (BACKUP / name).read_text()


@pytest.mark.parametrize("kind", ["snapshot", "drill", "check", "freshness"])
def test_every_unit_has_a_timer_and_reports_its_own_failure(kind):
    svc = _unit(f"disjorn-backup-{kind}.service")
    assert "OnFailure=disjorn-backup-alert@%n.service" in svc
    assert "EnvironmentFile=/etc/disjorn-backup/restic.env" in svc
    assert "OnCalendar=" in _unit(f"disjorn-backup-{kind}.timer")
    if kind in ("snapshot", "drill"):
        service = svc.partition("\n[Service]\n")[2].splitlines()
        assert any(line.startswith("MemoryMax=") for line in service)


def test_alert_unit_posts_the_failed_unit_name_and_its_log(house):
    line = next(x for x in _unit("disjorn-backup-alert@.service").splitlines()
                if x.startswith("ExecStart="))
    cmd = (line[len("ExecStart="):].replace("$$", "$").replace("%%", "%").replace("\\\\", "\\")
           .replace("%i", "disjorn-backup-drill.service")
           .replace("/home/plink/Disjorn/Disjorn/harness/backup", str(BACKUP)))
    journalctl = house["tmp"] / "bin/journalctl"
    journalctl.write_text('#!/bin/sh\necho "log for $2"\n')
    journalctl.chmod(0o755)
    r =subprocess.run(shlex.split(cmd), env=house["env"], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert posts(house) == ["backup RED: disjorn-backup-drill.service failed; last log lines:\n"
                            "log for disjorn-backup-drill.service"]


def test_cadence_drill_is_weekly_and_check_reads_five_percent():
    assert "OnCalendar=Sun " in _unit("disjorn-backup-drill.timer")
    assert "--read-data-subset=5%%" in _unit("disjorn-backup-check.service")
    assert "\nOnFailure=" not in _unit("disjorn-backup-alert@.service")
