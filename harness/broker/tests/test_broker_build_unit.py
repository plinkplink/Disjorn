"""start-build's transient systemd unit: identity, the sudo boundary, disk
bounding, and reattachment (WP-L4 open fork — KEYBOARD-NEXT 6b).

WP-L4 shipped the build as a plain detached subprocess of the broker, which
meant it ran as PLINK: wrong uid for podman keep-id and for SO_PEERCRED, wrong
$HOME, plink's filesystem access driven by a resident-authored spec, and death
(with its narration) on any broker restart. It also bounded build output in the
broker's RAM (BL-D2) but not on disk.

The build now runs as a TRANSIENT SYSTEM SERVICE under the resident's own uid,
launched through one validating setuid-free helper that `sudo` names exactly.
Four surfaces are tested here, in order of how much a mistake would cost:

  1. THE PRIVILEGE BOUNDARY — what the helper accepts and, mostly, refuses;
     that its systemd-run argv can never be steered by its caller; and that the
     sudoers drop-in stays two fixed shapes and never names systemd-run.
  2. IDENTITY — the argv really does carry the resident's uid and the
     deterministic unit name, and the broker agrees with the helper on that name.
  3. DISK — the kernel-enforced ceilings, and the janitor that sweeps spool
     files an interrupted broker left behind.
  4. REATTACHMENT — a build that outlives its broker still gets narrated,
     still releases its slug, and still honours its ORIGINAL deadline.
"""

from __future__ import annotations

import json
import os
import pwd
import re
import subprocess
import sys
import time
from pathlib import Path

import pytest

from broker_testlib import FakeBuildProc  # noqa: F401  (fixtures import first)
from brokerd import BUILD_UNIT_PREFIX, build_unit_name, slug_from_spec_filename

REPO_HARNESS = Path(__file__).resolve().parent.parent.parent
HELPER = REPO_HARNESS / "broker" / "disjorn-build-launch"
SUDOERS = REPO_HARNESS / "keyboard" / "91-disjorn-build.sudoers"
INSTALLED_HELPER = "/usr/local/lib/disjorn/disjorn-build-launch"
GOOD_SLUG = "2026-07-21-gif-picker"


def _account_exists(name: str) -> bool:
    try:
        pwd.getpwnam(name)
        return True
    except KeyError:
        return False


needs_resident = pytest.mark.skipif(
    not _account_exists("res-gable"),
    reason="needs the res-gable account (harness/keyboard/01-users.sh)")


def run_helper(*args: str, wrapper: str | None = None) -> subprocess.CompletedProcess:
    """Invoke the helper in DRY-RUN: every validation runs, nothing execs, and
    the systemd-run argv it WOULD have exec'd comes back as JSON on stdout.

    Dry-run is deliberately env-gated rather than flag-gated: sudo's env_reset
    strips DISJORN_* before the privileged path ever starts, so the sudoers rule
    can only reach the exec'ing branch. Tests get to read the argv; a caller
    with the sudoers grant does not get a second code path."""
    env = dict(os.environ, DISJORN_BUILD_LAUNCH_DRY_RUN="1")
    if wrapper is not None:
        env["DISJORN_BUILD_LAUNCH_WRAPPER"] = wrapper
    return subprocess.run([sys.executable, str(HELPER), *args],
                          capture_output=True, text=True, env=env, timeout=30)


def helper_argv(*args: str, wrapper: str = "/usr/bin/true") -> list[str]:
    cp = run_helper(*args, wrapper=wrapper)
    assert cp.returncode == 0, cp.stderr
    return json.loads(cp.stdout)


# ======================================================================
# 1. THE PRIVILEGE BOUNDARY
# ======================================================================

def test_helper_refuses_every_hostile_slug():
    """The slug is the only resident-influenced token that reaches root, and it
    ends up as a systemd unit name, a git branch and a container name. The
    broker validates it; the helper — the actual privilege boundary — refuses to
    take the broker's word for it."""
    hostile = [
        "../../etc/passwd", "/etc/passwd", "2026-07-21-x/../y",
        "2026-07-21-X", "2026-07-21-a b", "2026-07-21-a;b", "2026-07-21-a$b",
        "-2026-07-21-x", "2026-13-45-x", "202-07-21-x", "gif-picker",
        "2026-07-21-", "2026-07-21-x@evil", "2026-07-21-x.service",
        "2026-07-21-" + "a" * 60, "", "2026-07-21-x\nrm -rf /",
    ]
    for slug in hostile:
        cp = run_helper("run", "gable", slug)
        assert cp.returncode == 2, f"{slug!r} was ACCEPTED: {cp.stdout}"
        assert "REFUSED" in cp.stderr, slug


