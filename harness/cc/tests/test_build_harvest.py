"""The build publish path: entitled set, quarantine, post-exit harvest.

SPECS/2026-08-13-build-publish-path.md (confirmed by plink, #custodian seq
1211). The build container no longer has a gatehouse mount and the session no
longer pushes; `run-build.sh` clones the entitled set, runs podman as a CHILD,
and publishes `loop/<slug>` into the gatehouse itself once the container exits
clean. What the broker reaper reads is three machine-readable stdout lines and
their absence:

    PUBLISHED <repo>.git <sha>
    NO-COMMITS <repo>.git
    PUBLISH-FAILED <repo>.git <git error, flattened>

REAL GIT, NOT MOCKS. Every repo here is a real bare repo in tmp_path with real
commits, because the properties under test are git's: what a non-fast-forward
does, what a local clone's objects look like, whether a remote exists. A mocked
git would agree with whatever this file believed on the day it was written —
and the whole reason this spec exists is that a push "working" was an accident
nobody had measured. Only podman is faked; the container is the one thing these
tests do not need.

Sibling suite: test_run_wrappers.py (credentials, mounts, spine, reaper).
"""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

import pytest

CC_DIR = Path(__file__).resolve().parent.parent
RUN_BUILD = CC_DIR / "run-build.sh"

NAME = "gable"          # the resident argument: entitled to disjorn + gable
SLUG = "2026-08-13-a-spec"
BRANCH = f"loop/{SLUG}"

# A fake podman that behaves like a build session: it runs a shell snippet
# taken from $FAKE_BUILD_SCRIPT against the workspace the wrapper provisioned
# (the host path, since there is no container), then exits with
# $FAKE_BUILD_RC. That is exactly the surface the harvest measures — commits
# on a branch in ~/work/<repo> — with none of the container.
FAKE_PODMAN = r"""#!/usr/bin/env bash
set -u
if [ "${1:-}" = "rm" ]; then
  printf '%s\n' "$*" >> "$DUMP_DIR/reaped"
  exit 0
fi
printf '%s\0' "$@" > "$DUMP_DIR/argv"
: > "$DUMP_DIR/run-started"
if [ -n "${FAKE_BUILD_SCRIPT:-}" ] && [ -f "$FAKE_BUILD_SCRIPT" ]; then
  bash "$FAKE_BUILD_SCRIPT" || exit 90
fi
exit "${FAKE_BUILD_RC:-0}"
"""

# The same fake, but it lingers like a real container so a kill can be timed.
FAKE_PODMAN_SLOW = r"""#!/usr/bin/env bash
set -u
if [ "${1:-}" = "rm" ]; then
  printf '%s\n' "$*" >> "$DUMP_DIR/reaped"
  exit 0
fi
prev=""; cid=""
for a in "$@"; do
  [ "$prev" = "--cidfile" ] && cid="$a"
  prev="$a"
done
[ -n "$cid" ] && printf '%s' "__FAKE_CID__" > "$cid"
: > "$DUMP_DIR/run-started"
exec sleep 30
""".replace("__FAKE_CID__", "fakecid" + "0" * 55 + "f")


def git(*args, cwd=None, check=True):
    return subprocess.run(["git", *args], cwd=cwd, check=check, text=True,
                          capture_output=True)


def bare_repo(path: Path, first_commit=True):
    """A bare repo with (optionally) one commit on `main`."""
    git("init", "--bare", "-b", "main", str(path))
    if first_commit:
        seed = path.parent / f".seed-{path.name}"
        git("clone", "--quiet", str(path), str(seed))
        (seed / "README").write_text("base\n")
        git("add", "README", cwd=seed)
        git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "base", cwd=seed)
        git("push", "--quiet", "origin", "HEAD:refs/heads/main", cwd=seed)
        subprocess.run(["rm", "-rf", str(seed)], check=True)
    return path


def head_of(bare: Path, ref: str):
    """The sha a branch points at in a bare repo, or None if it has no such
    branch. Measured the way the wrapper measures it: in the gatehouse."""
    p = git("rev-parse", "--verify", "-q", f"refs/heads/{ref}",
            cwd=bare, check=False)
    return p.stdout.strip() or None


