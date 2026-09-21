"""The gate runner: what the broker reads back, and what it launches.

Two surfaces, both small on purpose. The PARSER decides whether a branch may
merge, so every shape that is not an explicit `pass` has to end up red. The
ARGV is the privileged half: a third mode of the same helper, with no
forwarded arguments at all.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from broker_testlib import *  # noqa: F401,F403

from gates import GateResult, gates_json, run_gates  # noqa: E402

HELPER = Path(__file__).resolve().parent.parent / "disjorn-build-launch"
SLUG = "2026-09-20-a-slug"
SERVER_SUMMARY = "======== 510 passed in 94.20s ========"
HARNESS_SUMMARY = "==== 2 skipped, 850 passed in 60.11s ===="


def fake_gate(tmp_path: Path, stdout: str, stderr: str = "", rc: int = 0,
              sleep: float = 0.0) -> list[str]:
    """A stand-in for `sudo … disjorn-build-launch gate`: it echoes a canned
    run and records the argv it was handed."""
    script = tmp_path / "fake-gate.sh"
    script.write_text(
        "#!/usr/bin/env bash\n"
        f'printf "%s\\n" "$*" > {tmp_path / "argv"}\n'
        + (f"sleep {sleep}\n" if sleep else "")
        + f"cat <<'EOF'\n{stdout}\nEOF\n"
        + (f"cat >&2 <<'EOF'\n{stderr}\nEOF\n" if stderr else "")
        + f"exit {rc}\n")
    script.chmod(0o755)
    return ["bash", str(script)]


def gate(tmp_path, stdout, **kw):
    return run_gates(fake_gate(tmp_path, stdout, **kw), "gable", SLUG,
                     timeout=30, log_dir=str(tmp_path / "logs"))


# ======================================================================
# the parser
# ======================================================================

def test_a_green_run_reads_green(tmp_path):
    r = gate(tmp_path, "GATE tests pass\nGATE typecheck pass\n"
                       "GATE build pass\nGATE exit 0")
    assert (r.tests, r.typecheck, r.build, r.exit_code) == (True, True, True, 0)
    assert gates_json(r) == {"tests": True, "typecheck": True, "build": True}


def test_skipped_client_gates_are_none_and_true_to_the_classifier(tmp_path):
    r = gate(tmp_path, "GATE tests pass\nGATE typecheck skipped\n"
                       "GATE build skipped\nGATE exit 0")
    assert r.tests is True and r.typecheck is None and r.build is None
    assert gates_json(r) == {"tests": True, "typecheck": True, "build": True}


def test_a_missing_line_is_a_fail(tmp_path):
    r = gate(tmp_path, "GATE exit 0", rc=0)
    assert (r.tests, r.typecheck, r.build) == (False, False, False)
    assert gates_json(r) == {"tests": False, "typecheck": False, "build": False}


def test_only_gate_lines_are_read(tmp_path):
    """Everything else the run prints is log. A suite that happens to echo the
    words `GATE tests pass` inside a longer line must not decide a merge."""
    r = gate(tmp_path,
             "collecting ...\nnote: GATE tests pass was expected\n"
             "GATE tests fail\nGATE typecheck skipped\nGATE build skipped\n"
             "GATE exit 1", rc=1)
    assert r.tests is False and r.exit_code == 1


def test_the_reported_exit_beats_the_process_status(tmp_path):
    r = gate(tmp_path, "GATE tests pass\nGATE typecheck skipped\n"
                       "GATE build skipped\nGATE exit 0", rc=0)
    assert r.exit_code == 0
    r = gate(tmp_path, "GATE tests fail\nGATE exit 1", rc=1)
    assert r.exit_code == 1


def test_no_exit_line_falls_back_to_the_process_status(tmp_path):
    r = gate(tmp_path, "GATE tests pass", rc=3)
    assert r.exit_code == 3


def test_a_timeout_is_a_fail(tmp_path):
    r = run_gates(fake_gate(tmp_path, "GATE tests pass", sleep=5), "gable",
                  SLUG, timeout=1, log_dir=str(tmp_path / "logs"))
    assert r.summary == "timed out"
    assert (r.tests, r.typecheck, r.build) == (False, False, False)
    assert gates_json(r) == {"tests": False, "typecheck": False, "build": False}


def test_a_launch_that_never_ran_leaves_the_gates_unset_and_red(tmp_path):
    r = run_gates([str(tmp_path / "nothing-here")], "gable", SLUG,
                  timeout=5, log_dir=str(tmp_path / "logs"))
    assert r.tests is None and r.typecheck is None and r.build is None
    assert gates_json(r)["tests"] is False


def test_the_log_holds_both_streams_and_is_private(tmp_path):
    r = gate(tmp_path, "GATE tests pass\nGATE exit 0",
             stderr="a secret-looking traceback")
    body = Path(r.log_path).read_text()
    assert "GATE tests pass" in body and "traceback" in body
    assert stat.S_IMODE(os.stat(r.log_path).st_mode) == 0o600
    assert Path(r.log_path).parent == tmp_path / "logs"


def test_the_summary_is_one_line_naming_every_gate(tmp_path):
    r = gate(tmp_path, "GATE tests pass\nGATE typecheck pass\n"
                       "GATE build pass\nGATE exit 0",
             stderr=f"{SERVER_SUMMARY}\n{HARNESS_SUMMARY}")
    assert "\n" not in r.summary
    assert "510 passed" in r.summary and "850 passed" in r.summary
    assert "typecheck ok" in r.summary and "build ok" in r.summary


def test_the_counts_come_only_from_pytests_own_summary_lines(tmp_path):
    """A suite that prints `3 passed` on an ordinary line is output, not a
    result: the first summary line is the server suite, the second the
    harness one."""
    green = ("GATE tests pass\nGATE typecheck skipped\n"
             "GATE build skipped\nGATE exit 0")
    r = gate(tmp_path, green,
             stderr=f"collected 3 items\nkept the 3 passed cases\n"
                    f"{SERVER_SUMMARY}\n{HARNESS_SUMMARY}")
    assert "server 510 passed; harness 850 passed" in r.summary
    r = gate(tmp_path, green, stderr="3 passed\n7 passed here too")
    assert r.summary.startswith("tests ok")
    assert "passed" not in r.summary


def test_a_quiet_summary_line_counts_without_the_rule(tmp_path):
    """`pytest -q` prints its final line with no `=====` rule around it."""
    green = ("GATE tests pass\nGATE typecheck skipped\n"
             "GATE build skipped\nGATE exit 0")
    r = gate(tmp_path, green,
             stderr="..s..\n510 passed in 94.20s\n"
                    "2 skipped, 850 passed, 1 warning in 60.11s")
    assert "server 510 passed; harness 850 passed" in r.summary
    r = gate(tmp_path, green,
             stderr=f"a run of 3 passed in 4.0s so far\n{SERVER_SUMMARY}\n"
                    f"{HARNESS_SUMMARY}")
    assert "server 510 passed; harness 850 passed" in r.summary


def test_the_seat_and_slug_are_appended_to_the_prefix(tmp_path):
    gate(tmp_path, "GATE tests pass\nGATE exit 0")
    assert (tmp_path / "argv").read_text().split() == ["gable", SLUG]


# ======================================================================
# the launcher's argv
# ======================================================================

def helper(*args: str, wrapper: str = "/usr/bin/true"):
    env = dict(os.environ, DISJORN_BUILD_LAUNCH_DRY_RUN="1",
               DISJORN_BUILD_LAUNCH_WRAPPER=wrapper)
    return subprocess.run([sys.executable, str(HELPER), *args],
                          capture_output=True, text=True, env=env, timeout=30)


needs_resident = pytest.mark.skipif(
    subprocess.run(["id", "res-gable"], capture_output=True).returncode != 0,
    reason="needs the res-gable account (harness/keyboard/01-users.sh)")


@needs_resident
def test_gate_mode_runs_the_gate_wrapper_as_the_resident():
    cp = helper("gate", "gable", SLUG)
    assert cp.returncode == 0, cp.stderr
    argv = json.loads(cp.stdout)
    assert argv[0] == "/usr/bin/systemd-run"
    assert f"--unit=disjorn-gate-{SLUG}" in argv
    assert "--uid=res-gable" in argv
    # Synchronous, unlike a build: the broker waits on this process for the
    # exit status and reads its stdout.
    assert {"--pipe", "--wait", "--quiet", "--collect"} <= set(argv)
    assert argv[-3:] == ["/usr/bin/true", "gable", SLUG]


@needs_resident
def test_a_gate_unit_can_never_collide_with_a_build_unit():
    gate_argv = json.loads(helper("gate", "gable", SLUG).stdout)
    run_argv = json.loads(helper("run", "gable", SLUG).stdout)
    units = [a for a in gate_argv + run_argv if a.startswith("--unit=")]
    assert units == [f"--unit=disjorn-gate-{SLUG}", f"--unit=disjorn-build-{SLUG}"]


@needs_resident
def test_gate_mode_forwards_nothing():
    """`run` carries a session argv; there is nothing for a caller to steer in
    a gate, so a fourth argument is refused rather than passed on."""
    cp = helper("gate", "gable", SLUG, "--model", "evil")
    assert cp.returncode == 2 and "REFUSED" in cp.stderr


@needs_resident
def test_gate_mode_refuses_every_hostile_slug():
    for slug in ["../../etc/passwd", "2026-09-20-a b", "2026-13-45-x", "",
                 "2026-09-20-X", "2026-09-20-x;rm", "gates"]:
        assert helper("gate", "gable", slug).returncode == 2, slug


@needs_resident
def test_gate_mode_carries_the_gatehouse_and_a_run_root_it_can_write():
    argv = json.loads(helper("gate", "gable", SLUG).stdout)
    setenv = [a for a in argv if a.startswith("--setenv=")]
    assert any(a.startswith("--setenv=RESIDENT_GATEHOUSE=") for a in setenv)
    assert any(a.startswith("--setenv=XDG_RUNTIME_DIR=/run/user/") for a in setenv)
    assert any(a.startswith("--setenv=RESIDENT_GATE_RUNS=/home/res-gable/")
               for a in setenv)


@needs_resident
def test_gate_mode_names_the_res_readable_client_toolchain():
    """A res-* uid cannot traverse /home/plink, so no part of a gate may name a
    path under it."""
    argv = json.loads(helper("gate", "gable", SLUG).stdout)
    assert ("--setenv=RESIDENT_CLIENT_NODE_MODULES=/srv/disjorn-client-node-modules"
            in argv)
    assert not any("/home/plink" in a for a in argv)


@needs_resident
def test_the_gate_unit_carries_the_cap_that_actually_kills_it():
    """The unit's cap is the kill that works; the broker's gate_timeout_sec
    only outwaits it."""
    argv = json.loads(helper("gate", "gable", SLUG).stdout)
    assert "--property=RuntimeMaxSec=1200" in argv


def test_an_unknown_mode_is_refused():
    cp = helper("merge", "gable", SLUG)
    assert cp.returncode == 2 and "unknown mode" in cp.stderr