def test_helper_and_broker_agree_on_which_slugs_are_valid():
    """Two independent validators guarding one name: if they ever disagree, one
    of them is decoration. Anything the broker refuses to derive a slug from
    must also be refused at the boundary."""
    for bad in ["2026-07-21-Bad_Slug.md", "2026-07-21-.md", "2026-07-21-a b.md",
                "voice-notes.md", "202-07-21-x.md", "2026-13-45-x.md"]:
        with pytest.raises(Exception):
            slug_from_spec_filename(bad)
        assert run_helper("run", "gable", bad[:-3]).returncode == 2, bad


def test_helper_refuses_a_resident_it_cannot_resolve():
    """`res-<name>` must already exist in passwd — and creating a user needs
    root, so account membership is a fact the helper cannot be talked into."""
    for name in ["root", "plink", "nope", "../root", "Gable", "gable evil",
                 "res-gable", "", "a" * 40]:
        cp = run_helper("run", name, GOOD_SLUG)
        assert cp.returncode == 2, f"{name!r} was ACCEPTED: {cp.stdout}"


def test_helper_refuses_unknown_modes_and_malformed_calls():
    for args in (["frob", "gable", GOOD_SLUG], ["run"], ["run", "gable"],
                 ["--help"], ["stop", "gable", GOOD_SLUG, "extra"],
                 ["RUN", "gable", GOOD_SLUG]):
        cp = run_helper(*args)
        assert cp.returncode == 2, f"{args} was ACCEPTED: {cp.stdout}"


@needs_resident
def test_helper_refuses_a_wrapper_it_does_not_trust(tmp_path):
    """The one path the helper names must be root-owned and unwritable by
    anyone else. If plink could rewrite it, `sudo <helper>` would quietly become
    `sudo <whatever plink wrote>` — still as a resident, never as root, but not
    a boundary worth arguing about."""
    fake = tmp_path / "run-build.sh"
    fake.write_text("#!/bin/sh\n")
    fake.chmod(0o755)
    cp = run_helper("run", "gable", GOOD_SLUG, wrapper=str(fake))
    assert cp.returncode == 2 and "root-owned" in cp.stderr
    cp = run_helper("run", "gable", GOOD_SLUG, wrapper=str(tmp_path / "absent"))
    assert cp.returncode == 2 and "unusable" in cp.stderr
    # A root-owned, group/world-unwritable file is accepted (/usr/bin/true).
    assert run_helper("run", "gable", GOOD_SLUG,
                      wrapper="/usr/bin/true").returncode == 0


@needs_resident
def test_forwarded_args_can_never_become_systemd_run_options():
    """The session argv is forwarded verbatim to run-build.sh. It must not be
    able to reach systemd-run's option parser — that is the difference between
    'runs as the resident' and 'runs as root'. Two independent reasons it
    cannot: everything after `--` is the command, and systemd-run stops option
    parsing at the command anyway."""
    argv = helper_argv("run", "gable", GOOD_SLUG,
                       "--uid=0", "-p", "User=root",
                       "--property=ExecStartPre=/bin/sh -c id")
    sep = argv.index("--")
    assert argv[sep + 1] == "/usr/bin/true"          # the wrapper, then args
    assert argv[sep + 2:sep + 4] == ["gable", GOOD_SLUG]
    assert argv[sep + 4:] == ["--uid=0", "-p", "User=root",
                              "--property=ExecStartPre=/bin/sh -c id"]
    # ...and nothing hostile leaked into the OPTION run before `--`.
    options = argv[1:sep]
    assert [a for a in options if a.startswith("--uid=")] == ["--uid=res-gable"]
    assert not any("User=root" in a or "ExecStartPre" in a for a in options)


@needs_resident
def test_helper_bounds_the_number_and_size_of_forwarded_args():
    assert run_helper("run", "gable", GOOD_SLUG,
                      *["x"] * 65).returncode == 2
    assert run_helper("run", "gable", GOOD_SLUG, "x" * 5000).returncode == 2
    assert run_helper("run", "gable", GOOD_SLUG, "a\nb").returncode == 2


