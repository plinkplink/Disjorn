"""09-build-lane-preflight.sh — the checks that run before the first build.

The pre-flight's job is to find, at the keyboard and in seconds, the four
classes of thing that otherwise surface as a build dying forty seconds in with
a message about something else: a stale deploy, a gatehouse repo whose group
layer does not hold, a gatehouse `main` behind the canonical one, and a seat
whose credential or spine ground is not there.

These tests build a scratch version of the whole layout (every path the script
reads is env-overridable) and check that each failure is FOUND and NAMED. What
they deliberately do not cover is the privileged half — the `sudo -u res-<name>`
seat probes — which cannot be faked without becoming exactly the
keyboard-reported verification the 2026-08-07 lesson forbids. Instead, the
unprivileged run is asserted to FAIL rather than pass quietly.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

KEYBOARD = Path(__file__).resolve().parent.parent
SCRIPT = KEYBOARD / "09-build-lane-preflight.sh"
IS_ROOT = os.getuid() == 0


def test_every_keyboard_script_parses():
    """bash -n on the lane's scripts. Cheap, and a syntax error in a script
    plink runs with sudo is a bad way to find out."""
    for name in ("08-gatehouse-repo.sh", "09-build-lane-preflight.sh",
                 "06-spine-mirror.sh", "01-users.sh", "04-broker.sh"):
        proc = subprocess.run(["bash", "-n", str(KEYBOARD / name)],
                              capture_output=True, text=True)
        assert proc.returncode == 0, f"{name}: {proc.stderr}"


@pytest.fixture()
def lane(tmp_path):
    """A scratch build lane: a fake repo, a fake deploy tree, a fake /etc.

    Returns (paths, run). `run` invokes the pre-flight against the scratch tree
    and returns the CompletedProcess; its combined output is what the tests read.
    """
    repo = tmp_path / "repo"
    lib = tmp_path / "lib"
    etc = tmp_path / "etc"
    gate = tmp_path / "gatehouse"
    spine = tmp_path / "spine"
    cfg = tmp_path / "build-config"
    for d in (lib, etc, gate, spine, cfg):
        d.mkdir()

    # The repo copies the deploy is diffed against.
    sources = {
        "harness/broker/disjorn-build-launch": "#!/usr/bin/python3\nlaunch\n",
        "harness/cc/run-build.sh": "#!/usr/bin/env bash\n# NO BROKER SOCKET\nbuild\n",
        "harness/cc/run-resident.sh": "#!/usr/bin/env bash\nresident\n",
        "harness/cc/build-kernel.md": "# Build session\n",
        "harness/keyboard/91-disjorn-build.sudoers": "plink ALL=(root) NOPASSWD: X\n",
        "harness/broker/broker.toml": '[start_build]\nresident = "gable"\n',
        "harness/broker/verbs.toml": '[res-gable]\n"start-build" = false\n',
        "harness/house_memory/house_memory/__init__.py": "\n",
    }
    for rel, text in sources.items():
        p = repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)

    # A deploy that matches, to start from.
    deployed = {
        "disjorn-build-launch": "harness/broker/disjorn-build-launch",
        "run-build.sh": "harness/cc/run-build.sh",
        "run-resident.sh": "harness/cc/run-resident.sh",
        "build-kernel.md": "harness/cc/build-kernel.md",
    }
    for dst, src in deployed.items():
        (lib / dst).write_text((repo / src).read_text())
    subprocess.run(["cp", "-r", str(repo / "harness/house_memory/house_memory"),
                    str(lib / "house_memory")], check=True)
    (etc / "broker.toml").write_text((repo / "harness/broker/broker.toml").read_text())
    (etc / "verbs.toml").write_text((repo / "harness/broker/verbs.toml").read_text())

    paths = dict(repo=repo, lib=lib, etc=etc, gate=gate, spine=spine, cfg=cfg,
                 tmp=tmp_path)

    def run(resident="gable", **overrides):
        env = dict(os.environ)
        env.update(
            REPO_ROOT=str(repo),
            DISJORN_LIB=str(lib),
            BROKER_ETC=str(etc),
            GATEHOUSE_DIR=str(gate),
            SPINE_MIRROR_ROOT=str(spine),
            BUILD_CONFIG_ROOT=str(cfg),
        )
        env.update({k: str(v) for k, v in overrides.items()})
        proc = subprocess.run(["bash", str(SCRIPT), resident],
                              capture_output=True, text=True, env=env)
        proc.all = proc.stdout + proc.stderr        # type: ignore[attr-defined]
        return proc

    return paths, run


# ── 1. the stale-deploy family ───────────────────────────────────────────

def test_a_matching_deploy_reports_identical(lane):
    paths, run = lane
    out = run().all
    assert "identical: " in out
    assert "STALE DEPLOY" not in out


def test_an_edited_deployed_copy_is_caught_and_the_diff_printed(lane):
    """The exact shape that cost this project four evenings: the deployed file
    and the repo file disagree, and everything downstream reads like a code bug."""
    paths, run = lane
    (paths["lib"] / "run-build.sh").write_text(
        "#!/usr/bin/env bash\n# NO BROKER SOCKET\nbuild\n# hand-edited on the box\n")
    out = run().all
    assert "STALE DEPLOY" in out
    assert "run-build.sh" in out
    assert "hand-edited on the box" in out, "the diff itself must be printed, not just the verdict"


def test_a_never_deployed_file_is_caught(lane):
    """`run-build.sh` had never been deployed while [start_build].command
    already pointed at it — so start-build failed on invocation regardless of
    its verb flag. Absence is a louder failure than difference, not a quieter one."""
    paths, run = lane
    (paths["lib"] / "disjorn-build-launch").unlink()
    out = run().all
    assert "NOT DEPLOYED" in out
    assert "disjorn-build-launch" in out


def test_config_differing_from_its_template_is_a_note_not_a_failure(lane):
    """broker.toml is STATE. It is MEANT to differ — uids, the pin, the switches.
    Counting that as a failure would train everyone to ignore the whole report."""
    paths, run = lane
    (paths["etc"] / "broker.toml").write_text(
        '[start_build]\nresident = "gable"\nmodel = "claude-opus-4-8"\n')
    out = run().all
    assert "differs from the template" in out
    assert "STALE DEPLOY" not in out
    assert "claude-opus-4-8" in out, "the config diff is printed for reading"


# ── 3. the stale-base hazard ─────────────────────────────────────────────

def _bare_with_main(path: Path, content: str):
    """A bare repo carrying one commit on main, made without a worktree."""
    subprocess.run(["git", "init", "--bare", "-b", "main", str(path)],
                   check=True, capture_output=True)
    env = dict(os.environ, GIT_DIR=str(path))
    blob = subprocess.run(["git", "hash-object", "-w", "--stdin"], input=content,
                          text=True, capture_output=True, check=True, env=env).stdout.strip()
    subprocess.run(["git", "update-index", "--add", "--cacheinfo",
                    f"100644,{blob},f"], check=True, capture_output=True,
                   env=dict(env, GIT_INDEX_FILE=str(path / "tmpindex")))
    tree = subprocess.run(["git", "write-tree"], capture_output=True, text=True,
                          check=True, env=dict(env, GIT_INDEX_FILE=str(path / "tmpindex"))).stdout.strip()
    commit = subprocess.run(["git", "commit-tree", tree, "-m", "c"],
                            capture_output=True, text=True, check=True,
                            env=dict(env, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t",
                                     GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t")).stdout.strip()
    subprocess.run(["git", "update-ref", "refs/heads/main", commit],
                   check=True, capture_output=True, env=env)
    return commit


def test_gatehouse_main_behind_canonical_is_caught(lane):
    """The 2026-08-07 hazard, live: a build clones from the gatehouse, so
    gatehouse main IS the base. Merge and forget to push it back, and the next
    build branches off yesterday."""
    paths, run = lane
    canon = paths["tmp"] / "canonical"
    subprocess.run(["git", "init", "-b", "main", str(canon)], check=True, capture_output=True)
    (canon / "f").write_text("new\n")
    for cmd in (["git", "add", "f"], ["git", "-c", "user.name=t", "-c", "user.email=t@t",
                                      "commit", "-m", "c"]):
        subprocess.run(cmd, cwd=canon, check=True, capture_output=True)
    _bare_with_main(paths["gate"] / "disjorn.git", "old\n")

    out = run(REPO_ROOT=str(canon)).all
    assert "STALE BASE" in out
    assert "Push main back to the gatehouse" in out


def test_gatehouse_repo_with_no_main_is_reported(lane):
    paths, run = lane
    subprocess.run(["git", "init", "--bare", str(paths["gate"] / "disjorn.git")],
                   check=True, capture_output=True)
    out = run().all
    assert "has no main branch" in out


# ── 4. the credential route ──────────────────────────────────────────────

def _build_config(paths, text, mode=0o600):
    d = paths["cfg"] / "gable"
    d.mkdir(exist_ok=True)
    (d / "settings.json").write_text("{}\n")
    env = d / "env"
    env.write_text(text)
    env.chmod(mode)
    return env


def test_api_key_only_is_a_failure_for_the_build_seat(lane):
    """The build seat is Max-only. Finding this at the keyboard beats finding it
    as a launch refusal, and BOTH beat silently spending the metered key."""
    paths, run = lane
    _build_config(paths, "ANTHROPIC_API_KEY=sk-ant-api03-PLACEHOLDER\n")
    out = run().all
    assert "ONLY ANTHROPIC_API_KEY" in out
    assert "REFUSE TO LAUNCH" in out
    assert "claude setup-token" in out


def test_oauth_token_is_the_pass(lane):
    paths, run = lane
    _build_config(paths, "CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat01-PLACEHOLDER\n")
    out = run().all
    assert "routes to the Max account" in out


def test_both_credentials_present_is_a_note(lane):
    """Safe today — OAuth wins and the key is filtered — but a metered key in a
    Max-only seat's env file is a route waiting to be taken by accident."""
    paths, run = lane
    _build_config(paths,
                  "CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat01-PLACEHOLDER\n"
                  "ANTHROPIC_API_KEY=sk-ant-api03-PLACEHOLDER\n")
    out = run().all
    assert "holds BOTH credentials" in out
    assert "Remove it" in out


