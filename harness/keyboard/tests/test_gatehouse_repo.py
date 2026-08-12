"""08-gatehouse-repo.sh — the recipe, applied at creation and then verified.

The script's whole claim is that a gatehouse repo comes out of `create` with
every property already true, rather than being repaired afterwards. So these
tests run the real script against a scratch gatehouse and then check the
filesystem directly — never the script's own report, which is the thing under
test.

WHAT CANNOT BE TESTED HERE, and is therefore asserted as a refusal instead.
Three parts of the recipe need root: creating the `gatehouse` group, chowning
to the broker user, and the `sudo -u res-<name>` seat probe. An unprivileged
test that pretended to cover them would be exactly the keyboard-reported
verification the 2026-08-07 lesson rules out. So the tests below run with
BROKER_USER and GATEHOUSE_GROUP pointed at the invoking uid's own user/group —
which exercises setgid, g+rwX, core.sharedRepository, safe.directory and the
wrong-group check for real — and separately assert that the privileged paths
REFUSE rather than proceed when they cannot be done properly.

GIT_CONFIG_SYSTEM redirects `git config --system` to a scratch file (git
2.32+), so the safe.directory half is exercised without writing /etc/gitconfig.
"""

from __future__ import annotations

import grp
import os
import pwd
import stat
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "08-gatehouse-repo.sh"

ME = pwd.getpwuid(os.getuid()).pw_name
MY_GROUP = grp.getgrgid(os.getgid()).gr_name
IS_ROOT = os.getuid() == 0


@pytest.fixture()
def gatehouse(tmp_path):
    """A scratch gatehouse dir + a run() helper for the script."""
    gh = tmp_path / "gatehouse"
    gh.mkdir()
    sysconfig = tmp_path / "gitconfig-system"

    def run(*args, **overrides):
        env = dict(os.environ)
        env.update(
            GATEHOUSE_DIR=str(gh),
            GATEHOUSE_GROUP=MY_GROUP,
            BROKER_USER=ME,
            GIT_CONFIG_SYSTEM=str(sysconfig),
            # Keep the invoking user's real gitconfig out of it entirely.
            GIT_CONFIG_GLOBAL=str(tmp_path / "gitconfig-global"),
        )
        env.update({k: str(v) for k, v in overrides.items()})
        return subprocess.run(["bash", str(SCRIPT), *args],
                              capture_output=True, text=True, env=env)

    return gh, run


def walk(repo: Path):
    for root, dirs, files in os.walk(repo):
        yield Path(root)
        for f in files:
            yield Path(root) / f


# ── the recipe is applied AT CREATION ────────────────────────────────────

@pytest.mark.skipif(IS_ROOT, reason="the unprivileged create path is what this covers")
def test_create_refuses_without_root(gatehouse):
    """Group creation, chown and the seat probe all need root. Refusing is the
    correct outcome — a half-applied recipe is the thing this file prevents."""
    gh, run = gatehouse
    proc = run("create", "gable", "gable")
    assert proc.returncode != 0
    assert "needs root" in proc.stderr
    assert not (gh / "gable.git").exists(), "nothing may be created on the refusal path"


def make_repo(gh: Path, run, name="gable"):
    """Create the repo the way `create` would, minus the two root-only steps
    (groupadd, chown), so the rest of the recipe can be checked for real."""
    repo = gh / f"{name}.git"
    subprocess.run(["git", "init", "--bare", "--shared=group", str(repo)],
                   check=True, capture_output=True)
    proc = run("verify", name)
    return repo, proc


def test_git_init_shared_alone_is_not_the_recipe(gatehouse):
    """The premise, pinned: `git init --bare --shared=group` does NOT leave a
    repo that passes. If it did, this script would be ceremony."""
    gh, run = gatehouse
    repo, proc = make_repo(gh, run)
    assert proc.returncode != 0
    # safe.directory is the part git never does for you.
    assert "safe.directory" in proc.stderr


@pytest.mark.skipif(not hasattr(os, "chown"), reason="POSIX only")
def test_verify_passes_once_the_full_recipe_is_applied(gatehouse):
    """Apply exactly what do_create() applies (minus chown, which needs root
    and is a no-op when the broker user IS the invoking user) and verify must
    then pass every check."""
    gh, run = gatehouse
    repo = gh / "gable.git"
    subprocess.run(["git", "init", "--bare", "--shared=group", str(repo)],
                   check=True, capture_output=True)
    subprocess.run(["chmod", "-R", "g+rwX", str(repo)], check=True)
    subprocess.run(["find", str(repo), "-type", "d", "-exec", "chmod", "g+s", "{}", "+"],
                   check=True)
    subprocess.run(["git", "-C", str(repo), "config", "core.sharedRepository", "group"],
                   check=True, capture_output=True)
    env = dict(os.environ, GIT_CONFIG_SYSTEM=str(gh.parent / "gitconfig-system"))
    subprocess.run(["git", "config", "--system", "--add", "safe.directory", str(repo)],
                   check=True, capture_output=True, env=env)

    proc = run("verify", "gable")
    assert proc.returncode == 0, proc.stderr
    assert "VERIFIED" in proc.stdout


def test_every_directory_is_setgid_and_group_writable(gatehouse):
    """setgid on EVERY directory is the property that governs files that do not
    exist yet — the one that cannot be retrofitted onto tomorrow's objects."""
    gh, run = gatehouse
    repo = gh / "gable.git"
    subprocess.run(["git", "init", "--bare", "--shared=group", str(repo)],
                   check=True, capture_output=True)
    subprocess.run(["chmod", "-R", "g+rwX", str(repo)], check=True)
    subprocess.run(["find", str(repo), "-type", "d", "-exec", "chmod", "g+s", "{}", "+"],
                   check=True)
    for p in walk(repo):
        mode = p.stat().st_mode
        assert mode & stat.S_IWGRP, f"not group-writable: {p}"
        if p.is_dir():
            assert mode & stat.S_ISGID, f"not setgid: {p}"
            assert mode & stat.S_IXGRP, f"not group-traversable: {p}"


