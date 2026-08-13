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

WHAT GIT PROMISES ABOUT A LOOSE OBJECT, since two checks got it wrong for a
week: 0444. Not 0664. core.sharedRepository=group adds group READ to what git
writes and stops there, because an object file is named by its own content and
nothing may ever rewrite it. Asserting group-write on objects/ failed a healthy
repo forever, and the caveat lived in chat memory instead of in the check —
which is why `test_a_repo_with_real_objects_passes_verification` below writes
REAL objects with git rather than checking an empty repo, where the bug hides.
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
    subprocess.run(["git", "init", "--bare", "--shared=group", "-b", "main", str(repo)],
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
    subprocess.run(["git", "init", "--bare", "--shared=group", "-b", "main", str(repo)],
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
    subprocess.run(["git", "init", "--bare", "--shared=group", "-b", "main", str(repo)],
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
    subprocess.run(["git", "init", "--bare", "--shared=group", "-b", "main", str(repo)],
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
    """A MUTABLE path — config is rewritten in place, so the group must be able
    to write it. This is the half of the old sweep that was always right."""
    gh, run = gatehouse
    repo = _full_recipe(gh)
    victim = repo / "config"
    victim.chmod(0o644)
    proc = run("verify", "gable")
    assert proc.returncode != 0
    assert "NOT group-writable" in proc.stderr


# ── the objects sweep: group READ, which is what git actually promises ───

def _write_objects(repo: Path, n=3):
    """Write n real loose objects the way a push would — with git, so their
    modes are git's and not the test's."""
    shas = []
    for i in range(n):
        sha = subprocess.run(["git", "-C", str(repo), "hash-object", "-w", "--stdin"],
                             input=f"object {i}\n", text=True, check=True,
                             capture_output=True).stdout.strip()
        shas.append(sha)
    return [repo / "objects" / s[:2] / s[2:] for s in shas]


def test_git_writes_loose_objects_0444_under_shared_group(gatehouse):
    """The premise of the fix, pinned against git itself rather than asserted.
    If a future git starts writing 0664 here, this test says so before the two
    checks below start passing for the wrong reason."""
    gh, run = gatehouse
    repo = _full_recipe(gh)
    for obj in _write_objects(repo, 1):
        mode = obj.stat().st_mode & 0o777
        assert not mode & stat.S_IWGRP, f"expected a read-only object, got {mode:o}"
        assert mode & stat.S_IRGRP, f"expected group-readable, got {mode:o}"


def test_a_repo_with_real_objects_passes_verification(gatehouse):
    """THE ACCEPTANCE TEST. A healthy gatehouse repo that has been pushed into
    must verify clean. It could not before: every loose object is 0444, the
    global sweep demanded group-write on everything, and so a repo with any
    history at all FAILED forever while being entirely correct."""
    gh, run = gatehouse
    repo = _full_recipe(gh)
    _write_objects(repo)
    proc = run("verify", "gable")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "FAIL" not in proc.stderr, proc.stderr
    assert "VERIFIED" in proc.stdout


def test_an_object_that_is_not_group_readable_fails_verification(gatehouse):
    """The failure that IS real for an object: the group cannot read it, so the
    broker's next fetch dies on one file it has read a hundred times."""
    gh, run = gatehouse
    repo = _full_recipe(gh)
    victim = _write_objects(repo, 1)[0]
    victim.chmod(0o400)
    proc = run("verify", "gable")
    assert proc.returncode != 0
    assert "NOT group-readable" in proc.stderr
    assert victim.name in proc.stderr, "the failing object must be named"


def test_an_object_directory_without_group_write_fails_verification(gatehouse):
    """A fan-out directory is where the NEXT object gets written. Group-write
    there is about the objects that do not exist yet, so it stays asserted even
    though the objects inside it are read-only."""
    gh, run = gatehouse
    repo = _full_recipe(gh)
    obj = _write_objects(repo, 1)[0]
    fanout = obj.parent
    fanout.chmod(0o2755)
    proc = run("verify", "gable")
    assert proc.returncode != 0
    assert "setgid and group-writable" in proc.stderr
    assert fanout.name in proc.stderr


def test_objects_are_not_swept_for_group_write(gatehouse):
    """Stated as its own test because it is the exact regression: a 0444 object
    must not appear in the mutable-path sweep's output under any wording."""
    gh, run = gatehouse
    repo = _full_recipe(gh)
    objs = _write_objects(repo)
    proc = run("verify", "gable")
    out = proc.stdout + proc.stderr
    for obj in objs:
        assert obj.name not in out, f"object {obj} was flagged: {out}"


# ── HEAD, which git writes once and never mentions again ─────────────────

def test_head_pointing_at_master_fails_verification(gatehouse):
    """The birth defect: `git init --bare` with no -b leaves HEAD →
    refs/heads/master while every lane pushes main. Nothing complains — the
    push works, the ref exists — until a clone comes out on a branch nobody
    pushes."""
    gh, run = gatehouse
    repo = _full_recipe(gh)
    subprocess.run(["git", "-C", str(repo), "symbolic-ref", "HEAD", "refs/heads/master"],
                   check=True, capture_output=True)
    proc = run("verify", "gable")
    assert proc.returncode != 0
    assert "refs/heads/master, not refs/heads/main" in proc.stderr
    assert "symbolic-ref HEAD refs/heads/main" in proc.stderr, "the fix must be printed"


def test_head_on_main_passes(gatehouse):
    gh, run = gatehouse
    _full_recipe(gh)
    proc = run("verify", "gable")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "HEAD is refs/heads/main" in proc.stdout


def test_create_inits_the_repo_on_main():
    """`create` needs root, so the birth of the repo cannot be exercised here
    (see the module docstring: a faked privileged path is worse than none). The
    one line that decides HEAD forever is therefore pinned in the source — it
    runs exactly once per repo, and re-running `create` will NOT repair it,
    because create does not touch refs."""
    src = SCRIPT.read_text()
    assert "git init --bare --shared=group -b main" in src


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


def test_the_seat_probe_asks_for_group_read_not_group_write():
    """The probe runs only as root, so what it ASSERTS is pinned in the source
    instead. hash-object succeeding already proves res-<name> can write; the
    object it leaves behind is 0444, and demanding group-write of it made every
    healthy probe report a failure that was not there."""
    src = SCRIPT.read_text()
    assert "not group-READABLE" in src
    assert "is not group-writable (mode" not in src, \
        "the probe must not demand group-write of an object git writes 0444"


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