def test_sudoers_drop_in_stays_a_boundary():
    """The sudoers file is the privilege boundary's outer fence; these are the
    properties that make it one, asserted so a future edit has to argue with a
    test rather than with a comment."""
    text = SUDOERS.read_text()
    rules = [ln.strip() for ln in text.splitlines()
             if ln.strip() and not ln.strip().startswith("#")]
    body = " ".join(rules)
    # It grants exactly one program, by absolute installed path.
    assert INSTALLED_HELPER in body
    assert body.count(INSTALLED_HELPER) == 2          # run + stop, nothing else
    # It NEVER names a general-purpose privileged tool. A wildcard-bearing
    # sudoers rule for systemd-run is equivalent to a grant of full root
    # (sudoers matches args as one concatenated string, so `*` matches spaces
    # and a second `--uid=0` can always be appended).
    for forbidden in ("systemd-run", "/usr/bin/systemctl", "/bin/sh",
                      "/usr/bin/env", "ALL) NOPASSWD: ALL"):
        assert forbidden not in body, forbidden
    # Arguments are constrained by anchored POSIX EREs, not by globs: no bare
    # `*` argument may appear.
    assert " *" not in body and body.count("^") == body.count("$") >= 2
    assert "^run gable " in body and "^stop gable " in body
    # Only plink (the uid the broker runs as) gets it, and only as root.
    assert re.search(r"^plink ALL=\(root\) NOPASSWD:", text, re.M)
    assert not re.search(r"^\s*%", text, re.M)        # no group grants


@pytest.mark.skipif(not os.path.exists("/usr/sbin/visudo"),
                    reason="visudo not installed")
def test_sudoers_drop_in_parses():
    cp = subprocess.run(["/usr/sbin/visudo", "-cf", str(SUDOERS)],
                        capture_output=True, text=True, timeout=30)
    assert cp.returncode == 0, cp.stdout + cp.stderr


# ======================================================================
# 2. IDENTITY
# ======================================================================

@needs_resident
def test_launch_argv_carries_the_resident_uid_and_the_unit_name():
    """The whole point of the fork: the build's kernel identity is the
    resident's, so podman's keep-id, $HOME and the SO_PEERCRED the broker socket
    sees are all correct BY CONSTRUCTION rather than by convention."""
    argv = helper_argv("run", "gable", GOOD_SLUG, "--model", "claude-opus-4-8")
    pw = pwd.getpwnam("res-gable")
    assert argv[0] == "/usr/bin/systemd-run"
    assert f"--unit={BUILD_UNIT_PREFIX}{GOOD_SLUG}" in argv
    assert "--uid=res-gable" in argv
    assert any(a.startswith("--gid=") for a in argv)
    # Passing the broker's descriptors straight through is what keeps BL-D2
    # intact: build output goes to the broker's 0600 spool FILES, not into the
    # privileged broker's address space.
    assert "--pipe" in argv
    assert "--collect" in argv
    # rootless podman needs the resident's own runtime dir, and run-build.sh's
    # documented defaults sit under /home/plink, which a res-* uid cannot
    # traverse (the same three deviations gable-summon.service carries).
    assert f"--setenv=XDG_RUNTIME_DIR=/run/user/{pw.pw_uid}" in argv
    assert "--setenv=RESIDENT_CONFIG_DIR=/srv/disjorn-resident-config/gable" in argv
    assert "--setenv=RESIDENT_HOUSE_MEMORY=/usr/local/lib/disjorn/house_memory" in argv


@needs_resident
def test_helper_and_broker_derive_the_same_unit_name():
    """Two programs name one unit. If they drift, the broker stops, polls and
    re-adopts a unit that does not exist while the real build runs on unwatched."""
    for slug in [GOOD_SLUG, "2026-01-01-a", "2026-12-31-voice-notes-v2"]:
        argv = helper_argv("run", "gable", slug)
        unit = next(a.split("=", 1)[1] for a in argv if a.startswith("--unit="))
        assert unit + ".service" == build_unit_name(slug)
        stop = helper_argv("stop", "gable", slug)
        assert stop == ["/usr/bin/systemctl", "stop", build_unit_name(slug)]


def test_build_unit_name_refuses_a_slug_the_gate_never_produced():
    for bad in ["../x", "2026-13-45-x", "X", "", "2026-07-21-x;y"]:
        with pytest.raises(Exception):
            build_unit_name(bad)