# ── verify catches each way the recipe can rot ───────────────────────────

def _full_recipe(gh: Path, name="gable"):
    repo = gh / f"{name}.git"
    subprocess.run(["git", "init", "--bare", "--shared=group", str(repo)],
                   check=True, capture_output=True)
    subprocess.run(["chmod", "-R", "g+rwX", str(repo)], check=True)
    subprocess.run(["find", str(repo), "-type", "d", "-exec", "chmod", "g+s", "{}", "+"],
                   check=True)
    subprocess.run(["git", "-C", str(repo), "config", "core.sharedRepository", "group"],
                   check=True, capture_output=True)
    env = dict(os.environ, GIT_CONFIG_SYSTEM=str(gh.parent / "gitconfig-system"))
    subprocess.run(["git", "config", "--system", "--add", "safe.directory", str(repo)],
                   check=True, capture_output=True, env=env)
    return repo


def test_a_single_directory_losing_setgid_fails_verification(gatehouse):
    gh, run = gatehouse
    repo = _full_recipe(gh)
    victim = repo / "objects" / "pack"
    victim.chmod(victim.stat().st_mode & ~stat.S_ISGID)
    proc = run("verify", "gable")
    assert proc.returncode != 0
    assert "WITHOUT setgid" in proc.stderr
    assert "objects/pack" in proc.stderr, "the failing path must be named"


def test_core_shared_repository_unset_fails_verification(gatehouse):
    gh, run = gatehouse
    repo = _full_recipe(gh)
    subprocess.run(["git", "-C", str(repo), "config", "--unset", "core.sharedRepository"],
                   check=True, capture_output=True)
    proc = run("verify", "gable")
    assert proc.returncode != 0
    assert "core.sharedRepository is UNSET" in proc.stderr


def test_a_non_group_writable_file_fails_verification(gatehouse):
    gh, run = gatehouse
    repo = _full_recipe(gh)
    victim = repo / "config"
    victim.chmod(0o644)
    proc = run("verify", "gable")
    assert proc.returncode != 0
    assert "NOT group-writable" in proc.stderr


def test_missing_safe_directory_fails_and_says_why(gatehouse):
    """The dubious-ownership refusal is silent until a resident tries, and then
    it looks like a git bug rather than a missing config line."""
    gh, run = gatehouse
    _full_recipe(gh)
    proc = run("verify", "gable", GIT_CONFIG_SYSTEM=str(gh / "empty-sysconfig"))
    assert proc.returncode != 0
    assert "safe.directory" in proc.stderr
    assert "dubious ownership" in proc.stderr


def test_verify_refuses_a_repo_that_is_not_there(gatehouse):
    gh, run = gatehouse
    proc = run("verify", "nope")
    assert proc.returncode != 0
    assert "no such gatehouse repo" in proc.stderr


# ── the wrong-group check, which is the whole 08-07 lesson ───────────────

@pytest.mark.skipif(len(os.getgroups()) < 2,
                    reason="needs a second group to put a file in")
def test_one_wrong_group_file_is_found_and_named(gatehouse):
    """The hazard is exactly ONE file in ONE fan-out directory. Nothing notices
    until the other uid touches that file, weeks later, on a fetch."""
    gh, run = gatehouse
    repo = _full_recipe(gh)
    other = next(g for g in os.getgroups() if g != os.getgid())
    victim = repo / "objects" / "info" / "planted"
    victim.write_text("x")
    os.chown(victim, -1, other)
    victim.chmod(0o664)
    proc = run("verify", "gable")
    assert proc.returncode != 0
    assert "WRONG-GROUP FILES" in proc.stderr
    assert "planted" in proc.stderr


# ── the seat probe is never skipped quietly ──────────────────────────────

@pytest.mark.skipif(IS_ROOT, reason="covers the unprivileged path")
def test_naming_a_resident_without_root_fails_rather_than_skipping(gatehouse):
    """A group layer that was not verified from the seat must not read as
    verified. Unprivileged + a named resident = FAIL, not a friendly note."""
    gh, run = gatehouse
    _full_recipe(gh)
    proc = run("verify", "gable", "gable")
    assert proc.returncode != 0
    assert "NOT ROOT" in proc.stderr
    assert "seat probe did not run" in proc.stderr


def test_verify_with_no_resident_warns_that_no_seat_was_asked(gatehouse):
    gh, run = gatehouse
    _full_recipe(gh)
    proc = run("verify", "gable")
    assert proc.returncode == 0, proc.stderr
    assert "NOT verified from any seat" in proc.stderr


# ── argument validation ──────────────────────────────────────────────────

@pytest.mark.parametrize("name", ["../escape", "gable.git", "-flag", "with space", ""])
def test_bad_repo_names_are_refused(gatehouse, name):
    gh, run = gatehouse
    proc = run("verify", name)
    assert proc.returncode != 0
    assert "repo name must be" in proc.stderr or "usage:" in proc.stderr


@pytest.mark.parametrize("resident", ["res-gable", "Gable", "ga ble", "../x"])
def test_bad_resident_names_are_refused(gatehouse, resident):
    gh, run = gatehouse
    proc = run("verify", "gable", resident)
    assert proc.returncode != 0
    assert "resident must be a plain lowercase name" in proc.stderr


def test_unknown_mode_is_refused(gatehouse):
    gh, run = gatehouse
    proc = run("repair", "gable")
    assert proc.returncode != 0
    assert "unknown mode" in proc.stderr