@pytest.fixture()
def rig(tmp_path):
    """A build seat's whole ground in tmp_path, plus a run() helper.

    Returns run(build_script=None, rc=0, name=NAME, podman=FAKE_PODMAN, ...)
    -> CompletedProcess, with `.gatehouse`, `.work` and `.dump` on the rig.
    """
    home_vol = tmp_path / "build-home"
    config = tmp_path / "config"
    dump = tmp_path / "dump"
    bindir = tmp_path / "bin"
    gatehouse = tmp_path / "gatehouse"
    for d in (home_vol, config, dump, bindir, gatehouse):
        d.mkdir()
    (config / "env").write_text("BROKER_DISABLE=1\n")
    kernel = tmp_path / "build-kernel.md"
    kernel.write_text("# Build session\n")

    bare_repo(gatehouse / "disjorn.git")
    bare_repo(gatehouse / f"{NAME}.git")

    class Rig:
        pass

    rig = Rig()
    rig.tmp = tmp_path
    rig.gatehouse = gatehouse
    rig.work = home_vol / "work"
    rig.home_vol = home_vol
    rig.dump = dump
    rig.procs = []

    def _env(extra_env, podman_src):
        podman = bindir / "podman"
        podman.write_text(podman_src)
        podman.chmod(0o755)
        env = dict(os.environ)
        env.update(
            PATH=f"{bindir}:{env['PATH']}",
            DUMP_DIR=str(dump),
            RESIDENT_IMAGE="localhost/disjorn-resident:test",
            RESIDENT_HOME_VOL=str(home_vol),
            RESIDENT_CONFIG_DIR=str(config),
            RESIDENT_BUILD_KERNEL=str(kernel),
            RESIDENT_GATEHOUSE=str(gatehouse),
            RESIDENT_NETWORK="none",
            FAKE_BUILD_SCRIPT="",
            FAKE_BUILD_RC="0",
        )
        env.update(extra_env or {})
        return env

    def run(build_script: str | None = None, rc: int = 0, name: str = NAME,
            extra_env: dict | None = None, podman: str = FAKE_PODMAN):
        extra = dict(extra_env or {})
        if build_script is not None:
            script = tmp_path / "fake-build.sh"
            script.write_text(build_script)
            extra["FAKE_BUILD_SCRIPT"] = str(script)
        extra["FAKE_BUILD_RC"] = str(rc)
        return subprocess.run(
            ["bash", str(RUN_BUILD), name, SLUG],
            capture_output=True, text=True, env=_env(extra, podman),
            stdin=subprocess.DEVNULL, timeout=120)

    def launch(extra_env: dict | None = None, podman: str = FAKE_PODMAN_SLOW):
        p = subprocess.Popen(
            ["bash", str(RUN_BUILD), NAME, SLUG],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL, env=_env(extra_env, podman))
        rig.procs.append(p)
        return p

    rig.run = run
    rig.launch = launch
    yield rig
    for p in rig.procs:
        try:
            p.kill()
        except OSError:
            pass


def commit_script(repo: str, n: int = 1, message="work"):
    """A fake build session: n commits on the branch already checked out in
    ~/work/<repo>. Written the way the real session works — in the clone the
    wrapper provisioned, with no push, because there is nothing to push to."""
    return f"""#!/usr/bin/env bash
set -euo pipefail
cd "$HOME/build-home/work/{repo}" 2>/dev/null || cd "{{work}}/{repo}"
for i in $(seq {n}); do
  echo "{message} $i" >> {message}.txt
  git add {message}.txt
  git -c user.name=t -c user.email=t@t commit -qm "{message} $i"
done
"""


def session(rig, repo: str, n: int = 1, message="work"):
    """commit_script bound to this rig's work dir."""
    return commit_script(repo, n, message).replace("{work}", str(rig.work))


def lines(proc, prefix):
    return [ln for ln in proc.stdout.splitlines() if ln.startswith(prefix)]


# ── the entitled set ─────────────────────────────────────────────────────
#
# Until 2026-08-13 the wrapper cloned every *.git in the gatehouse, so every
# seat got a writable clone of every other seat's repo — a widening that
# arrived by directory listing rather than by decision, and that grew every
# time the keyboard created a lane. The set is now declared: disjorn + <name>.