def test_broker_argv_routes_through_sudo_and_the_helper(harness):
    """End to end from the verb: the broker's argv is still config-pure — the
    spec never rides argv — and the privileged prefix is exactly
    `sudo -n <helper> run`."""
    harness.set_verbs(**{"start-build": True})
    harness.broker.start_build["command"] = [
        "sudo", "-n", INSTALLED_HELPER, "run"]
    spawn = harness.use_fake_build()
    harness.write_spec(f"{GOOD_SLUG}.md")
    assert harness.call("start-build", {"spec": f"{GOOD_SLUG}.md"})["ok"] is True
    harness.broker.join_builds()
    (argv,) = spawn.calls
    assert argv[:6] == ["sudo", "-n", INSTALLED_HELPER, "run", "gable", GOOD_SLUG]
    assert argv[-2:] == ["--model", "claude-opus-4-8"]
    assert not any("Confirm record" in a for a in argv)


def test_result_names_the_unit(harness):
    harness.set_verbs(**{"start-build": True})
    harness.use_fake_build()
    harness.write_spec(f"{GOOD_SLUG}.md")
    r = harness.call("start-build", {"spec": f"{GOOD_SLUG}.md"})["result"]
    assert r["unit"] == f"{BUILD_UNIT_PREFIX}{GOOD_SLUG}.service"
    harness.broker.join_builds()


# ======================================================================
# 3. DISK (and the rest of the kernel-enforced envelope)
# ======================================================================

@needs_resident
def test_launch_argv_bounds_disk_memory_and_wall_clock():
    """BL-D2 bounded build output in the broker's RAM; it stayed unbounded on
    DISK — a runaway build could write for a whole timeout_sec into
    /var/log/disjorn-broker/build-logs and fill /. RLIMIT_FSIZE is the fix, and
    it is the right one because the build's stdout IS a regular file: the spool
    the broker opened. The build dies loudly on SIGXFSZ instead of the host
    filling up quietly."""
    argv = helper_argv("run", "gable", GOOD_SLUG)
    props = dict(a.split("=", 1)[1].split("=", 1)
                 for a in argv if a.startswith("--property="))
    fsize = int(props["LimitFSIZE"])
    assert 0 < fsize <= 2 * 1024 ** 3
    assert props["LimitCORE"] == "0"          # no core dumps into the spool dir
    assert props["MemorySwapMax"] == "0"
    assert props["MemoryMax"].endswith("G")
    assert int(props["TasksMax"]) > 0
    # The outer wall-clock backstop: a build orphaned by a broker crash still
    # dies on its own. Deliberately LONGER than the broker's own cap (3600s),
    # which the reaper enforces — they are layers, not duplicates.
    assert int(props["RuntimeMaxSec"]) >= 3600


def test_spool_files_and_sidecars_are_swept_when_a_broker_died_mid_build(harness):
    """The other half of bounding disk: files an interrupted broker left behind.
    Nothing else ever deletes them, so without this sweep build-logs grows by
    two files per crashed build, forever."""
    d = harness.build_log_dir
    orphan_out = d / f"{BUILD_UNIT_PREFIX}2026-07-21-dead.abc.out"
    orphan_err = d / f"{BUILD_UNIT_PREFIX}2026-07-21-dead.abc.err"
    orphan_out.write_text("x" * 1000)
    orphan_err.write_text("boom")
    unrelated = d / "keep-me.txt"
    unrelated.write_text("not ours")

    assert harness.broker.adopt_inflight_builds() == []
    assert not orphan_out.exists() and not orphan_err.exists()
    assert unrelated.exists()          # the sweep only claims its own names


def test_a_live_adopted_builds_spool_is_not_swept(harness):
    """The sweep must not delete the files of a build that is still running —
    that is where its report is going to land."""
    slug = "2026-07-21-live"
    out_p, err_p, out_fh, err_fh = harness.broker._open_build_logs(slug)
    harness.broker._close_build_logs(out_fh, err_fh)
    harness.broker._write_build_sidecar(
        {"slug": slug, "branch": f"loop/{slug}", "confirmed_by": "plink",
         "seq": 139, "resident": "res-test"},
        out_path=out_p, err_path=err_p, timeout=30)
    harness.set_unit_state(slug, "active")
    harness.broker.BUILD_POLL_SEC = 0.01

    assert harness.broker.adopt_inflight_builds() == [slug]
    assert os.path.exists(out_p) and os.path.exists(err_p)
    harness.set_unit_state(slug, "inactive")
    harness.broker.join_builds()
    assert harness.build_log_files() == []


