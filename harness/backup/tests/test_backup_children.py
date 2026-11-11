"""A child of the backup run as another user: none of our environment, and a deadline."""

import re
import time

import pytest

import apps
from conftest import BACKUP
from test_backup_apps import DRAFT, LIVE, srv  # noqa: F401
from test_backup_drill import house, run  # noqa: F401

SENTINELS = {"RESTIC_PASSWORD": "sentinel-secret", "RESTIC_REPOSITORY": "sentinel-repo",
             "AWS_ACCESS_KEY_ID": "sentinel-id", "AWS_SECRET_ACCESS_KEY": "sentinel-key",
             "FOO": "sentinel-foo"}
SHELL_OWN = {"PWD", "OLDPWD", "SHLVL", "_"}


def _script(path, body):
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(0o755)


def _pretend_root(house):
    """`id -u` says 0, and the stand-in runuser keeps its caller's environment as the real one does."""
    bin_ = house["tmp"] / "bin"
    _script(bin_ / "id", '[ "$1" = "-u" ] && echo 0 && exit 0\nexec /usr/bin/id "$@"\n')
    _script(bin_ / "runuser", f'env > {house["tmp"] / "runuser-env"}\n'
                              'while [ "$1" != "--" ]; do shift; done\nshift\nexec "$@"\n')
    return bin_


def _keys(dump):
    text = dump.read_text()
    assert "sentinel" not in text, text
    return {line.split("=", 1)[0] for line in text.splitlines()}


def test_a_database_backup_run_as_its_owner_sees_none_of_our_environment(house):
    bin_ = _pretend_root(house)
    house["env"].update(SENTINELS)
    fake = (bin_ / "sqlite3").read_text().splitlines()[1]
    _script(bin_ / "sqlite3", f'case "$2" in *copy.db*) env > {house["tmp"]}/sqlite-env.$$;; esac\n{fake}\n')
    r = run(house, "snapshot.sh")
    assert r.returncode == 0, r.stderr
    assert "sentinel-secret" in (house["tmp"] / "runuser-env").read_text()
    dumps = list(house["tmp"].glob("sqlite-env.*"))
    assert len(dumps) == 2
    for dump in dumps:
        assert "PATH=/usr/bin:/bin" in dump.read_text()
        assert _keys(dump) <= {"PATH"} | SHELL_OWN


@pytest.mark.parametrize("as_root", [True, False])
def test_git_on_an_app_repo_sees_none_of_our_environment(srv, tmp_path, monkeypatch, as_root):
    bin_ = _pretend_root(srv)
    dump = tmp_path / "git-env"
    _script(bin_ / "git", f'env >> {dump}\nexec /usr/bin/git "$@"\n')
    for key, value in {**SENTINELS, "PATH": srv["env"]["PATH"]}.items():
        monkeypatch.setenv(key, value)
    if as_root:
        monkeypatch.setattr(apps.os, "geteuid", lambda: 0)
    facts = apps.stage(srv["tmp"] / "apps-git", srv["tmp"] / "apps-www", tmp_path / "out")
    assert facts["warnings"] == {} and facts["apps"][LIVE]["bundle_sha256"]
    assert (srv["tmp"] / "runuser-env").exists() == as_root
    assert _keys(dump) <= set(apps.CHILD_ENV) | SHELL_OWN
    assert "HOME=/nonexistent" in dump.read_text() and "PATH=/usr/bin:/bin" in dump.read_text()


def test_every_runuser_in_the_backup_clears_the_environment_for_its_child():
    calls = []
    for path in [*BACKUP.glob("*.sh"), *BACKUP.glob("*.py"), *BACKUP.glob("*.service")]:
        for line in path.read_text().splitlines():
            if re.search(r"\brunuser\b", line) and not re.match(r"\s*(#|need )", line):
                calls.append((path.name, line))
    assert {name for name, _ in calls} == {"lib.sh", "apps.py"}
    for name, line in calls:
        assert re.search(r"\brunuser\b.*--\W+env\W+-i\b", line), (name, line)


def test_a_hung_git_costs_one_app_its_bundle_and_the_rest_are_staged(srv, tmp_path, monkeypatch):
    _script(srv["tmp"] / "bin/git",
            f'case "$*" in *{LIVE}.git*bundle*) exec sleep 60;; esac\nexec /usr/bin/git "$@"\n')
    monkeypatch.setenv("PATH", srv["env"]["PATH"])
    monkeypatch.setattr(apps, "CHILD_TIMEOUT", 3)
    began = time.monotonic()
    facts = apps.stage(srv["tmp"] / "apps-git", srv["tmp"] / "apps-www", tmp_path / "out")
    assert time.monotonic() - began < 30
    assert facts["warnings"] == {LIVE: "git timed out after 3s"}
    assert facts["apps"][LIVE]["bundle_sha256"] is None and facts["apps"][LIVE]["live"]["files"] == 2
    assert not (tmp_path / "out" / LIVE / "repo.bundle").exists()
    assert facts["apps"][DRAFT]["bundle_sha256"]


def test_a_hung_database_backup_fails_the_snapshot_instead_of_stalling_it(house):
    bin_ = _pretend_root(house)
    fake = (bin_ / "sqlite3").read_text().splitlines()[1]
    _script(bin_ / "sqlite3", f'case "$2" in *copy.db*) exec sleep 60;; esac\n{fake}\n')
    house["env"]["BACKUP_CHILD_TIMEOUT"] = "2"
    began = time.monotonic()
    r = run(house, "snapshot.sh")
    assert r.returncode != 0 and time.monotonic() - began < 40
    assert list((house["tmp"] / "t").iterdir()) == []