def test_no_credential_at_all_is_a_failure(lane):
    paths, run = lane
    _build_config(paths, "BROKER_DISABLE=1\n")
    out = run().all
    assert "holds neither" in out


def test_a_world_readable_credential_file_is_a_failure(lane):
    paths, run = lane
    _build_config(paths, "CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat01-PLACEHOLDER\n", mode=0o644)
    out = run().all
    assert "should not be group- or world-readable" in out


def test_the_preflight_never_prints_a_credential_value(lane):
    """It reads the env file to decide the route. It must never echo what it read."""
    paths, run = lane
    secret = "sk-ant-oat01-PLACEHOLDER-NOT-A-REAL-TOKEN"
    _build_config(paths, f"CLAUDE_CODE_OAUTH_TOKEN={secret}\n")
    out = run().all
    assert secret not in out


def test_missing_build_config_dir_is_a_failure(lane):
    """run-build.sh exits before podman without it — loudly, but forty seconds
    into a build slot that has already been counted."""
    paths, run = lane
    out = run().all
    assert "build config dir missing" in out


# ── 4b. the spine mirror ─────────────────────────────────────────────────

def test_absent_spine_mirror_is_a_note_not_a_failure(lane):
    """No mirror published => disjorn-build-launch sets no RESIDENT_SPINE_HOST
    => no mount => exactly today's invocation. That is a choice, not a fault."""
    paths, run = lane
    out = run().all
    assert "no spine mirror at" in out
    assert "unchanged behaviour" in out