# ======================================================================
# 4. REATTACHMENT
# ======================================================================

def _sidecars(harness) -> list[Path]:
    return [p for p in harness.build_log_files() if p.name.endswith(".build.json")]


def test_a_sidecar_records_what_a_future_broker_needs(harness):
    """Persistence is what makes adoption possible at all: the unit name, the
    branch, both spool paths and the ORIGINAL deadline (so a restart does not
    hand a build a fresh hour)."""
    harness.set_verbs(**{"start-build": True})
    proc = FakeBuildProc(out=b"{}", block=True)
    harness.use_fake_build(proc_factory=lambda: proc)
    harness.write_spec(f"{GOOD_SLUG}.md")
    assert harness.call("start-build", {"spec": f"{GOOD_SLUG}.md"})["ok"] is True

    (side,) = _sidecars(harness)
    rec = json.loads(side.read_text())
    assert rec["slug"] == GOOD_SLUG
    assert rec["unit"] == build_unit_name(GOOD_SLUG)
    assert rec["branch"] == f"loop/{GOOD_SLUG}"
    assert rec["confirmed_by"] == "plink" and rec["seq"] == 139
    assert os.path.basename(rec["out_path"]).endswith(".out")
    assert rec["timeout_sec"] == 30
    assert rec["deadline"] > time.time()
    assert os.stat(side).st_mode & 0o777 == 0o600
    proc.release.set()
    harness.broker.join_builds()
    assert _sidecars(harness) == []          # gone on the terminal transition


@pytest.mark.parametrize("factory,expect", [
    (lambda: FakeBuildProc(out=b'{"files": [], "tests": "ok", "diff": ""}'),
     "build done"),
    (lambda: FakeBuildProc(err=b"boom", rc=1), "BUILD FAILED"),
    (lambda: FakeBuildProc(raise_timeout=True), "BUILD FAILED"),
])
def test_the_sidecar_is_removed_on_every_exit_path(harness, factory, expect):
    harness.set_verbs(**{"start-build": True})
    harness.use_fake_build(proc_factory=factory)
    harness.write_spec("2026-07-21-cleanup2.md")
    assert harness.call("start-build", {"spec": "2026-07-21-cleanup2.md"})["ok"]
    harness.broker.join_builds()
    assert any(b.startswith(expect) for b in [p["body"] for p in harness.proposals])
    assert harness.build_log_files() == []


def test_a_never_launched_build_leaves_no_sidecar(harness):
    harness.set_verbs(**{"start-build": True})

    def _boom(argv, *, stdout, stderr):
        raise OSError("no such file")

    harness.broker._build_spawn = _boom
    harness.write_spec("2026-07-21-nospawn2.md")
    assert harness.call("start-build",
                        {"spec": "2026-07-21-nospawn2.md"})["error"]["code"] == "exec-failure"
    assert harness.build_log_files() == []


def test_timeout_stops_the_unit_not_just_the_local_process(harness):
    """The regression this whole change could have introduced: the build now
    lives OUTSIDE the broker's cgroup, so killing our local sudo/systemd-run
    process leaves it running. The cap only still bites because the reaper asks
    systemd to stop the unit — through the same validating helper, in its one
    other fixed shape."""
    harness.set_verbs(**{"start-build": True})
    proc = FakeBuildProc(raise_timeout=True)
    harness.use_fake_build(proc_factory=lambda: proc)
    harness.write_spec("2026-07-21-hang2.md")
    assert harness.call("start-build", {"spec": "2026-07-21-hang2.md"})["ok"]
    harness.broker.join_builds()
    assert harness.stop_calls() == [["stop", "gable", "2026-07-21-hang2"]]
    assert proc.killed is True           # and the local process is reaped too
    assert any(b.startswith("BUILD FAILED") and "timed out" in b
               for b in [p["body"] for p in harness.proposals])