def test_only_the_entitled_repos_are_cloned(rig):
    """A foreign repo in the gatehouse is not the build's business."""
    bare_repo(rig.gatehouse / "claudette.git")
    proc = rig.run()
    assert proc.returncode == 0, proc.stderr
    assert (rig.work / "disjorn").is_dir()
    assert (rig.work / NAME).is_dir()
    assert not (rig.work / "claudette").exists(), (
        "a foreign gatehouse repo was cloned into the seat's workspace")


def test_a_missing_entitled_repo_refuses_before_podman_runs(rig):
    """Loud, and BEFORE the model is ever invoked. A build that quietly
    proceeds with half its ground produces a report nobody can trust."""
    subprocess.run(["rm", "-rf", str(rig.gatehouse / "disjorn.git")], check=True)
    proc = rig.run()
    assert proc.returncode != 0
    assert "REFUSING TO LAUNCH" in proc.stderr
    assert "disjorn.git" in proc.stderr
    assert not (rig.dump / "run-started").exists(), "podman must not have run"


def test_a_missing_own_repo_also_refuses(rig):
    proc = rig.run(name="nosuchresident")
    assert proc.returncode != 0
    assert "REFUSING TO LAUNCH" in proc.stderr
    assert "nosuchresident.git" in proc.stderr
    assert not (rig.dump / "run-started").exists()


def test_the_provisioned_clone_has_no_origin(rig):
    """[1175] The origin rewrite is DELETED, not repointed. With the mount
    gone, a remote aimed at /run/gatehouse is a path that looks real and is
    not; `no such remote` is the better error, and it is the one the session's
    kernel now describes."""
    proc = rig.run()
    assert proc.returncode == 0, proc.stderr
    for repo in ("disjorn", NAME):
        remotes = git("remote", cwd=rig.work / repo).stdout.strip()
        assert remotes == "", f"{repo} still has a remote: {remotes!r}"
        push = git("push", "origin", "HEAD", cwd=rig.work / repo, check=False)
        assert push.returncode != 0
        assert "origin" in (push.stderr + push.stdout)


# ── the harvest ──────────────────────────────────────────────────────────


def test_published_sha_is_measured_in_the_gatehouse(rig):
    """`git push` exiting 0 is a claim about a transport; the banner is a claim
    about what a human finds when they fetch. Only the second is printed."""
    proc = rig.run(build_script=session(rig, "disjorn", n=2))
    assert proc.returncode == 0, proc.stderr + proc.stdout

    workspace_sha = git("rev-parse", BRANCH, cwd=rig.work / "disjorn").stdout.strip()
    gatehouse_sha = head_of(rig.gatehouse / "disjorn.git", BRANCH)
    assert gatehouse_sha == workspace_sha
    assert f"PUBLISHED disjorn.git {workspace_sha}" in proc.stdout.splitlines()
    # and the repo that did nothing gets the honest line, not a phantom branch
    assert f"NO-COMMITS {NAME}.git" in proc.stdout.splitlines()
    assert head_of(rig.gatehouse / f"{NAME}.git", BRANCH) is None


def test_both_entitled_repos_publish_independently(rig):
    script = (session(rig, "disjorn") + "\n"
              + session(rig, NAME).split("\n", 1)[1])
    proc = rig.run(build_script=script)
    assert proc.returncode == 0, proc.stderr + proc.stdout
    for repo in ("disjorn", NAME):
        sha = git("rev-parse", BRANCH, cwd=rig.work / repo).stdout.strip()
        assert f"PUBLISHED {repo}.git {sha}" in proc.stdout.splitlines()
        assert head_of(rig.gatehouse / f"{repo}.git", BRANCH) == sha


