"""Credential selection in run-resident.sh / run-build.sh.

These wrappers `exec podman`, so the way to test them without podman (and
without any real credential) is to put a FAKE podman first on PATH. The
fake dumps, to files the test reads:

    argv       — NUL-separated, so we can prove no secret is in the process
                 table (this is the whole point of the name-only `--env VAR`
                 form: `--env VAR=value` would put an account credential in
                 /proc/*/cmdline for every user on the host to read);
    environ    — NUL-separated, what podman itself was handed;
    envfile    — the contents of the filtered env-file podman was pointed at
                 (read through the inherited fd), i.e. exactly what podman's
                 own parser would have injected into the container.

Container env == (envfile dump) + (the name-only --env vars taken from
environ). So these three dumps determine, precisely, what the resident
session would see. No podman, no image, no network, no real token.
"""

from __future__ import annotations

import os
import re
import subprocess
import time
from pathlib import Path

import pytest

CC_DIR = Path(__file__).resolve().parent.parent
WRAPPERS = {
    "run-resident.sh": ["smoketest"],
    "run-build.sh": ["smoketest", "some-slug"],
}

# Obvious placeholders. Nothing here is or resembles a live credential.
FAKE_OAUTH = "sk-ant-oat01-PLACEHOLDER-NOT-A-REAL-TOKEN"
FAKE_APIKEY = "sk-ant-api03-PLACEHOLDER-NOT-A-REAL-KEY"

FAKE_PODMAN = r"""#!/usr/bin/env bash
# Fake podman for harness/cc/tests/test_run_wrappers.py. Dumps what the real
# podman would have been given, then exits 0 without running anything.
set -u
printf '%s\0' "$@" > "$DUMP_DIR/argv"
env -0 > "$DUMP_DIR/environ"
: > "$DUMP_DIR/envfile"
prev=""
for a in "$@"; do
  if [ "$prev" = "--env-file" ]; then cat "$a" >> "$DUMP_DIR/envfile"; fi
  prev="$a"
done
exit 0
"""