def _plant(harness, slug: str, *, out: bytes = b"", err: bytes = b"",
           timeout: int = 30, deadline: float | None = None) -> dict:
    """Stand in for 'the previous broker process launched this build' — spool
    files plus a sidecar, exactly what it would have left."""
    out_p, err_p, out_fh, err_fh = harness.broker._open_build_logs(slug)
    out_fh.write(out)
    err_fh.write(err)
    harness.broker._close_build_logs(out_fh, err_fh)
    path = harness.broker._write_build_sidecar(
        {"slug": slug, "branch": f"loop/{slug}", "confirmed_by": "plink",
         "seq": 139, "resident": "res-test"},
        out_path=out_p, err_path=err_p, timeout=timeout)
    if deadline is not None:
        rec = json.loads(Path(path).read_text())
        rec["deadline"] = deadline
        Path(path).write_text(json.dumps(rec))
    return {"out": out_p, "err": err_p, "sidecar": path}


def test_a_build_that_finished_while_the_broker_was_down_is_still_narrated(harness):
    """The plain restart case: the unit outlived the broker, finished, and was
    --collect'ed away. Its report is still on disk, so the done line still lands
    — which is the whole reason the sidecar exists."""
    slug = "2026-07-21-orphan"
    _plant(harness, slug,
           out=b'{"files": ["a.py"], "tests": "9 passed", "diff": "+3 -1"}')
    assert harness.broker.adopt_inflight_builds() == []
    (post,) = harness.proposals
    assert post["body"].startswith(f"build done | {slug} -> loop/{slug}")
    assert "files: a.py" in post["body"] and "9 passed" in post["body"]
    assert harness.build_log_files() == []


def test_a_build_that_vanished_without_a_report_fails_loud(harness):
    """No report means we do not know it worked, and a broker that guesses is
    worse than one that says so. Fail loud, never fail over."""
    slug = "2026-07-21-vanished"
    _plant(harness, slug, err=b"segfault")
    harness.set_unit_state(slug, "failed")
    assert harness.broker.adopt_inflight_builds() == []
    (post,) = harness.proposals
    assert post["body"].startswith("BUILD FAILED")
    assert "outcome unknown" in post["body"] and "segfault" in post["body"]
    assert harness.build_log_files() == []


def test_a_still_running_build_is_re_adopted_and_narrated_when_it_lands(harness):
    """The case the deferral was really about: a build in flight across a broker
    restart. It keeps running (it is outside the cgroup), the new process
    re-claims its slug, and the done line still arrives."""
    slug = "2026-07-21-inflight"
    harness.broker.BUILD_POLL_SEC = 0.01
    _plant(harness, slug,
           out=b'{"files": ["b.py"], "tests": "ok", "diff": "+1 -0"}')
    harness.set_unit_state(slug, "active")

    assert harness.broker.adopt_inflight_builds() == [slug]
    assert not harness.proposals              # nothing narrated while it runs
    with harness.broker._build_lock:
        assert slug in harness.broker._active_builds   # slug is claimed again

    harness.set_unit_state(slug, "inactive")  # the build lands
    harness.broker.join_builds()
    (post,) = harness.proposals
    assert post["body"].startswith(f"build done | {slug}")
    assert harness.build_log_files() == []
    with harness.broker._build_lock:
        assert slug not in harness.broker._active_builds


def test_an_adopted_build_refuses_a_duplicate_launch_of_the_same_spec(harness):
    """BL-D4's in-flight claim has to survive the restart too, or a restart
    becomes the way to get two builds racing one branch."""
    slug = "2026-07-21-dup"
    harness.broker.BUILD_POLL_SEC = 0.01
    _plant(harness, slug, out=b"{}")
    harness.set_unit_state(slug, "active")
    assert harness.broker.adopt_inflight_builds() == [slug]

    harness.set_verbs(**{"start-build": True})
    spawn = harness.use_fake_build()
    harness.write_spec(f"{slug}.md")
    resp = harness.call("start-build", {"spec": f"{slug}.md"})
    assert resp["ok"] is False and resp["error"]["code"] == "bad-args"
    assert "already running" in resp["error"]["message"]
    assert spawn.calls == []

    harness.set_unit_state(slug, "inactive")
    harness.broker.join_builds()


def test_an_adopted_build_honours_the_original_deadline(harness):
    """A restart must not hand a build a fresh hour: the deadline travels in the
    sidecar, and past it the adopted reaper stops the unit like the first one
    would have."""
    slug = "2026-07-21-late"
    harness.broker.BUILD_POLL_SEC = 0.01
    _plant(harness, slug, out=b"{}", deadline=time.time() - 5)
    harness.set_unit_state(slug, "active")
    assert harness.broker.adopt_inflight_builds() == [slug]
    harness.broker.join_builds()
    assert harness.stop_calls() == [["stop", "gable", slug]]
    (post,) = harness.proposals
    assert post["body"].startswith("BUILD FAILED") and "timed out" in post["body"]
    assert harness.build_log_files() == []