def test_zero_commit_build_gets_the_honest_line_and_no_branch(rig):
    """"On the branch for review" without a measured sha becomes impossible to
    print. A build that produced nothing says so."""
    proc = rig.run()
    assert proc.returncode == 0, proc.stderr
    assert f"NO-COMMITS disjorn.git" in proc.stdout.splitlines()
    assert f"NO-COMMITS {NAME}.git" in proc.stdout.splitlines()
    assert not lines(proc, "PUBLISHED ")
    for repo in ("disjorn", NAME):
        assert head_of(rig.gatehouse / f"{repo}.git", BRANCH) is None, (
            "a zero-commit build created a phantom branch in the gatehouse")


def test_non_fast_forward_is_refused_and_never_forced(rig):
    """The gatehouse branch moved under us — another seat's rescue push, or a
    re-run of the same slug. The wrapper is not entitled to decide whose
    commits lose, so it prints git's own refusal and fails."""
    # Pre-seed the gatehouse branch with a DIVERGING commit.
    seed = rig.tmp / "diverge"
    git("clone", "--quiet", str(rig.gatehouse / "disjorn.git"), str(seed))
    git("checkout", "--quiet", "-b", BRANCH, cwd=seed)
    (seed / "theirs.txt").write_text("someone else was here\n")
    git("add", "theirs.txt", cwd=seed)
    git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "theirs", cwd=seed)
    git("push", "--quiet", "origin", f"HEAD:refs/heads/{BRANCH}", cwd=seed)
    theirs = head_of(rig.gatehouse / "disjorn.git", BRANCH)

    proc = rig.run(build_script=session(rig, "disjorn"))
    assert proc.returncode != 0, "a failed publish must fail the wrapper"
    failed = lines(proc, "PUBLISH-FAILED disjorn.git")
    assert len(failed) == 1, proc.stdout
    assert "\n" not in failed[0]
    # git's own words, verbatim enough to diagnose from the banner alone
    assert "non-fast-forward" in failed[0] or "fetch first" in failed[0].lower(), failed[0]
    assert "rejected" in failed[0].lower()
    # and nothing was overwritten
    assert head_of(rig.gatehouse / "disjorn.git", BRANCH) == theirs


def test_no_force_flag_anywhere_in_the_harvest():
    """Belt and braces on the wall above: not --force, not --force-with-lease,
    not a leading + in a refspec. A grep, because the failure mode is someone
    'fixing' the non-ff test by making it pass."""
    code = [ln for ln in RUN_BUILD.read_text().splitlines()
            if not ln.lstrip().startswith("#")]
    for ln in code:
        if "git" in ln and "push" in ln:
            assert "--force" not in ln, ln
            assert "-f " not in ln, ln
            assert '"+' not in ln, ln


def test_nonzero_container_exit_skips_the_harvest_and_keeps_the_clones(rig):
    """A failed build's partial work is not a product and does not get a line
    saying it is. The clones stay where they are; the quarantine clause is what
    protects them at the next launch."""
    proc = rig.run(build_script=session(rig, "disjorn"), rc=7)
    assert proc.returncode == 7, proc.stderr
    assert not lines(proc, "PUBLISHED ")
    assert not lines(proc, "NO-COMMITS ")
    assert lines(proc, "HARVEST-SKIPPED"), proc.stdout
    assert head_of(rig.gatehouse / "disjorn.git", BRANCH) is None
    # left in place, with the commits still on the branch
    assert (rig.work / "disjorn" / ".git").is_dir()
    assert git("rev-parse", BRANCH, cwd=rig.work / "disjorn").stdout.strip()


def test_harvest_lines_are_one_per_entitled_repo_always(rig):
    """The reaper builds the banner from these lines and nothing else, so
    exactly one line per repo has to arrive on every clean exit."""
    proc = rig.run(build_script=session(rig, "disjorn"))
    assert proc.returncode == 0, proc.stderr
    verdicts = [ln.split()[1] for ln in proc.stdout.splitlines()
                if ln.split()[:1] and ln.split()[0] in
                ("PUBLISHED", "NO-COMMITS", "PUBLISH-FAILED")]
    assert sorted(verdicts) == sorted(["disjorn.git", f"{NAME}.git"])


# ── the quarantine clause ────────────────────────────────────────────────
#
# [1175]: harvest failure makes the workspace clone undeletable. The 08-13
# rescue existed only because a human posted a warning in-channel and nobody
# launched in the meantime; a human remembering is not a design.