@pytest.mark.skipif(IS_ROOT, reason="covers the unprivileged path")
def test_a_present_spine_mirror_cannot_be_cleared_without_root(lane):
    """Root's own answer to 'is this writable by the resident' is worthless, so
    an unprivileged run must refuse to answer rather than answer wrongly."""
    paths, run = lane
    (paths["spine"] / "gable").mkdir()
    out = run().all
    assert "NOT ROOT" in out
    assert "root's own answer is worth nothing here" in out


# ── 5. the verb surface, and BR-1 ────────────────────────────────────────

def test_two_residents_with_start_build_raises_br1(lane):
    """BR-1: the build identity is one global config value, not the caller's
    SO_PEERCRED uid. With two residents holding the verb the ledger records a
    name and not an actor — 'wrong half the time'."""
    paths, run = lane
    (paths["etc"] / "verbs.toml").write_text(
        '[res-claudette]\n"start-build" = true\n\n[res-gable]\n"start-build" = true\n')
    out = run().all
    assert "That is BR-1" in out
    assert "required before a second resident gets the verb" in out


def test_one_resident_with_start_build_does_not_raise_br1(lane):
    paths, run = lane
    (paths["etc"] / "verbs.toml").write_text(
        '[res-claudette]\n"start-build" = false\n\n[res-gable]\n"start-build" = true\n')
    out = run().all
    assert "That is BR-1" not in out
    assert "start-build is ON for: res-gable" in out


def test_deployed_build_wrapper_mounting_the_broker_socket_is_caught(lane):
    """No verbs at all in a build container, and it is a MOUNT that decides it —
    checked against the DEPLOYED copy, which is the one that runs."""
    paths, run = lane
    (paths["lib"] / "run-build.sh").write_text(
        '#!/usr/bin/env bash\nargs+=( -v "$X:/run/disjorn-broker:ro" )\n')
    out = run().all
    assert "mounts the broker socket" in out


# ── the verdict line ─────────────────────────────────────────────────────

def test_any_failure_means_not_ready_and_a_nonzero_exit(lane):
    paths, run = lane
    proc = run()
    assert proc.returncode != 0
    assert "NOT READY" in proc.all


def test_a_bad_resident_name_is_refused(lane):
    paths, run = lane
    proc = run("res-gable")
    assert proc.returncode != 0
    assert "plain lowercase name" in proc.all