def seed_bare_repo(path: Path, scratch: Path):
    """A bare repo with one commit on `main`, built with plumbing only.

    One commit and not zero, so the clone the wrapper makes has a base to
    branch from — the same shape a real gatehouse repo has.
    """
    subprocess.run(["git", "init", "--bare", "-b", "main", str(path)],
                   check=True, capture_output=True)
    env = dict(os.environ, GIT_DIR=str(path))
    blob = subprocess.run(["git", "hash-object", "-w", "--stdin"], input="x\n",
                          text=True, capture_output=True, check=True,
                          env=env).stdout.strip()
    idx = dict(env, GIT_INDEX_FILE=str(scratch / f"idx-{path.name}"))
    subprocess.run(["git", "update-index", "--add", "--cacheinfo",
                    f"100644,{blob},f"], check=True, capture_output=True, env=idx)
    tree = subprocess.run(["git", "write-tree"], capture_output=True, text=True,
                          check=True, env=idx).stdout.strip()
    commit = subprocess.run(
        ["git", "commit-tree", tree, "-m", "base"], capture_output=True, text=True,
        check=True, env=dict(env, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t",
                             GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t")).stdout.strip()
    subprocess.run(["git", "update-ref", "refs/heads/main", commit],
                   check=True, capture_output=True, env=env)
    return commit


def build_seat_ground(tmp_path):
    """The two paths run-build.sh checks before it does anything else: the task
    kernel it copies, and a gatehouse holding the ENTITLED repos it clones.

    Without these the build wrapper exits at line 130-odd with 'build kernel
    missing: /usr/local/lib/disjorn/…', i.e. against the PRODUCTION default —
    so every run-build.sh test silently measured whether the host happened to
    have a deploy, instead of measuring the wrapper. Pointing them at tmp_path
    makes the suite hermetic and the assertions real.

    BOTH entitled repos since 2026-08-13: the wrapper no longer globs `*.git`,
    it clones `disjorn.git` + `<name>.git` and REFUSES TO LAUNCH if either is
    missing. WRAPPERS names the resident `smoketest`, so the gatehouse needs a
    `smoketest.git`. (The entitled set itself is measured in
    tests/test_build_harvest.py; here it is only the ground every other
    run-build.sh assertion stands on.)
    """
    kernel = tmp_path / "build-kernel.md"
    kernel.write_text("# Build session\n\nYou are a build tool.\n")
    gatehouse = tmp_path / "gatehouse"
    gatehouse.mkdir()
    for repo in ("disjorn", WRAPPERS["run-build.sh"][0]):
        seed_bare_repo(gatehouse / f"{repo}.git", tmp_path)
    return {"RESIDENT_BUILD_KERNEL": str(kernel), "RESIDENT_GATEHOUSE": str(gatehouse)}


@pytest.fixture()
def rig(tmp_path):
    """Scratch mounts + a fake podman on PATH. Returns a run() helper."""
    home_vol = tmp_path / "home"
    config = tmp_path / "config"
    dump = tmp_path / "dump"
    bindir = tmp_path / "bin"
    for d in (home_vol, config, dump, bindir):
        d.mkdir()

    podman = bindir / "podman"
    podman.write_text(FAKE_PODMAN)
    podman.chmod(0o755)
    build_ground = build_seat_ground(tmp_path)

    def run(script: str, env_file_text: str | None, extra_env: dict | None = None):
        env_file = config / "env"
        if env_file_text is None:
            if env_file.exists():
                env_file.unlink()
        else:
            env_file.write_text(env_file_text)
            env_file.chmod(0o600)
        env = dict(os.environ)
        env.update(
            PATH=f"{bindir}:{env['PATH']}",
            DUMP_DIR=str(dump),
            RESIDENT_IMAGE="localhost/disjorn-resident:test",
            RESIDENT_HOME_VOL=str(home_vol),
            RESIDENT_CONFIG_DIR=str(config),
            RESIDENT_BROKER_SOCKET=str(tmp_path / "broker.sock"),
            RESIDENT_HOUSE_MEMORY=str(tmp_path / "no-house-memory"),
            RESIDENT_NETWORK="none",
        )
        # Build-seat ground only for the build wrapper: run-resident.sh mounts
        # the gatehouse when RESIDENT_GATEHOUSE is set, and its mount-set
        # assertions are about the summon path as it actually runs.
        if script == "run-build.sh":
            env.update(build_ground)
        # Poison our own environment: the wrappers must ignore it entirely
        # and take the credential from the env file only.
        env["ANTHROPIC_API_KEY"] = "sk-ant-INHERITED-MUST-NOT-BE-USED"
        env["CLAUDE_CODE_OAUTH_TOKEN"] = "sk-ant-oat01-INHERITED-MUST-NOT-BE-USED"
        env.update(extra_env or {})
        proc = subprocess.run(
            ["bash", str(CC_DIR / script)] + WRAPPERS[script],
            capture_output=True, text=True, env=env,
        )
        argv = (dump / "argv").read_bytes().decode().split("\0") if (dump / "argv").exists() else []
        environ = {}
        if (dump / "environ").exists():
            for entry in (dump / "environ").read_bytes().decode().split("\0"):
                if "=" in entry:
                    k, v = entry.split("=", 1)
                    environ[k] = v
        envfile = (dump / "envfile").read_text() if (dump / "envfile").exists() else ""
        return proc, argv, environ, envfile

    return run


def container_env(argv, environ, envfile):
    """Reconstruct what the container would actually see."""
    out = {}
    for line in envfile.splitlines():
        line_s = line.strip()
        if not line_s or line_s.startswith("#"):
            continue
        if "=" in line:
            k, v = line.split("=", 1)
            out[k.strip()] = v
    for i, a in enumerate(argv):
        if a in ("--env", "-e") and i + 1 < len(argv):
            spec = argv[i + 1]
            if "=" in spec:
                k, v = spec.split("=", 1)
                out[k] = v
            elif spec in environ:           # name-only: podman reads its own env
                out[spec] = environ[spec]
    return out


ALL = list(WRAPPERS)
# Spine-bearing wrappers: BOTH, again, as of 2026-08-12
# (SPECS/2026-08-08-gable-build-lane-provisioning.md, confirmed #custodian seq
# 1008). Between 08-06 and 08-12 this was run-resident.sh only — branch B
# removed the build seat's spine on a trial against Claudette's spine, whose
# entries all declare `seats: [resident]`, generalised into a claim about the
# seat. Gable's spine has declared a build seat since the 07-22 seat-split.
# The mount is back for both; the CUTOVER (RESIDENT_SPINE_DIR + a bootstrap
# call in session_argv) is still separate and still plink's.
SPINE_WRAPPERS = list(WRAPPERS)

# Which seat may spend the metered key. The credential-routing spec's table:
# conversational seats on per-seat API keys, build/agent loops on Max. A build
# that finds only an API key refuses to launch rather than billing it.
METERED_FALLBACK = {"run-resident.sh": "allow", "run-build.sh": "refuse"}
APIKEY_SEATS = [s for s, v in METERED_FALLBACK.items() if v == "allow"]
MAX_ONLY_SEATS = [s for s, v in METERED_FALLBACK.items() if v == "refuse"]


# ── precedence ───────────────────────────────────────────────────────────

@pytest.mark.parametrize("script", ALL)
def test_oauth_only(rig, script):
    proc, argv, environ, envfile = rig(
        script, f"CLAUDE_CODE_OAUTH_TOKEN={FAKE_OAUTH}\nBROKER_DISABLE=\n")
    assert proc.returncode == 0, proc.stderr
    cenv = container_env(argv, environ, envfile)
    assert cenv["CLAUDE_CODE_OAUTH_TOKEN"] == FAKE_OAUTH
    assert "ANTHROPIC_API_KEY" not in cenv
    assert "auth: CLAUDE_CODE_OAUTH_TOKEN" in proc.stderr


@pytest.mark.parametrize("script", APIKEY_SEATS)
def test_apikey_only_is_the_chat_seat_s_own_route(rig, script):
    """For a conversational seat the API key is not a failover — it IS the
    routing table's answer (metered, per-seat, hard cap, fast rotation)."""
    proc, argv, environ, envfile = rig(script, f"ANTHROPIC_API_KEY={FAKE_APIKEY}\n")
    assert proc.returncode == 0, proc.stderr
    cenv = container_env(argv, environ, envfile)
    assert cenv["ANTHROPIC_API_KEY"] == FAKE_APIKEY
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in cenv
    assert "auth: ANTHROPIC_API_KEY" in proc.stderr


@pytest.mark.parametrize("script", MAX_ONLY_SEATS)
def test_apikey_only_refuses_to_launch_a_max_only_seat(rig, script):
    """THE POINT OF THE SEAT SPLIT, enforced where a build can spend.

    Build and agent loops run on plink's Max account. A loop that quietly fails
    over to the metered key has converted "your account said not now" into
    "spend your API money instead", which is the decision plink reserved for
    himself (#custodian 683/697). The halt protocol's words: no silent
    key-fallback. So the wrapper refuses, loudly, and podman never runs.
    """
    proc, argv, environ, envfile = rig(script, f"ANTHROPIC_API_KEY={FAKE_APIKEY}\n")
    assert proc.returncode != 0, "a Max-only seat must not launch on a metered key"
    assert not argv, "podman must not have been executed"
    assert "REFUSING TO LAUNCH" in proc.stderr
    # It must name the fix and the ruling, not just say no.
    assert "claude setup-token" in proc.stderr
    assert "no silent key-fallback" in proc.stderr.lower()
    assert FAKE_APIKEY not in proc.stderr and FAKE_APIKEY not in proc.stdout


@pytest.mark.parametrize("script", MAX_ONLY_SEATS)
def test_a_max_only_seat_still_launches_on_the_oauth_token(rig, script):
    """The refusal is about WHICH credential, never about having one."""
    proc, argv, environ, envfile = rig(script, f"CLAUDE_CODE_OAUTH_TOKEN={FAKE_OAUTH}\n")
    assert proc.returncode == 0, proc.stderr
    cenv = container_env(argv, environ, envfile)
    assert cenv["CLAUDE_CODE_OAUTH_TOKEN"] == FAKE_OAUTH


@pytest.mark.parametrize("script", MAX_ONLY_SEATS)
def test_a_max_only_seat_with_both_credentials_launches_on_oauth(rig, script):
    """Both present is not the refusal case: OAuth wins and the API key is
    filtered out, so nothing metered can be spent. Refusing here would break a
    seat whose env file is merely untidy."""
    proc, argv, environ, envfile = rig(
        script, f"ANTHROPIC_API_KEY={FAKE_APIKEY}\nCLAUDE_CODE_OAUTH_TOKEN={FAKE_OAUTH}\n")
    assert proc.returncode == 0, proc.stderr
    cenv = container_env(argv, environ, envfile)
    assert cenv["CLAUDE_CODE_OAUTH_TOKEN"] == FAKE_OAUTH
    assert "ANTHROPIC_API_KEY" not in cenv


def test_the_two_wrappers_declare_different_metered_fallbacks():
    """The seat's credential route is ONE line, outside the byte-identical
    credential block, set by which wrapper launched — never by /config."""
    for script, expected in METERED_FALLBACK.items():
        text = (CC_DIR / script).read_text()
        assert f"_seat_metered_fallback={expected}" in text, script
        other = "refuse" if expected == "allow" else "allow"
        assert f"_seat_metered_fallback={other}" not in text, script


@pytest.mark.parametrize("script", ALL)
def test_both_present_oauth_wins_and_api_key_is_dropped(rig, script):
    """The invariant: exactly one credential reaches the container."""
    proc, argv, environ, envfile = rig(
        script,
        f"ANTHROPIC_API_KEY={FAKE_APIKEY}\nCLAUDE_CODE_OAUTH_TOKEN={FAKE_OAUTH}\n")
    assert proc.returncode == 0, proc.stderr
    cenv = container_env(argv, environ, envfile)
    assert cenv["CLAUDE_CODE_OAUTH_TOKEN"] == FAKE_OAUTH
    assert "ANTHROPIC_API_KEY" not in cenv, "API key must not ride along"
    assert FAKE_APIKEY not in envfile
    # loud, and it names the loser
    assert "WARNING" in proc.stderr and "BOTH" in proc.stderr
    assert "NOT passing ANTHROPIC_API_KEY" in proc.stderr


@pytest.mark.parametrize("script", ALL)
def test_neither_present_warns_loudly_and_passes_nothing(rig, script):
    proc, argv, environ, envfile = rig(script, "BROKER_DISABLE=1\n")
    assert proc.returncode == 0, proc.stderr
    cenv = container_env(argv, environ, envfile)
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in cenv
    assert "ANTHROPIC_API_KEY" not in cenv
    assert cenv["BROKER_DISABLE"] == "1", "non-credential vars must survive"
    assert "WARNING no credential" in proc.stderr


@pytest.mark.parametrize("script", ALL)
def test_env_file_absent_warns_and_names_both_vars(rig, script):
    proc, argv, environ, envfile = rig(script, None)
    assert proc.returncode == 0, proc.stderr
    assert "WARNING env file absent" in proc.stderr
    assert "CLAUDE_CODE_OAUTH_TOKEN" in proc.stderr
    assert "ANTHROPIC_API_KEY" in proc.stderr
    assert "--env-file" not in argv


@pytest.mark.parametrize("script", ALL)
def test_ambient_environment_is_never_the_source(rig, script):
    """A key in the unit's Environment= must not become a session identity."""
    proc, argv, environ, envfile = rig(script, "BROKER_DISABLE=1\n")
    cenv = container_env(argv, environ, envfile)
    assert "MUST-NOT-BE-USED" not in str(cenv)
    assert "WARNING no credential" in proc.stderr


@pytest.mark.parametrize("script", ALL)
def test_last_assignment_wins_matching_podman_env_file_semantics(rig, script):
    proc, argv, environ, envfile = rig(
        script,
        f"CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat01-FIRST\n"
        f"CLAUDE_CODE_OAUTH_TOKEN={FAKE_OAUTH}\n")
    cenv = container_env(argv, environ, envfile)
    assert cenv["CLAUDE_CODE_OAUTH_TOKEN"] == FAKE_OAUTH


@pytest.mark.parametrize("script", ALL)
def test_value_is_taken_literally_like_podman(rig, script):
    """podman 5.4.2 does not strip quotes from env-file values; nor do we."""
    proc, argv, environ, envfile = rig(script, 'CLAUDE_CODE_OAUTH_TOKEN="quoted"\n')
    cenv = container_env(argv, environ, envfile)
    assert cenv["CLAUDE_CODE_OAUTH_TOKEN"] == '"quoted"'


@pytest.mark.parametrize("script", ALL)
def test_leading_whitespace_and_comments_in_env_file(rig, script):
    proc, argv, environ, envfile = rig(
        script,
        f"# a comment\n\n  CLAUDE_CODE_OAUTH_TOKEN={FAKE_OAUTH}\nBROKER_DISABLE=1\n")
    cenv = container_env(argv, environ, envfile)
    assert cenv["CLAUDE_CODE_OAUTH_TOKEN"] == FAKE_OAUTH
    assert cenv["BROKER_DISABLE"] == "1"


# ── leak checks ──────────────────────────────────────────────────────────

def _assert_name_only_env(argv, environ, name, text):
    assert text not in "\0".join(argv)
    # name-only form: `--env NAME`, never `--env NAME=value`
    assert argv[argv.index("--env") + 1] == name
    assert environ[name] == text


@pytest.mark.parametrize("script", ALL)
def test_oauth_never_appears_in_podman_argv(rig, script):
    """`ps` / /proc/*/cmdline must not show it. Name-only --env is why."""
    proc, argv, environ, envfile = rig(
        script, f"CLAUDE_CODE_OAUTH_TOKEN={FAKE_OAUTH}\n")
    _assert_name_only_env(argv, environ, "CLAUDE_CODE_OAUTH_TOKEN", FAKE_OAUTH)


@pytest.mark.parametrize("script", APIKEY_SEATS)
def test_apikey_never_appears_in_podman_argv(rig, script):
    """Same guarantee for the metered key — on the seats that may carry one.
    A Max-only seat never gets this far; it refuses (see the routing tests)."""
    proc, argv, environ, envfile = rig(script, f"ANTHROPIC_API_KEY={FAKE_APIKEY}\n")
    _assert_name_only_env(argv, environ, "ANTHROPIC_API_KEY", FAKE_APIKEY)


@pytest.mark.parametrize("script", ALL)
def test_credential_never_appears_on_stdout_or_stderr(rig, script):
    proc, argv, environ, envfile = rig(
        script,
        f"CLAUDE_CODE_OAUTH_TOKEN={FAKE_OAUTH}\nANTHROPIC_API_KEY={FAKE_APIKEY}\n")
    assert FAKE_OAUTH not in proc.stderr and FAKE_OAUTH not in proc.stdout
    assert FAKE_APIKEY not in proc.stderr and FAKE_APIKEY not in proc.stdout


@pytest.mark.parametrize("script", ALL)
def test_filtered_env_file_holds_no_credential_and_is_unlinked(rig, script, tmp_path):
    """The temp copy podman reads must be credential-free, and must be gone."""
    proc, argv, environ, envfile = rig(
        script,
        f"CLAUDE_CODE_OAUTH_TOKEN={FAKE_OAUTH}\nANTHROPIC_API_KEY={FAKE_APIKEY}\n"
        "BROKER_DISABLE=1\n")
    assert FAKE_OAUTH not in envfile and FAKE_APIKEY not in envfile
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in envfile
    assert "ANTHROPIC_API_KEY" not in envfile
    assert "BROKER_DISABLE=1" in envfile
    # podman is pointed at an inherited fd, not a path that survives.
    assert "--env-file" in argv
    path = argv[argv.index("--env-file") + 1]
    assert path.startswith("/dev/fd/") or path.startswith("/proc/self/fd/")
    leftovers = list(Path(os.environ.get("TMPDIR", "/tmp")).glob("run-*-env.*"))
    assert not leftovers, f"temp env copies left on disk: {leftovers}"


@pytest.mark.parametrize("script", ALL)
def test_bare_inherit_form_is_not_honoured_for_credentials(rig, script):
    """`CLAUDE_CODE_OAUTH_TOKEN` with no `=` must not smuggle the ambient one."""
    proc, argv, environ, envfile = rig(script, "CLAUDE_CODE_OAUTH_TOKEN\n")
    cenv = container_env(argv, environ, envfile)
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in cenv
    assert "MUST-NOT-BE-USED" not in str(cenv)
    assert "WARNING no credential" in proc.stderr


@pytest.mark.parametrize("script", ALL)
def test_config_env_is_masked_inside_the_container(rig, script):
    """`cat /config/env` must not hand the session its own credential.

    settings.json denies Read(//config/env) but allows Bash(cat:*) and
    Bash(python3:*), so that deny is hygiene. The mount is the mechanism.
    """
    proc, argv, environ, envfile = rig(script, f"CLAUDE_CODE_OAUTH_TOKEN={FAKE_OAUTH}\n")
    assert "/dev/null:/config/env:ro" in argv


@pytest.mark.parametrize("script", ALL)
def test_mask_escape_hatch_warns(rig, script):
    proc, argv, environ, envfile = rig(
        script, f"CLAUDE_CODE_OAUTH_TOKEN={FAKE_OAUTH}\n", {"RESIDENT_MASK_ENV": "0"})
    assert "/dev/null:/config/env:ro" not in argv
    assert "WARNING RESIDENT_MASK_ENV=0" in proc.stderr


@pytest.mark.parametrize("script", ALL)
def test_no_mask_when_there_is_no_env_file(rig, script):
    """Nothing to mask, and bind-mounting over a missing path would error."""
    proc, argv, environ, envfile = rig(script, None)
    assert "/dev/null:/config/env:ro" not in argv


# ── the spine mount (protection by placement) ────────────────────────────
#
# The spine is the directory house_memory/bootstrap.py assembles into
# ~/.claude/CLAUDE.md at the start of EVERY session — the resident's kernel.
# AGENTHOOD.md puts a resident's own prompt permanently in Tier 2 (human
# reviews every change). That is only true if the spine the container loads
# is not writable by the resident, so the wrappers mount a plink-owned
# mirror read-only at /opt/spine.
#
# These tests pin three things: it is mounted READ-ONLY when asked for; it
# is COMPLETELY ABSENT when not asked for (no regression to a live summon);
# and the wrapper REFUSES TO LAUNCH if the source it was pointed at is
# writable by the uid it runs as — the check that catches a cutover
# mis-pointed back at the resident's home volume.


@pytest.fixture()
def spine_dir(tmp_path):
    """A spine source that is NOT writable by the uid running the test.

    0555 dir / 0444 files: access(2) honours the mode for the owner too (we
    are not root), so `find -writable` sees nothing — exactly the property
    the production mirror has via ownership. Restored at teardown so pytest
    can clean tmp_path up.
    """
    d = tmp_path / "spine"
    d.mkdir()
    (d / "00-kernel.md").write_text("---\nkernel: true\n---\nbe good\n")
    (d / "10-people.md").write_text("plink\n")
    for f in d.iterdir():
        f.chmod(0o444)
    d.chmod(0o555)
    yield d
    d.chmod(0o755)
    for f in d.iterdir():
        f.chmod(0o644)


def mounts(argv):
    """[(source, target, opts...)] for every -v in the podman argv."""
    out = []
    for i, a in enumerate(argv):
        if a == "-v" and i + 1 < len(argv):
            out.append(argv[i + 1].split(":"))
    return out


@pytest.mark.parametrize("script", SPINE_WRAPPERS)
def test_spine_is_mounted_read_only_at_opt_spine(rig, script, spine_dir):
    proc, argv, environ, envfile = rig(
        script, "BROKER_DISABLE=1\n", {"RESIDENT_SPINE_HOST": str(spine_dir)})
    assert proc.returncode == 0, proc.stderr
    spine_mounts = [m for m in mounts(argv) if m[1] == "/opt/spine"]
    assert spine_mounts, f"no /opt/spine mount in argv: {argv}"
    assert len(spine_mounts) == 1
    src, target, *opts = spine_mounts[0]
    assert src == str(spine_dir)
    assert "ro" in opts, f"/opt/spine must be read-only, got opts {opts}"


# The mount set each wrapper produces with no spine asked for. Per-wrapper
# because the two seats deliberately differ: the resident seat has the broker
# socket, and the build seat has NO SOCKET and — since 2026-08-13 — NO
# GATEHOUSE either. The build seat's set is now strictly the smaller one, and
# nothing in it is a writable path out of the container.
UNSET_SPINE_MOUNTS = {
    "run-resident.sh": {"/home/resident", "/run/disjorn-broker", "/config", "/config/env"},
    "run-build.sh": {"/home/resident", "/config", "/config/env"},
}


@pytest.mark.parametrize("script", SPINE_WRAPPERS)
def test_unset_spine_var_adds_nothing_at_all(rig, script):
    """UNSET must be byte-for-byte today's invocation: no mount, no flag.

    This is the ship-it-closed guarantee, and it is what makes the launcher's
    presence check safe: a resident with no published mirror gets no
    RESIDENT_SPINE_HOST, and therefore exactly the invocation running today.
    Anything appearing here is a live regression.
    """
    proc, argv, environ, envfile = rig(script, "BROKER_DISABLE=1\n")
    assert proc.returncode == 0, proc.stderr
    assert not any("/opt/spine" in a for a in argv), f"spine in argv: {argv}"
    # The whole mount set, not just the absence of /opt/spine: nothing new
    # may appear on the unset path at all.
    assert {m[1] for m in mounts(argv)} == UNSET_SPINE_MOUNTS[script]


@pytest.mark.parametrize("script", SPINE_WRAPPERS)
def test_empty_spine_var_is_treated_as_unset(rig, script):
    proc, argv, environ, envfile = rig(
        script, "BROKER_DISABLE=1\n", {"RESIDENT_SPINE_HOST": ""})
    assert proc.returncode == 0, proc.stderr
    assert "/opt/spine" not in "\0".join(argv)


@pytest.mark.parametrize("script", SPINE_WRAPPERS)
def test_writable_spine_source_refuses_to_launch(rig, script, tmp_path):
    """Fail CLOSED. A spine the caller can write is not a wall.

    This is the misconfiguration that matters: RESIDENT_SPINE_HOST left
    pointing at /home/res-<name>/resident-home/bots/<name>/spine, which the
    resident owns. A ro bind mount would still let the resident edit the
    HOST path outside the container and be loaded next session.
    """
    d = tmp_path / "writable-spine"
    d.mkdir()
    (d / "00-kernel.md").write_text("kernel\n")
    proc, argv, environ, envfile = rig(
        script, "BROKER_DISABLE=1\n", {"RESIDENT_SPINE_HOST": str(d)})
    assert proc.returncode != 0, "must not launch with a writable spine source"
    assert "REFUSING TO LAUNCH" in proc.stderr
    assert "WRITABLE" in proc.stderr
    assert str(d) in proc.stderr           # names the offending path
    assert "do NOT loosen" in proc.stderr  # and the wrong way to fix it
    assert not argv, "podman must not have been executed"


@pytest.mark.parametrize("script", SPINE_WRAPPERS)
def test_writable_entry_inside_unwritable_dir_is_also_refused(
        rig, script, spine_dir):
    """A 0666 entry in a 0555 dir is still a rewritable kernel line."""
    spine_dir.chmod(0o755)
    (spine_dir / "00-kernel.md").chmod(0o666)
    spine_dir.chmod(0o555)
    proc, argv, environ, envfile = rig(
        script, "BROKER_DISABLE=1\n", {"RESIDENT_SPINE_HOST": str(spine_dir)})
    assert proc.returncode != 0
    assert "REFUSING TO LAUNCH" in proc.stderr
    assert "00-kernel.md" in proc.stderr


@pytest.mark.parametrize("script", SPINE_WRAPPERS)
def test_missing_spine_dir_fails_loud(rig, script, tmp_path):
    """Never silently continue without the kernel's source."""
    proc, argv, environ, envfile = rig(
        script, "BROKER_DISABLE=1\n",
        {"RESIDENT_SPINE_HOST": str(tmp_path / "nope")})
    assert proc.returncode != 0
    assert "RESIDENT_SPINE_HOST not a dir" in proc.stderr
    assert not argv


@pytest.mark.parametrize("script", ALL)
def test_spine_mount_does_not_set_resident_spine_dir(rig, script, spine_dir):
    """Mounting is NOT the cutover.

    Which spine bootstrap.py loads is RESIDENT_SPINE_DIR, and that lives in
    the /config env file — plink's single-line, deliberate decision. The
    wrapper must not quietly redirect a live resident's kernel just because
    a mount appeared.
    """
    proc, argv, environ, envfile = rig(
        script, "BROKER_DISABLE=1\n", {"RESIDENT_SPINE_HOST": str(spine_dir)})
    cenv = container_env(argv, environ, envfile)
    assert "RESIDENT_SPINE_DIR" not in cenv


@pytest.mark.parametrize("script", ALL)
def test_env_file_still_supplies_resident_spine_dir(rig, script, spine_dir):
    """...and the env file's value passes through untouched (the cutover)."""
    proc, argv, environ, envfile = rig(
        script, "RESIDENT_SPINE_DIR=/opt/spine\n",
        {"RESIDENT_SPINE_HOST": str(spine_dir)})
    cenv = container_env(argv, environ, envfile)
    assert cenv["RESIDENT_SPINE_DIR"] == "/opt/spine"


# ── the container reaper ─────────────────────────────────────────────────
#
# Rootless `podman run` hands the container to conmon, which is reparented
# away — killing the podman CLIENT leaves the container Up (measured on
# podman 5.4.2). Both supervisors that kill this wrapper use Python's
# proc.kill(), i.e. SIGKILL: residency/launcher.py's pre-act model gate when
# it refuses a session, and brokerd.py's build reaper at timeout_sec. So a
# refused session's SIDE EFFECTS — file writes, broker calls — kept running
# inside a container nobody was going to read from.
#
# A signal trap cannot fix that: no trap runs on SIGKILL. The wrappers start
# a watchdog sibling before `exec podman`; it waits for the wrapper's PID
# ($$ survives exec) to disappear and then `podman rm -f -t 0 --ignore`s the
# container. These tests kill the wrapper for real and prove the reap
# COMMAND is issued — test_container.sh check 14 proves a real container
# actually dies.

FAKE_CID = "fakecid0000000000000000000000000000000000000000000000000000000f"

FAKE_PODMAN_REAP = r"""#!/usr/bin/env bash
# Fake podman for the reaper tests. `run` writes the cidfile and imitates a
# container that keeps running (or returns at once when detached); `rm`
# records what the watchdog asked to be removed.
set -u
if [ "${1:-}" = "run" ]; then
  for a in "$@"; do
    case "$a" in -d|--detach|--detach=true) : > "$DUMP_DIR/detached"; exit 0 ;; esac
  done
  prev=""; cid=""
  for a in "$@"; do
    [ "$prev" = "--cidfile" ] && cid="$a"
    prev="$a"
  done
  if [ -n "$cid" ]; then
    printf '%s' "__FAKE_CID__" > "$cid"
    printf '%s\n' "$cid" > "$DUMP_DIR/cidfile-path"
  fi
  : > "$DUMP_DIR/run-started"
  exec sleep 30          # exec: the wrapper's PID is now this, as in real life
elif [ "${1:-}" = "rm" ]; then
  printf '%s\n' "$*" >> "$DUMP_DIR/reaped"
fi
exit 0
""".replace("__FAKE_CID__", FAKE_CID)


@pytest.fixture()
def reap_rig(tmp_path):
    """Launch a wrapper for real (Popen) with a fake podman that lingers."""
    home_vol, config, dump, bindir = (tmp_path / n for n in
                                      ("home", "config", "dump", "bin"))
    for d in (home_vol, config, dump, bindir):
        d.mkdir()
    (config / "env").write_text("BROKER_DISABLE=1\n")
    podman = bindir / "podman"
    podman.write_text(FAKE_PODMAN_REAP)
    podman.chmod(0o755)
    build_ground = build_seat_ground(tmp_path)

    procs = []

    def launch(script: str, extra_env: dict | None = None):
        env = dict(os.environ)
        env.update(
            PATH=f"{bindir}:{env['PATH']}",
            DUMP_DIR=str(dump),
            RESIDENT_IMAGE="localhost/disjorn-resident:test",
            RESIDENT_HOME_VOL=str(home_vol),
            RESIDENT_CONFIG_DIR=str(config),
            RESIDENT_BROKER_SOCKET=str(tmp_path / "broker.sock"),
            RESIDENT_HOUSE_MEMORY=str(tmp_path / "no-house-memory"),
            RESIDENT_NETWORK="none",
        )
        if script == "run-build.sh":
            env.update(build_ground)
        env.update(extra_env or {})
        p = subprocess.Popen(
            ["bash", str(CC_DIR / script)] + WRAPPERS[script],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
        procs.append(p)
        return p

    def wait_for(path: Path, timeout=10.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if path.exists():
                return True
            time.sleep(0.05)
        return False

    yield launch, dump, wait_for

    for p in procs:
        try:
            p.kill()
        except OSError:
            pass


def expected_container_name(script):
    return {"run-resident.sh": "resident-cc-smoketest",
            "run-build.sh": "disjorn-build-some-slug"}[script]


@pytest.mark.parametrize("script", ALL)
def test_sigkilled_wrapper_reaps_its_container(reap_rig, script):
    """The gap, closed: SIGKILL the wrapper, the container is taken down.

    SIGKILL specifically — that is what proc.kill() sends, and it is the
    case a signal trap provably cannot cover.
    """
    launch, dump, wait_for = reap_rig
    p = launch(script)
    assert wait_for(dump / "run-started"), "fake podman never started"
    p.kill()                      # SIGKILL, exactly like the supervisors
    p.wait(timeout=10)
    assert wait_for(dump / "reaped"), "container was never reaped"
    reaped = (dump / "reaped").read_text()
    assert FAKE_CID in reaped, f"reaped something other than its own cid: {reaped}"
    assert "--ignore" in reaped, "must not error on an already-gone container"
    assert "-t 0" in reaped, "no grace period for a refused session"
    assert "-f" in reaped


@pytest.mark.parametrize("script", ALL)
def test_reaper_targets_the_container_id_not_the_name(reap_rig, script):
    """The bug this pins: names are REUSED every summon
    ("resident-cc-gable"). A name-based watchdog kills the NEXT summon's
    container in the window after its own wrapper exits. Reaping by cid
    makes that impossible. (Found by test_container.sh check 14, not by
    review — keep the assertion.)"""
    launch, dump, wait_for = reap_rig
    p = launch(script)
    assert wait_for(dump / "run-started")
    p.kill()
    p.wait(timeout=10)
    assert wait_for(dump / "reaped")
    reaped = (dump / "reaped").read_text()
    assert expected_container_name(script) not in reaped, (
        "watchdog reaps by NAME — it will kill a later session's container")
    assert FAKE_CID in reaped


@pytest.mark.parametrize("script", ALL)
def test_reaper_cleans_up_its_cidfile(reap_rig, script):
    launch, dump, wait_for = reap_rig
    p = launch(script)
    assert wait_for(dump / "run-started")
    cid_path = Path((dump / "cidfile-path").read_text().strip())
    p.kill()
    p.wait(timeout=10)
    assert wait_for(dump / "reaped")
    deadline = time.time() + 5
    while cid_path.exists() and time.time() < deadline:
        time.sleep(0.05)
    assert not cid_path.exists(), f"cidfile left on disk: {cid_path}"


@pytest.mark.parametrize("script", ALL)
def test_sigterm_also_reaps(reap_rig, script):
    launch, dump, wait_for = reap_rig
    p = launch(script)
    assert wait_for(dump / "run-started")
    p.terminate()
    p.wait(timeout=10)
    assert wait_for(dump / "reaped")
    assert FAKE_CID in (dump / "reaped").read_text()


@pytest.mark.parametrize("script", ALL)
@pytest.mark.parametrize("code", [0, 7])
def test_reaper_does_not_mask_the_exit_status(tmp_path, script, code):
    """The wrapper still exits with podman's status, reaper or no reaper.

    The broker and the summon adapter both read this exit code. `exec
    podman` gives it to us for free — which is one more reason the reaper is
    a watchdog sibling rather than a trap around a backgrounded child.
    """
    home_vol, config, bindir = (tmp_path / n for n in ("home", "config", "bin"))
    for d in (home_vol, config, bindir):
        d.mkdir()
    (config / "env").write_text("BROKER_DISABLE=1\n")
    podman = bindir / "podman"
    podman.write_text(f'#!/usr/bin/env bash\n[ "$1" = rm ] && exit 0\nexit {code}\n')
    podman.chmod(0o755)
    env = dict(os.environ)
    env.update(
        PATH=f"{bindir}:{env['PATH']}",
        RESIDENT_IMAGE="localhost/disjorn-resident:test",
        RESIDENT_HOME_VOL=str(home_vol), RESIDENT_CONFIG_DIR=str(config),
        RESIDENT_BROKER_SOCKET=str(tmp_path / "broker.sock"),
        RESIDENT_HOUSE_MEMORY=str(tmp_path / "nohm"), RESIDENT_NETWORK="none")
    if script == "run-build.sh":
        env.update(build_seat_ground(tmp_path))
    proc = subprocess.run(["bash", str(CC_DIR / script)] + WRAPPERS[script],
                          capture_output=True, text=True, env=env)
    assert proc.returncode == code


@pytest.mark.parametrize("script", ALL)
def test_detached_container_is_not_reaped(reap_rig, script):
    """`RESIDENT_PODMAN_EXTRA=-d` means the caller owns the container's
    lifetime; the wrapper exiting IS the success path. Arming the watchdog
    there would kill the container it just started (and would break
    test_container.sh check 8d, which needs the detached container alive)."""
    launch, dump, wait_for = reap_rig
    p = launch(script, {"RESIDENT_PODMAN_EXTRA": "-d"})
    p.wait(timeout=10)
    assert (dump / "detached").exists(), "fake podman did not see -d"
    time.sleep(1.5)
    assert not (dump / "reaped").exists(), "detached container was reaped"


@pytest.mark.parametrize("script", ALL)
def test_reap_escape_hatch_warns_loudly(reap_rig, script):
    launch, dump, wait_for = reap_rig
    p = launch(script, {"RESIDENT_REAP": "0"})
    assert wait_for(dump / "run-started")
    p.kill()
    p.wait(timeout=10)
    time.sleep(1.5)
    assert not (dump / "reaped").exists()
    err = p.stderr.read().decode()
    assert "WARNING RESIDENT_REAP=0" in err
    assert "OUTLIVE" in err


@pytest.mark.parametrize("script", ALL)
def test_container_name_is_a_single_source_of_truth(rig, script):
    """One variable names the container, for --name and for the warning the
    reaper prints — no second spelling to drift."""
    proc, argv, environ, envfile = rig(script, "BROKER_DISABLE=1\n")
    assert argv[argv.index("--name") + 1] == expected_container_name(script)
    text = (CC_DIR / script).read_text()
    assert 'CONTAINER_NAME=' in text
    assert '--name "$CONTAINER_NAME"' in text


@pytest.mark.parametrize("script", ALL)
def test_cidfile_is_requested_before_the_image_argument(rig, script):
    """--cidfile is a podman-run flag; after the image it would be passed to
    the container's command instead."""
    proc, argv, environ, envfile = rig(script, "BROKER_DISABLE=1\n")
    assert "--cidfile" in argv
    assert argv.index("--cidfile") < argv.index("localhost/disjorn-resident:test")


@pytest.mark.parametrize("script", ALL)
def test_detached_run_gets_no_cidfile(rig, script):
    """No watchdog, so no cidfile to leave lying around."""
    proc, argv, environ, envfile = rig(
        script, "BROKER_DISABLE=1\n", {"RESIDENT_PODMAN_EXTRA": "-d"})
    assert "--cidfile" not in argv


# ── the seat (spec 2026-07-22 seat-split) ────────────────────────────────
#
# One spine of record serves two seats. The wrapper decides which: a summon
# (run-resident.sh) is the resident seat and loads the whole spine; a detached
# build (run-build.sh) is the build seat and loads the operational set only,
# never biography. bootstrap.py reads RESIDENT_SEAT inside the container, so
# the wrapper's only job is to put the right value there. It is passed by
# NAME=VALUE (not a secret), and it must win over any /config env-file value —
# the seat is a property of which wrapper launched, never of per-resident
# config.

EXPECTED_SEAT = {"run-resident.sh": "resident", "run-build.sh": "build"}


@pytest.mark.parametrize("script", ALL)
def test_wrapper_sets_its_seat_in_the_container_env(rig, script):
    proc, argv, environ, envfile = rig(script, "BROKER_DISABLE=1\n")
    assert proc.returncode == 0, proc.stderr
    cenv = container_env(argv, environ, envfile)
    assert cenv["RESIDENT_SEAT"] == EXPECTED_SEAT[script]


@pytest.mark.parametrize("script", ALL)
def test_seat_is_passed_by_name_value_not_via_env_file(rig, script):
    """It rides `-e RESIDENT_SEAT=<seat>` in argv (harmless — not a secret),
    so the value is fixed by the wrapper, not read from the filtered env-file."""
    proc, argv, environ, envfile = rig(script, "BROKER_DISABLE=1\n")
    joined = list(zip(argv, argv[1:]))
    assert ("-e", f"RESIDENT_SEAT={EXPECTED_SEAT[script]}") in joined
    assert "RESIDENT_SEAT" not in envfile


@pytest.mark.parametrize("script", ALL)
def test_wrapper_seat_wins_over_an_env_file_value(rig, script):
    """A /config env file must not be able to reclassify the seat: podman
    honours the last --env/--env-file for a given name, and the wrapper's
    `-e RESIDENT_SEAT` is emitted AFTER the credential block's --env-file."""
    # Point the env file at the WRONG seat; the wrapper's own value must win.
    wrong = "build" if EXPECTED_SEAT[script] == "resident" else "resident"
    proc, argv, environ, envfile = rig(
        script, f"BROKER_DISABLE=1\nRESIDENT_SEAT={wrong}\n")
    assert proc.returncode == 0, proc.stderr
    env_file_idx = argv.index("--env-file")
    seat_idx = argv.index(f"RESIDENT_SEAT={EXPECTED_SEAT[script]}")
    assert seat_idx > env_file_idx, "wrapper seat must be emitted after --env-file"
    cenv = container_env(argv, environ, envfile)
    assert cenv["RESIDENT_SEAT"] == EXPECTED_SEAT[script]


def test_the_two_wrappers_declare_different_seats():
    """The seat line is the ONE line that must differ between the wrappers —
    it is deliberately outside every byte-identical block."""
    res = (CC_DIR / "run-resident.sh").read_text()
    bld = (CC_DIR / "run-build.sh").read_text()
    assert 'args+=( -e "RESIDENT_SEAT=resident" )' in res
    assert 'args+=( -e "RESIDENT_SEAT=build" )' in bld
    assert 'RESIDENT_SEAT=build' not in res
    assert 'RESIDENT_SEAT=resident' not in bld


# ── drift guard ──────────────────────────────────────────────────────────

BLOCK_RE = re.compile(
    r"# ── BEGIN credential block .*?# ── END credential block[^\n]*\n",
    re.S)

SPINE_BLOCK_RE = re.compile(
    r"# ── BEGIN spine mount block .*?# ── END spine mount block[^\n]*\n",
    re.S)

REAPER_BLOCK_RE = re.compile(
    r"# ── BEGIN container reaper block .*?# ── END container reaper block[^\n]*\n",
    re.S)


def test_credential_block_is_identical_in_both_wrappers():
    blocks = {}
    for script in ALL:
        m = BLOCK_RE.search((CC_DIR / script).read_text())
        assert m, f"{script} has no marked credential block"
        blocks[script] = m.group(0)
    assert blocks["run-resident.sh"] == blocks["run-build.sh"], (
        "credential block drifted between the two wrappers")


def test_spine_block_is_identical_in_both_wrappers():
    """Both seats, one block — restored 2026-08-12.

    THE HISTORY IS THE ASSERTION. Until 2026-08-06 this test existed in exactly
    this form. Branch B then deleted the build seat's spine and replaced this
    with `test_build_wrapper_has_no_spine_block_and_says_why`, whose docstring
    said: "If the decision has genuinely been revisited, change this test
    deliberately; do not delete it to make a paste-from-the-sibling compile."

    It was revisited, deliberately, by
    SPECS/2026-08-08-gable-build-lane-provisioning.md (confirmed by plink,
    #custodian seq 1008), which sets RESIDENT_SPINE_HOST in the build launcher
    and publishes Gable's mirror. The 08-06 reasoning was measured against
    Claudette's spine — every entry `seats: [resident]` — and generalised into
    a claim about the seat; Gable's spine has declared a build seat since the
    07-22 seat-split, applied and verified live 07-23.

    Whoever revisits it NEXT: the same rule applies in the same words. Change
    this test on purpose, with a spec behind it, or leave it alone.
    """
    blocks = {}
    for script in ALL:
        m = SPINE_BLOCK_RE.search((CC_DIR / script).read_text())
        assert m, f"{script} has no marked spine mount block"
        blocks[script] = m.group(0)
        assert "/opt/spine:ro" in m.group(0)
    assert blocks["run-resident.sh"] == blocks["run-build.sh"], (
        "spine mount block drifted between the two wrappers")


def test_the_build_seat_s_other_two_deletions_still_stand():
    """Branch B deleted THREE things from the build wrapper: the spine, the
    broker socket, and house_memory. Only the spine came back. Pinned together
    so restoring one is never read as licence to restore the rest."""
    text = (CC_DIR / "run-build.sh").read_text()
    assert "NO BROKER SOCKET" in text
    assert "NO /opt/house_memory" in text
    # And the task kernel is still wired: mounting a spine is not the cutover,
    # so build-kernel.md is still what this seat reads.
    assert "BUILD_KERNEL" in text and "build-kernel.md" in text


def test_mounting_a_spine_does_not_make_the_build_seat_read_one():
    """The distinction the whole § Kernel section turns on: the mount is
    provisioned, the cutover is not. Nothing in this wrapper may quietly stop
    copying the task kernel just because /opt/spine appeared."""
    text = (CC_DIR / "run-build.sh").read_text()
    assert 'cp -f "$BUILD_KERNEL" "$HOME_VOL/.claude/CLAUDE.md"' in text
    code = [ln for ln in text.splitlines() if not ln.lstrip().startswith("#")]
    assert not any("RESIDENT_SPINE_DIR" in ln for ln in code), (
        "the cutover line lives in the /config env file, not in the wrapper — "
        "see test_spine_mount_does_not_set_resident_spine_dir")


def test_build_wrapper_has_no_broker_socket():
    """A build must not be able to start a build. Deleted at the mount, not
    filtered at the verb — see the 'NO BROKER SOCKET' block in run-build.sh."""
    text = (CC_DIR / "run-build.sh").read_text()
    assert "/run/disjorn-broker" not in text.replace("NO BROKER SOCKET", ""), (
        "run-build.sh mounts the broker socket again — that is how a 2026-08-05 "
        "build session could see `broker start-build` from inside a build"
    )
    assert "BROKER_SOCKET=" not in text


def test_build_wrapper_does_not_mount_the_gatehouse():
    """NO WRITABLE PATH OUT (2026-08-13, SPECS/2026-08-13-build-publish-path.md).

    This assertion is the exact inverse of the one that stood here until
    2026-08-13 (`test_build_wrapper_mounts_the_gatehouse_writable`, "the one
    writable path out of a build container"). It was inverted on purpose, with
    a confirmed spec behind it: `--userns keep-id` does not map supplementary
    groups, so the `gatehouse` group never existed inside the container and
    every in-container push wrote objects by uid-ownership accident. The
    product now leaves by the wrapper's post-exit harvest, host-side.

    If you are here because a build "has nowhere to push" — correct, it does
    not push. See the harvest at the bottom of run-build.sh.
    """
    text = (CC_DIR / "run-build.sh").read_text()
    code = [ln for ln in text.splitlines() if not ln.lstrip().startswith("#")]
    assert not any("/run/gatehouse" in ln for ln in code), (
        "run-build.sh references /run/gatehouse in code again — the container "
        "must have no writable path out")
    assert not any("GATEHOUSE_IN_CONTAINER" in ln for ln in code)


def test_reaper_blocks_diverge_by_contract():
    """They were byte-identical until 2026-08-13; now they legitimately differ,
    and the difference is pinned rather than dropped.

    run-resident.sh still `exec`s podman, so the watchdog sibling is the whole
    mechanism: a trap would cost the exec and would not cover the SIGKILL both
    supervisors actually send. run-build.sh cannot exec any more — it has to
    outlive its container to harvest — so it is podman's PARENT, and a killed
    parent must not leave a container writing into ~/work while the next launch
    deletes it ([1175]). It therefore carries BOTH: the watchdog for SIGKILL
    and a trap for EXIT/INT/TERM.

    What must stay common is the substance of the reap, and that is asserted
    for both: by cid (never by name), `rm -f -t 0 --ignore`.
    """
    blocks = {}
    for script in ALL:
        m = REAPER_BLOCK_RE.search((CC_DIR / script).read_text())
        assert m, f"{script} has no marked container reaper block"
        blocks[script] = m.group(0)
        assert "--cidfile" in blocks[script], script
        assert "podman rm -f -t 0 --ignore" in blocks[script], script

    # The resident seat: watchdog only, exec intact, no trap. (Matched as a
    # STATEMENT, not as the word — the block's prose explains at length why a
    # trap is not the mechanism there, and that prose must survive.)
    assert not re.search(r"^\s*trap\s", blocks["run-resident.sh"], re.M), (
        "run-resident.sh grew a trap — it still `exec`s podman, where a trap "
        "cannot run and the exec is the mechanism")
    # The build seat: watchdog AND trap, on every signal a trap can see.
    bld = blocks["run-build.sh"]
    assert "trap '_build_reap' EXIT" in bld
    assert "INT" in bld and "TERM" in bld
    assert "_reap_pid" in bld, "the SIGKILL watchdog must still be there too"


def test_the_wrappers_launch_podman_by_their_seat_s_contract():
    """run-resident.sh execs; run-build.sh runs podman as a child and harvests.

    The exec was load-bearing for years — same PID, same stdin, same exit
    status, no extra shell between the supervisor and podman — so it is still
    asserted for the summon path, which has no reason to give it up. The build
    seat gave it up for the harvest, and the shape it gave it up FOR is pinned
    here: `&` + `wait` (so the trap is not deferred behind a foreground
    command) with an explicit `<&0` (so the spec on stdin is not redirected
    from /dev/null, as bash does to async commands).
    """
    res = (CC_DIR / "run-resident.sh").read_text()
    bld = (CC_DIR / "run-build.sh").read_text()
    assert 'exec podman "${args[@]}"' in res
    assert 'podman "${args[@]}" <&0 &' in bld
    assert 'wait "$_podman_pid"' in bld
    # The one exec left in the build wrapper is the DETACHED path, where the
    # caller owns the container and there is nothing yet to harvest.
    lines = bld.splitlines()
    execs = [i for i, ln in enumerate(lines)
             if ln.strip() == 'exec podman "${args[@]}"']
    assert len(execs) == 1, "run-build.sh must exec podman on the detached path only"
    assert any('DETACHED" = "1"' in ln for ln in lines[execs[0] - 3:execs[0]])


def test_wrappers_never_hardcode_a_spine_under_home_plink():
    """/home/plink is 0700 and un-mountable by rootless podman; a default
    pointing there would fail at launch, and 'fixing' it by loosening
    /home/plink is the one repair this design forbids."""
    for script in ALL:
        for line in (CC_DIR / script).read_text().splitlines():
            if line.lstrip().startswith("#"):
                continue
            assert "/home/plink/bots" not in line, (script, line)


def test_no_real_looking_credential_is_committed():
    """Belt and braces: the wrappers must carry placeholders only."""
    for script in ALL:
        text = (CC_DIR / script).read_text()
        for m in re.findall(r"sk-ant-[A-Za-z0-9_-]+", text):
            assert "..." in m or "PLACEHOLDER" in m or len(m) < 24, m