def unharvested_clone(rig, repo: str, slug: str, content: str):
    """A leftover workspace clone with commits on loop/<slug> that the
    gatehouse has never seen — exactly what a killed or failed harvest
    leaves behind."""
    dest = rig.work / repo
    dest.parent.mkdir(parents=True, exist_ok=True)
    git("clone", "--quiet", str(rig.gatehouse / f"{repo}.git"), str(dest))
    git("checkout", "--quiet", "-b", f"loop/{slug}", cwd=dest)
    (dest / "precious.txt").write_text(content)
    git("add", "precious.txt", cwd=dest)
    git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "precious", cwd=dest)
    return git("rev-parse", "HEAD", cwd=dest).stdout.strip()


def test_unharvested_clone_is_quarantined_not_deleted(rig):
    old_slug = "2026-08-12-earlier-spec"
    sha = unharvested_clone(rig, "disjorn", old_slug, "do not lose me\n")

    proc = rig.run()
    assert proc.returncode == 0, proc.stderr + proc.stdout

    quarantined = lines(proc, "QUARANTINED disjorn ")
    assert len(quarantined) == 1, proc.stdout
    qpath = Path(quarantined[0].split()[2])
    assert qpath.is_dir(), f"quarantine path does not exist: {qpath}"
    assert str(qpath).startswith(str(rig.home_vol / "quarantine"))
    assert old_slug in qpath.name, "the quarantine path must name the lost slug"

    # THE CONTENT IS THE ASSERTION, not just the directory: the commit and the
    # file it carries have to still be readable, or "quarantined" is a word.
    assert git("rev-parse", f"loop/{old_slug}", cwd=qpath).stdout.strip() == sha
    assert (qpath / "precious.txt").read_text() == "do not lose me\n"

    # ...and the build proceeded on a fresh clone alongside it.
    assert (rig.work / "disjorn" / ".git").is_dir()
    assert git("rev-parse", "--abbrev-ref", "HEAD",
               cwd=rig.work / "disjorn").stdout.strip() == BRANCH
    assert not (rig.work / "disjorn" / "precious.txt").exists()


def test_a_harvested_clone_is_deleted_normally(rig):
    """The clause must not turn every leftover into a quarantine pile. A clone
    whose commits a gatehouse REF still reaches is disposable, and a
    zero-commit leftover — a loop branch sitting on the clone point, which
    main contains — is too. "Unharvested" is measured as reachability from
    the gatehouse's own refs, never as bare object existence (see the 08-08
    regression below for why the weaker measurement loses work)."""
    old_slug = "2026-08-12-earlier-spec"
    sha = unharvested_clone(rig, "disjorn", old_slug, "already landed\n")
    git("push", "--quiet", str(rig.gatehouse / "disjorn.git"),
        f"{sha}:refs/heads/loop/{old_slug}", cwd=rig.work / "disjorn")

    proc = rig.run()
    assert proc.returncode == 0, proc.stderr + proc.stdout
    assert not lines(proc, "QUARANTINED"), proc.stdout
    assert not (rig.home_vol / "quarantine").exists()
    assert not (rig.work / "disjorn" / "precious.txt").exists()


def test_objects_landed_ref_died_is_still_quarantined(rig):
    """THE 08-08 REGRESSION (Claudette, #custodian seq 1224). That incident's
    push landed every OBJECT in the gatehouse and then died at the ref
    update: the branch did not exist, but `cat-file -e <sha>` answered yes.
    Under an object-existence measurement this leftover reads as harvested
    and the next launch rm -rf's the only reachable copy of the work. The
    measurement must be REF reachability: a gatehouse ref contains the sha,
    or it did not land."""
    old_slug = "2026-08-12-half-landed"
    sha = unharvested_clone(rig, "disjorn", old_slug, "objects landed, ref died\n")
    bare = rig.gatehouse / "disjorn.git"
    # Land the objects AND the ref, then kill the ref — the objects survive,
    # exactly the state the 08-08 push left behind.
    git("push", "--quiet", str(bare),
        f"{sha}:refs/heads/loop/{old_slug}", cwd=rig.work / "disjorn")
    git("update-ref", "-d", f"refs/heads/loop/{old_slug}", cwd=bare)
    # Precondition of the trap: the object IS present in the gatehouse...
    git("cat-file", "-e", f"{sha}^{{commit}}", cwd=bare)

    proc = rig.run()
    assert proc.returncode == 0, proc.stderr + proc.stdout
    # ...and the clone must be quarantined anyway, work intact.
    quarantined = lines(proc, "QUARANTINED disjorn ")
    assert len(quarantined) == 1, proc.stdout
    qpath = Path(quarantined[0].split()[2])
    assert git("rev-parse", f"loop/{old_slug}", cwd=qpath).stdout.strip() == sha
    assert (qpath / "precious.txt").read_text() == "objects landed, ref died\n"