def test_adoption_leaves_a_build_this_process_owns_alone(harness):
    """Adoption runs at startup, but it must be safe whenever it runs: a build
    this process launched is already being reaped, so its ticket and spool must
    not be narrated twice or swept out from under it."""
    harness.set_verbs(**{"start-build": True})
    proc = FakeBuildProc(out=b'{"files": ["a.py"], "tests": "ok", "diff": ""}',
                         block=True)
    spawn = harness.use_fake_build(proc_factory=lambda: proc)
    harness.write_spec("2026-07-21-mine.md")
    assert harness.call("start-build", {"spec": "2026-07-21-mine.md"})["ok"]
    (out_path, err_path), = spawn.log_paths

    assert harness.broker.adopt_inflight_builds() == []
    assert os.path.exists(out_path) and os.path.exists(err_path)
    assert not any("build done" in p["body"] for p in harness.proposals)

    proc.release.set()
    harness.broker.join_builds()
    assert len([p for p in harness.proposals if "build done" in p["body"]]) == 1
    assert harness.build_log_files() == []


def test_adoption_never_launches_anything(harness):
    """Adoption observes, narrates and tidies. It must never start a build: no
    confirm gate runs at startup, so a spawn here would be a build nobody
    authorized."""
    spawn = harness.use_fake_build()
    _plant(harness, "2026-07-21-noexec", out=b"{}")
    _plant(harness, "2026-07-21-noexec2", out=b"{}")
    harness.set_unit_state("2026-07-21-noexec2", "active")
    harness.broker.BUILD_POLL_SEC = 0.01
    harness.broker.adopt_inflight_builds()
    harness.set_unit_state("2026-07-21-noexec2", "inactive")
    harness.broker.join_builds()
    assert spawn.calls == []


@pytest.mark.parametrize("body", [
    "not json at all",
    json.dumps({"slug": "../../etc/passwd", "branch": "x"}),
    json.dumps({"slug": "2026-13-45-x"}),
    json.dumps({"branch": "loop/x"}),                 # no slug
    json.dumps([1, 2, 3]),
])
def test_a_corrupt_or_hostile_sidecar_is_discarded_not_obeyed(harness, body):
    """The sidecar lives in a plink-owned 0700 directory, so this should be
    unreachable — but it feeds a unit name and two paths straight into
    startup-time code, so it is treated as hostile input anyway."""
    bad = harness.build_log_dir / "2026-07-21-bad.build.json"
    bad.write_text(body)
    assert harness.broker.adopt_inflight_builds() == []
    assert not bad.exists()
    assert harness.proposals == []


def test_a_second_restart_does_not_strand_a_still_running_build(harness):
    """The ticket is torn up only on a TERMINAL state. If this broker shuts down
    while the build is still going, the sidecar and spool must survive — losing
    the ticket while the unit runs is the one way to strand a build for good."""
    slug = "2026-07-21-twice-restarted"
    harness.broker.BUILD_POLL_SEC = 0.01
    planted = _plant(harness, slug, out=b"{}")
    harness.set_unit_state(slug, "active")
    assert harness.broker.adopt_inflight_builds() == [slug]

    harness.broker._closed = True          # what shutdown() does
    harness.broker.join_builds()
    assert os.path.exists(planted["sidecar"]) and os.path.exists(planted["out"])
    assert harness.proposals == []         # nothing claimed, nothing narrated

    # ...and a FRESH broker over the same directory still adopts it. (A real
    # restart is a new process: no in-memory slug claims, closed flag clear.)
    harness.broker._closed = False
    with harness.broker._build_lock:
        harness.broker._active_builds.clear()
    assert harness.broker.adopt_inflight_builds() == [slug]
    harness.set_unit_state(slug, "inactive")
    harness.broker.join_builds()
    assert harness.build_log_files() == []


def test_adoption_survives_an_unreadable_log_dir(harness, monkeypatch):
    """Adoption is legibility, not control: it can never stop the broker coming
    up, or one lost narration costs every resident its hands."""
    monkeypatch.setattr(harness.broker, "_build_log_dir",
                        lambda: "/nonexistent/build-logs")
    assert harness.broker.adopt_inflight_builds() == []