def test_zero_commit_leftover_is_not_quarantined(rig):
    """The junk-pile guard, stated on its own: a previous build that committed
    nothing left a loop branch on the clone point. Nothing is lost by deleting
    it, and a quarantine directory full of empty clones is one nobody reads."""
    dest = rig.work / "disjorn"
    dest.parent.mkdir(parents=True, exist_ok=True)
    git("clone", "--quiet", str(rig.gatehouse / "disjorn.git"), str(dest))
    git("checkout", "--quiet", "-b", "loop/2026-08-12-nothing-happened", cwd=dest)

    proc = rig.run()
    assert proc.returncode == 0, proc.stderr
    assert not lines(proc, "QUARANTINED"), proc.stdout


def test_quarantine_surfaces_on_stdout_for_the_reaper(rig):
    """stdout, not stderr: stdout is what the broker reaper reads and posts,
    and surfacing is the entire point of the line."""
    unharvested_clone(rig, NAME, "2026-08-11-other", "x\n")
    proc = rig.run()
    assert proc.returncode == 0, proc.stderr
    assert lines(proc, f"QUARANTINED {NAME} ")
    # ...and a human-readable explanation on stderr alongside it
    assert "unharvested commits" in proc.stderr


# ── killing the wrapper kills the container ──────────────────────────────
#
# [1175]. With `exec` gone the wrapper is podman's PARENT, so a killed parent
# could leave a container running and writing into ~/work while the next launch
# deletes it underneath it. The trap covers EXIT/INT/TERM; the watchdog sibling
# (tested in test_run_wrappers.py) covers the SIGKILL no trap can see.


def _wait_for(path: Path, timeout=10.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if path.exists():
            return True
        time.sleep(0.05)
    return False


def test_sigterm_mid_container_run_kills_the_container_via_the_trap(rig):
    """The trap specifically, not the watchdog: the wrapper exits 143 because
    its own TERM handler ran and re-exited: a bash killed by SIGTERM with no
    trap would be reported as -15."""
    p = rig.launch()
    assert _wait_for(rig.dump / "run-started"), "fake podman never started"
    p.terminate()
    p.wait(timeout=15)
    assert p.returncode == 143, (
        f"expected the TERM trap's 128+15, got {p.returncode}")
    assert _wait_for(rig.dump / "reaped"), "container was never reaped"
    reaped = (rig.dump / "reaped").read_text()
    assert "fakecid" in reaped
    assert "-f" in reaped and "-t 0" in reaped and "--ignore" in reaped


def test_sigint_mid_container_run_kills_the_container(rig):
    p = rig.launch()
    assert _wait_for(rig.dump / "run-started")
    p.send_signal(2)                       # SIGINT
    p.wait(timeout=15)
    assert p.returncode == 130
    assert _wait_for(rig.dump / "reaped")


def test_a_killed_wrapper_publishes_nothing(rig):
    """Harvest is skipped by design when the wrapper dies — and the ABSENCE of
    the lines is the signal the reaper turns into a FAILED banner. Silence is
    never read as success."""
    p = rig.launch()
    assert _wait_for(rig.dump / "run-started")
    p.terminate()
    p.wait(timeout=15)
    out = p.stdout.read().decode()
    assert "PUBLISHED" not in out and "NO-COMMITS" not in out
    assert head_of(rig.gatehouse / "disjorn.git", BRANCH) is None
