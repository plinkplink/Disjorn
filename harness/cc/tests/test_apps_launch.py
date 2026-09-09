"""The apps-builder launcher: every refusal, and the exact argv it execs.

SPECS/2026-09-06-apps-builder-seat.md §C. The launcher is a sudoers-scoped
root program, so its ARGUMENT CHARSETS are the whole wall between "the broker
asked for a turn" and "an arbitrary path ran as a system user" — which is why
the spec writes them out rather than leaving them to code, and why every one of
them gets a refusal test here. A wall with an untested gap is a gap.

Everything runs the REAL program as a subprocess under
DISJORN_APPS_LAUNCH_DRY_RUN=1, which runs every check and prints the argv
instead of exec'ing systemd-run. Two other dry-run-only overrides make the
suite runnable on a box that is not the house: FAKE_SEAT (there is no
res-appsbuilding here) and WRAPPER (there is no /usr/local/lib/disjorn). Both
are unreachable through the privileged path — sudo's env_reset drops DISJORN_*
before the program starts — so they add no code path an attacker could use.

Sibling suite: test_apps_harvest.py (the post-exit half of the same turn).
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import pytest

CC_DIR = Path(__file__).resolve().parent.parent
LAUNCH = CC_DIR / "apps" / "disjorn-apps-launch"
EXAMPLE_TOML = CC_DIR / "apps" / "launch.toml.example"

# The seat as the launcher would resolve it on the house box: uid, home, group.
FAKE_SEAT = "997:/home/res-appsbuilding:res-appsbuilding"
FAKE_UID = 997

GOOD = ("res-gable", "12", "3", "abc234567xyz")    # principal, session, turn, app


@pytest.fixture()
def seat(tmp_path):
    """A scratch prompt directory, a config table pointing at it, and a valid
    prompt file inside it."""
    prompts = tmp_path / "apps-prompts"
    prompts.mkdir()
    prompt = prompts / "12-3.md"
    prompt.write_text("Build a tip calculator.\n", encoding="utf-8")
    wrapper = tmp_path / "run-apps.sh"
    wrapper.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    config = tmp_path / "launch.toml"
    config.write_text(f"""
[prompt_dirs]
res-gable = "{prompts}"

[runner]
command = ["claude", "-p", "--output-format", "stream-json", "--verbose"]
model = "claude-opus-5"

[apps]
turn_max_sec = 1800
ponytail_mode = "full"
image = "localhost/disjorn-apps-builder:latest"
""", encoding="utf-8")
    return {"dir": prompts, "prompt": prompt, "config": config, "wrapper": wrapper}


def run(seat, *args, config=None, env_extra=None):
    """The launcher, dry-run, as a subprocess. Returns CompletedProcess."""
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "DISJORN_APPS_LAUNCH_DRY_RUN": "1",
        "DISJORN_APPS_LAUNCH_CONFIG": str(config or seat["config"]),
        "DISJORN_APPS_LAUNCH_WRAPPER": str(seat["wrapper"]),
        "DISJORN_APPS_LAUNCH_HARVEST": "/usr/local/lib/disjorn/apps_harvest.py",
        "DISJORN_APPS_LAUNCH_FAKE_SEAT": FAKE_SEAT,
    }
    env.update(env_extra or {})
    return subprocess.run([sys.executable, str(LAUNCH), *args],
                          capture_output=True, text=True, env=env)


def refuse(seat, *args, **kw):
    """Assert a refusal: exit 64, a REFUSED line on stderr, no argv on stdout."""
    proc = run(seat, *args, **kw)
    assert proc.returncode == 64, (
        f"expected exit 64, got {proc.returncode}\n{proc.stderr}{proc.stdout}")
    assert "REFUSED" in proc.stderr
    assert proc.stdout.strip() == ""
    return proc


def accept(seat, *args, **kw):
    proc = run(seat, *args, **kw)
    assert proc.returncode == 0, f"expected exit 0:\n{proc.stderr}"
    return json.loads(proc.stdout)


# ───────────────────────────────────────────────────── the happy path first ──

def test_valid_run_prints_argv_and_exits_zero(seat):
    argv = accept(seat, "run", *GOOD, str(seat["prompt"]))
    assert argv[0] == "/usr/bin/systemd-run"
    # The wrapper gets NO prompt path: the prompt travels on stdin (memfd),
    # and root writes nothing under the seat's tree (Claudette's block).
    assert argv[-4:] == [str(seat["wrapper"]), "12", "3", "abc234567xyz"]
    assert not any(a.startswith("/srv/apps-turns") for a in argv)


def test_exact_systemd_run_argv(seat):
    """The argv IS the contract between the launcher and the unit. Asserted
    whole, in order, because every element of it is a decision someone made
    once: the unit name the broker will `systemctl stop`, the uid the turn runs
    as, the four resource properties spec §A names, and the APPS_* environment
    that is run-apps.sh's entire configuration."""
    argv = accept(seat, "run", *GOOD, str(seat["prompt"]))
    assert argv == [
        "/usr/bin/systemd-run",
        "--unit=disjorn-apps-12-3",
        "--description=Disjorn apps turn 12/3 (abc234567xyz)",
        "--uid=res-appsbuilding",
        "--gid=res-appsbuilding",
        "--collect",
        "--pipe",
        "--property=RuntimeMaxSec=1800",
        "--property=MemoryMax=4G",
        "--property=LimitFSIZE=268435456",
        "--property=LimitCORE=0",
        "--property=TasksMax=512",
        f"--setenv=XDG_RUNTIME_DIR=/run/user/{FAKE_UID}",
        "--setenv=HOME=/home/res-appsbuilding",
        "--setenv=APPS_SESSION=12",
        "--setenv=APPS_TURN=3",
        "--setenv=APPS_APP_ID=abc234567xyz",
        "--setenv=APPS_IMAGE=localhost/disjorn-apps-builder:latest",
        "--setenv=APPS_MODEL=claude-opus-5",
        "--setenv=APPS_PONYTAIL_MODE=full",
        '--setenv=APPS_RUNNER_COMMAND=["claude", "-p", "--output-format", '
        '"stream-json", "--verbose"]',
        "--setenv=APPS_TURN_MAX_SEC=1800",
        "--setenv=APPS_PROMPT_MAX_BYTES=65536",
        "--setenv=APPS_REPO_ROOT=/srv/apps",
        "--setenv=APPS_TURNS_ROOT=/srv/apps-turns",
        "--setenv=APPS_WWW_ROOT=/srv/apps-www",
        "--setenv=APPS_QUARANTINE_ROOT=/srv/apps-quarantine",
        "--setenv=APPS_CONFIG_DIR=/srv/disjorn-build-config/appsbuilding",
        "--setenv=APPS_HARVEST=/usr/local/lib/disjorn/apps_harvest.py",
        "--",
        str(seat["wrapper"]), "12", "3", "abc234567xyz",
    ]


def test_the_seat_uid_is_never_an_argument(seat, tmp_path):
    """--uid is res-appsbuilding whoever asked. The <resident> argument names a
    PROMPT DIRECTORY and nothing else (spec §C)."""
    for principal in ("res-gable", "res-claudette"):
        config = tmp_path / f"{principal}.toml"
        config.write_text(seat["config"].read_text().replace(
            "res-gable =", f"{principal} ="), encoding="utf-8")
        argv = accept(seat, "run", principal, "12", "3", "abc234567xyz",
                      str(seat["prompt"]), config=config)
        assert "--uid=res-appsbuilding" in argv
        assert not any(a.startswith("--uid=") and a != "--uid=res-appsbuilding"
                       for a in argv)


def test_turn_max_sec_drives_runtimemaxsec(seat, tmp_path):
    config = tmp_path / "short.toml"
    config.write_text(seat["config"].read_text().replace(
        "turn_max_sec = 1800", "turn_max_sec = 60"), encoding="utf-8")
    argv = accept(seat, "run", *GOOD, str(seat["prompt"]), config=config)
    assert "--property=RuntimeMaxSec=60" in argv
    assert "--setenv=APPS_TURN_MAX_SEC=60" in argv


# ─────────────────────────────────────────────────────────────── the modes ───

def test_no_arguments_is_refused(seat):
    refuse(seat)


# ── stop (slice (iv), SPECS/2026-09-08-apps-stop-turn.md, 2444) ────────────

def test_stop_argv_byte_for_byte(seat):
    """`stop` names a turn and nothing else; the unit name is derived from the
    same charsets `run` enforces, so a caller can never aim systemctl at a
    unit it did not launch."""
    assert accept(seat, "stop", "res-gable", "12", "3") == [
        "/usr/bin/systemctl", "stop", "disjorn-apps-12-3"]


@pytest.mark.parametrize("args", [
    [],                                   # nothing
    ["res-gable", "12"],                  # too few
    ["res-gable", "12", "3", "extra"],    # too many (run's shape)
    ["keyboard", "12", "3"],              # the retired principal
    ["res-gable", "0", "3"],              # session charset
    ["res-gable", "12", "03"],            # turn charset
    ["res-gable", "12", "3 --now"],       # a flag riding in an argument
    ["res-gable", "../12", "3"],          # a path riding in an argument
])
def test_stop_refuses_every_bad_shape(seat, args):
    refuse(seat, "stop", *args)


def test_stop_drops_the_marker_in_the_seats_turn_dir(seat, tmp_path):
    """The marker is how the harvest tells the user's stop from the clock:
    both arrive as one SIGTERM. Created once; a second stop finds it and says
    so rather than failing."""
    # The walk refuses any dir the seat does not own, so the fake seat here
    # IS this test's uid: tmp_path is ours, and that is the case being proved.
    mine = ":".join([str(os.getuid()), *FAKE_SEAT.split(":")[1:]])
    turn_dir = tmp_path / "turns" / "12" / "3"
    turn_dir.mkdir(parents=True)
    env = {"DISJORN_APPS_LAUNCH_TURNS_ROOT": str(tmp_path / "turns"),
           "DISJORN_APPS_LAUNCH_FAKE_SEAT": mine}
    first = json.loads(run(seat, "stop", "res-gable", "12", "3",
                           env_extra=env).stdout)
    assert first == {"argv": ["/usr/bin/systemctl", "stop", "disjorn-apps-12-3"],
                     "marker": True}
    marker = turn_dir / "stop-requested"
    assert marker.is_file() and marker.stat().st_size == 0
    again = json.loads(run(seat, "stop", "res-gable", "12", "3",
                           env_extra=env).stdout)
    assert again["marker"] is False


STOP_ARGV = ["/usr/bin/systemctl", "stop", "disjorn-apps-12-3"]


def _stop_dry(seat, env):
    """A dry stop on a named turns root: exit 0 and the JSON, whatever the
    marker did. The argv being present in every case IS the property."""
    proc = run(seat, "stop", "res-gable", "12", "3", env_extra=env)
    assert proc.returncode == 0, f"a marker problem must not exit:\n{proc.stderr}"
    return json.loads(proc.stdout), proc.stderr


def test_a_stop_with_no_turn_dir_yet_still_sends_the_stop(seat, tmp_path):
    """THE gate test (Claudette #2447). run-apps.sh makes the turn dir inside
    the unit, after systemd has started it, so a stop pressed in the first
    moments of a turn finds no dir. The marker is a label; the stop is the
    act. No dir: the argv is still built and the exec still happens, and the
    unit's journal keeps one warning saying the room will read it as a
    timeout. Root still creates nothing."""
    (tmp_path / "turns").mkdir()
    out, err = _stop_dry(seat, {"DISJORN_APPS_LAUNCH_TURNS_ROOT": str(tmp_path / "turns")})
    assert out["argv"] == STOP_ARGV
    assert out["marker"].startswith("no turn dir yet: 12/3")
    assert "stop marker not written" in err and "sent regardless" in err
    assert not (tmp_path / "turns" / "12").exists()


def test_a_turn_dir_the_seat_does_not_own_gets_no_marker_but_the_stop(seat, tmp_path):
    """Root never writes by name into a tree the seat controls. The walk
    fstat's each component and refuses a directory not owned by the seat —
    which, with a fake seat uid that is not ours, is every directory here.
    Refused means not written; it does not mean not stopped."""
    other = FAKE_SEAT.split(":")
    turn_dir = tmp_path / "turns" / "12" / "3"
    turn_dir.mkdir(parents=True)
    env = {"DISJORN_APPS_LAUNCH_TURNS_ROOT": str(tmp_path / "turns"),
           "DISJORN_APPS_LAUNCH_FAKE_SEAT": ":".join([str(os.getuid() + 1), *other[1:]])}
    out, _ = _stop_dry(seat, env)
    assert out["argv"] == STOP_ARGV
    assert "not the seat's own" in out["marker"]
    assert not (turn_dir / "stop-requested").exists()


def test_a_symlinked_turn_dir_gets_no_marker_anywhere_but_the_stop(seat, tmp_path):
    """A seat that replaces its turn dir with a symlink to somewhere root can
    write gets no file at the far end — and its turn still stops."""
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (tmp_path / "turns" / "12").mkdir(parents=True)
    (tmp_path / "turns" / "12" / "3").symlink_to(elsewhere)
    mine = ":".join([str(os.getuid()), *FAKE_SEAT.split(":")[1:]])
    out, _ = _stop_dry(seat, {"DISJORN_APPS_LAUNCH_TURNS_ROOT": str(tmp_path / "turns"),
                              "DISJORN_APPS_LAUNCH_FAKE_SEAT": mine})
    assert out["argv"] == STOP_ARGV
    assert not (elsewhere / "stop-requested").exists()
    assert not (tmp_path / "turns" / "12" / "3" / "stop-requested").exists()


def test_unknown_mode_is_refused(seat):
    refuse(seat, "launch", *GOOD, str(seat["prompt"]))


@pytest.mark.parametrize("extra", [[], ["one"], ["a", "b", "c"],
                                   ["res-gable", "12", "3", "abc234567xyz"]])
def test_wrong_argument_count_is_refused(seat, extra):
    refuse(seat, "run", *extra)


def test_extra_trailing_argument_is_refused(seat):
    refuse(seat, "run", *GOOD, str(seat["prompt"]), "--uid=0")


# ─────────────────────────────────────────── charsets: the principal (§C) ────

@pytest.mark.parametrize("principal", [
    "res-gable", "res-claudette", "res-a", "res-" + "a" * 24,
])
def test_principal_accepted(seat, tmp_path, principal):
    config = tmp_path / "p.toml"
    config.write_text(
        f'[prompt_dirs]\n{principal} = "{seat["dir"]}"\n', encoding="utf-8")
    accept(seat, "run", principal, "12", "3", "abc234567xyz",
           str(seat["prompt"]), config=config)


@pytest.mark.parametrize("principal", [
    "",                       # empty
    "gable",                  # missing the res- prefix
    "res-",                   # prefix only
    "res-Gable",              # uppercase
    "res-gable1",             # digit
    "res-gable-x",            # hyphen inside the name
    "res-gable ",             # trailing space
    " res-gable",             # leading space
    "res-" + "a" * 25,        # one over the 24-char limit
    "res-gable/../root",      # path traversal
    "res-gable\nres-evil",    # newline
    "root",
    "keyboard",               # slice (i)'s literal, gone with its proving turn
    "res-gable;id",
    "--uid=0",
])
def test_principal_refused(seat, principal):
    refuse(seat, "run", principal, "12", "3", "abc234567xyz", str(seat["prompt"]))


def test_principal_with_no_mapped_directory_is_refused(seat):
    """`res-claudette` is a perfectly legal principal and still has no entry in
    THIS table. The charset says the name is well formed; the table says
    whether it may launch. Both must pass — which is what makes deleting a
    line from launch.toml a complete removal of that principal, and it is how
    slice (i)'s `keyboard` entry was removed twice over."""
    refuse(seat, "run", "res-claudette", "12", "3", "abc234567xyz",
           str(seat["prompt"]))


def test_the_keyboard_principal_is_gone(seat, tmp_path):
    """Slice (i) admitted exactly one extra literal so its proving turn had a
    caller to be; slice (ii) has the `apps-build` verb and spec §C's charset
    wins (keyboard ruling D-C2). The refusal is the CHARSET's, so a stale
    /etc/disjorn-apps/launch.toml that still maps the name cannot bring the
    principal back — an installed config outlives a deploy, and this is the
    wall that does not."""
    stale = tmp_path / "stale.toml"
    stale.write_text(seat["config"].read_text().replace(
        "res-gable =", f'keyboard = "{seat["dir"]}"\nres-gable ='),
        encoding="utf-8")
    proc = refuse(seat, "run", "keyboard", "12", "3", "abc234567xyz",
                  str(seat["prompt"]), config=stale)
    assert "not res-<name>" in proc.stderr


# ───────────────────────────────────────────── charsets: session and turn ────

@pytest.mark.parametrize("session", ["1", "9", "12", "999999999"])
def test_session_accepted(seat, session):
    accept(seat, "run", "res-gable", session, "3", "abc234567xyz",
           str(seat["prompt"]))


@pytest.mark.parametrize("session", [
    "", "0", "01", "-1", "+1", "1.0", "1e3", "1234567890",   # 10 digits
    " 1", "1 ", "1\n2", "1;id", "abc", "0x10", "١٢",          # arabic-indic
])
def test_session_refused(seat, session):
    refuse(seat, "run", "res-gable", session, "3", "abc234567xyz",
           str(seat["prompt"]))


@pytest.mark.parametrize("turn", ["1", "9", "42", "9999"])
def test_turn_accepted(seat, turn):
    accept(seat, "run", "res-gable", "12", turn, "abc234567xyz",
           str(seat["prompt"]))


@pytest.mark.parametrize("turn", [
    "", "0", "007", "10000", "-1", "1.5", " 2", "2 ", "2;id", "two", "٣",
])
def test_turn_refused(seat, turn):
    refuse(seat, "run", "res-gable", "12", turn, "abc234567xyz",
           str(seat["prompt"]))


# ────────────────────────────────────────────────── charsets: the app id ─────

@pytest.mark.parametrize("app_id", [
    "aaaaaaaaaaaa", "abc234567xyz", "234567234567", "zzzzzzzzzzzz",
])
def test_app_id_accepted(seat, app_id):
    accept(seat, "run", "res-gable", "12", "3", app_id, str(seat["prompt"]))


@pytest.mark.parametrize("app_id", [
    "",
    "aaaaaaaaaaa",            # 11 — one short
    "aaaaaaaaaaaaa",          # 13 — one long
    "AAAAAAAAAAAA",           # uppercase
    "abcdefghijk0",           # 0 is not in base32-lower
    "abcdefghijk1",           # nor is 1
    "abcdefghijk8",           # nor 8
    "abcdefghijk9",           # nor 9
    "abcdefghij-k",           # hyphen
    "abcdefghij_k",           # underscore
    "../../etc/pa",           # exactly 12 chars, and a path
    "aaaaaaaaaaa/",
    "aaaa aaaaaaa",
    "aaaaaaaaaaa\n",
])
def test_app_id_refused(seat, app_id):
    refuse(seat, "run", "res-gable", "12", "3", app_id, str(seat["prompt"]))


def test_app_id_is_the_only_thing_that_names_a_directory(seat):
    """The app id lands in three /srv paths and a podman --name. Its charset is
    the reason none of them can be talked into a traversal — asserted here by
    reading it back out of the argv the launcher built."""
    argv = accept(seat, "run", "res-gable", "12", "3", "abc234567xyz",
                  str(seat["prompt"]))
    assert "--setenv=APPS_APP_ID=abc234567xyz" in argv
    joined = " ".join(argv)
    assert ".." not in joined


# ──────────────────────────────────── the prompt path: realpath confinement ──

def test_prompt_must_be_absolute(seat):
    refuse(seat, "run", *GOOD, "12-3.md")


def test_prompt_must_exist(seat):
    refuse(seat, "run", *GOOD, str(seat["dir"] / "nope.md"))


def test_prompt_must_be_a_regular_file(seat):
    subdir = seat["dir"] / "sub"
    subdir.mkdir()
    refuse(seat, "run", *GOOD, str(subdir))


def test_prompt_outside_the_mapped_directory_is_refused(seat, tmp_path):
    outside = tmp_path / "elsewhere.md"
    outside.write_text("x", encoding="utf-8")
    refuse(seat, "run", *GOOD, str(outside))


def test_prompt_traversal_is_refused(seat, tmp_path):
    outside = tmp_path / "elsewhere.md"
    outside.write_text("x", encoding="utf-8")
    refuse(seat, "run", *GOOD, str(seat["dir"] / ".." / "elsewhere.md"))


def test_prompt_symlink_escape_is_refused(seat, tmp_path):
    """A symlink INSIDE the mapped directory pointing out of it. This is the
    reason the check is realpath-then-compare and not a string prefix on the
    argument: the argument looks perfectly confined."""
    outside = tmp_path / "secret.md"
    outside.write_text("the key is ...", encoding="utf-8")
    link = seat["dir"] / "innocent.md"
    link.symlink_to(outside)
    proc = refuse(seat, "run", *GOOD, str(link))
    assert "escapes" in proc.stderr


def test_prompt_symlinked_directory_escape_is_refused(seat, tmp_path):
    """The same trick one level up: a symlinked SUBDIRECTORY of the mapped
    prompt dir whose target is elsewhere."""
    outside = tmp_path / "outdir"
    outside.mkdir()
    (outside / "p.md").write_text("x", encoding="utf-8")
    (seat["dir"] / "sub").symlink_to(outside)
    refuse(seat, "run", *GOOD, str(seat["dir"] / "sub" / "p.md"))


def test_sibling_directory_with_a_shared_prefix_is_refused(seat, tmp_path):
    """`/…/apps-prompts-evil/p.md` must not pass for the mapped directory
    `/…/apps-prompts`. A plain startswith would accept it; the containment test
    is segment-wise for exactly this."""
    evil = tmp_path / "apps-prompts-evil"
    evil.mkdir()
    prompt = evil / "p.md"
    prompt.write_text("x", encoding="utf-8")
    refuse(seat, "run", *GOOD, str(prompt))


def test_prompt_path_with_a_newline_is_refused(seat):
    refuse(seat, "run", *GOOD, str(seat["prompt"]) + "\nrm -rf /")


def test_a_symlink_prompt_is_refused_even_when_it_points_inside(seat):
    """Claudette's second slice-(i) block: the thing behind the wall is a
    file, not a name. The final component is opened O_NOFOLLOW, so a symlink
    is refused outright — even one that currently points inside — because
    "currently" is the principal's to change between any two syscalls."""
    link = seat["dir"] / "alias.md"
    link.symlink_to(seat["prompt"])
    refuse(seat, "run", *GOOD, str(link))


def test_the_tripwire_a_planted_symlink_to_the_credential_is_refused(seat, tmp_path):
    """The exact attack: the named file passes every name check, then is
    swapped for a symlink to the credential drop file. With one O_NOFOLLOW
    open and identity taken from the fd there is no window: exit 64, and the
    target's bytes never reach anything."""
    secret = tmp_path / "appsbuilding-env"
    secret.write_text("ANTHROPIC_API_KEY=sk-ant-not-for-you\n", encoding="utf-8")
    victim = seat["dir"] / "12-3.md"
    victim.unlink()
    victim.symlink_to(secret)
    proc = run(seat, "run", *GOOD, str(victim))
    assert proc.returncode == 64, proc.stderr
    assert "sk-ant-not-for-you" not in proc.stdout + proc.stderr
    assert "REFUSED" in proc.stderr

    # The sharper form: the target sits INSIDE the mapped directory, so every
    # name check passes and only the O_NOFOLLOW open can refuse it.
    inside_secret = seat["dir"] / "not-a-prompt.env"
    inside_secret.write_text("ANTHROPIC_API_KEY=sk-ant-still-not-for-you\n", encoding="utf-8")
    victim.unlink()
    victim.symlink_to(inside_secret)
    proc = run(seat, "run", *GOOD, str(victim))
    assert proc.returncode == 64, proc.stderr
    assert "sk-ant-still-not-for-you" not in proc.stdout + proc.stderr
    assert "symlink" in proc.stderr


def test_a_planted_fifo_cannot_wedge_the_launcher(seat):
    """Claudette #2320: O_NOFOLLOW refuses a symlink, but a FIFO is not a
    symlink — without O_NONBLOCK a resident plants one at the prompt path and
    root blocks in open() forever, under sudo, before fstat ever runs. Now it
    is refused in the time it takes to say so."""
    seat["prompt"].unlink()
    os.mkfifo(seat["prompt"])
    t0 = time.monotonic()
    proc = subprocess.run(
        [sys.executable, str(LAUNCH), "run", *GOOD, str(seat["prompt"])],
        capture_output=True, text=True, timeout=20, env={
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "DISJORN_APPS_LAUNCH_DRY_RUN": "1",
            "DISJORN_APPS_LAUNCH_CONFIG": str(seat["config"]),
            "DISJORN_APPS_LAUNCH_WRAPPER": str(seat["wrapper"]),
            "DISJORN_APPS_LAUNCH_HARVEST": "/usr/local/lib/disjorn/apps_harvest.py",
            "DISJORN_APPS_LAUNCH_FAKE_SEAT": FAKE_SEAT,
        })
    assert time.monotonic() - t0 < 10
    assert proc.returncode == 64 and "not a regular file" in proc.stderr


def test_the_prompt_must_be_owned_by_the_principal(seat, tmp_path):
    """Claudette #2320: everything checked WHERE the file is, nothing WHOSE
    it is. The principal's uid is the owner of its mapped directory; a file
    with any other owner — root, the seat, a neighbour — is refused, which
    closes the hardlink-into-the-prompt-dir shape by code and not by
    fs.protected_hardlinks. Exercised for real where a second uid is
    available (root, or the house's res-* users); the root-owned-directory
    refusal below runs everywhere."""
    # A directory owned by root as the mapped one: refused as misconfiguration.
    for candidate in ("/", "/etc"):
        if os.stat(candidate).st_uid == 0:
            root_owned = tmp_path / "launch-root-owned.toml"
            root_owned.write_text(
                f'[prompt_dirs]\nres-gable = "{candidate}"\n', encoding="utf-8")
            proc = refuse(seat, "run", *GOOD, f"{candidate}/nope.md",
                          config=root_owned)
            assert "owned by root" in proc.stderr
            break
    # A root-owned FILE inside an honest directory: the wall the sysctl used
    # to be. Needs a root-owned regular file we can name; /etc/hostname is one
    # on every Debian box, reached through a symlink planted in the mapped
    # directory... which O_NOFOLLOW already refuses. So the owner check is
    # exercised where a hardlink can be made (same filesystem, and only when
    # protected_hardlinks lets an unprivileged user link a foreign file —
    # which is exactly the setting we no longer rely on): try, and assert
    # the refusal names the owner if the link could be made at all.
    foreign = None
    for candidate in (Path("/etc/hostname"), Path("/etc/os-release")):
        try:
            if candidate.is_file() and candidate.stat().st_uid != os.geteuid():
                link = seat["dir"] / "planted.md"
                os.link(candidate, link)
                foreign = link
                break
        except OSError:
            continue
    if foreign is not None:
        proc = refuse(seat, "run", *GOOD, str(foreign))
        assert "owned by" in proc.stderr
    # And deterministically, whatever the sysctl: the same open_prompt() with
    # the principal resolved to a uid the file does not carry. The comparison
    # is what is under test; the fd path is the real one (a real file, a
    # real fstat) and only the answer to "who is the principal" is planted.
    code = (
        "import sys\n"
        f"src = open({str(LAUNCH)!r}).read()\n"
        "ns = {'__name__': 'launcher_under_test'}\n"
        "exec(compile(src, 'launcher', 'exec'), ns)\n"
        "ns['principal_uid'] = lambda principal, root: 4242\n"
        "import tomllib\n"
        f"cfg = tomllib.load(open({str(seat['config'])!r}, 'rb'))\n"
        f"ns['open_prompt']('res-gable', {str(seat['prompt'])!r}, cfg)\n"
    )
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert r.returncode == 64, r.stderr
    assert f"owned by uid {os.geteuid()}, not by res-gable (uid 4242)" in r.stderr
    # ...and with the truth planted, the same file is accepted.
    code_ok = code.replace("lambda principal, root: 4242",
                           f"lambda principal, root: {os.geteuid()}")
    r = subprocess.run([sys.executable, "-c", code_ok], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


# ─────────────────────────────────────────────────── the config table itself ──

def test_missing_config_is_refused(seat, tmp_path):
    refuse(seat, "run", *GOOD, str(seat["prompt"]),
           config=tmp_path / "nope.toml")


def test_malformed_config_is_refused(seat, tmp_path):
    bad = tmp_path / "bad.toml"
    bad.write_text("[prompt_dirs\nres-gable = ", encoding="utf-8")
    refuse(seat, "run", *GOOD, str(seat["prompt"]), config=bad)


def test_config_without_prompt_dirs_is_refused(seat, tmp_path):
    bad = tmp_path / "empty.toml"
    bad.write_text('[runner]\nmodel = "claude-opus-5"\n', encoding="utf-8")
    refuse(seat, "run", *GOOD, str(seat["prompt"]), config=bad)


def test_relative_mapped_directory_is_refused(seat, tmp_path):
    bad = tmp_path / "rel.toml"
    bad.write_text('[prompt_dirs]\nres-gable = "apps-prompts"\n', encoding="utf-8")
    refuse(seat, "run", *GOOD, str(seat["prompt"]), config=bad)


@pytest.mark.parametrize("line, ok", [
    ('command = ["claude"]', True),
    ('command = []', False),
    ('command = "claude -p"', False),
    ('command = ["claude", 3]', False),
    ('command = ["claude", "--x\\ny"]', False),
])
def test_runner_command_is_validated(seat, tmp_path, line, ok):
    config = tmp_path / "runner.toml"
    config.write_text(re.sub(r"^command = .*$", line,
                             seat["config"].read_text(), flags=re.M),
                      encoding="utf-8")
    (accept if ok else refuse)(seat, "run", *GOOD, str(seat["prompt"]),
                               config=config)


@pytest.mark.parametrize("mode, ok", [
    ("full", True), ("lite", True), ("ultra", True), ("off", True),
    ("FULL", False), ("maximum", False), ("", False),
])
def test_ponytail_mode_is_validated(seat, tmp_path, mode, ok):
    config = tmp_path / "pony.toml"
    config.write_text(seat["config"].read_text().replace(
        'ponytail_mode = "full"', f'ponytail_mode = "{mode}"'), encoding="utf-8")
    (accept if ok else refuse)(seat, "run", *GOOD, str(seat["prompt"]),
                               config=config)


@pytest.mark.parametrize("value, ok", [
    ("1800", True), ("60", True), ("0", False), ("-5", False),
    ("86401", False), ('"1800"', False),
])
def test_turn_max_sec_is_validated(seat, tmp_path, value, ok):
    config = tmp_path / "clock.toml"
    config.write_text(seat["config"].read_text().replace(
        "turn_max_sec = 1800", f"turn_max_sec = {value}"), encoding="utf-8")
    (accept if ok else refuse)(seat, "run", *GOOD, str(seat["prompt"]),
                               config=config)


def test_model_with_a_space_is_refused(seat, tmp_path):
    config = tmp_path / "model.toml"
    config.write_text(seat["config"].read_text().replace(
        '"claude-opus-5"', '"claude opus 5"'), encoding="utf-8")
    refuse(seat, "run", *GOOD, str(seat["prompt"]), config=config)


def test_shipped_example_config_parses_and_launches(seat, tmp_path):
    """The example the keyboard script installs must actually work: same
    defaults, same two principals — the seats, and nobody else. Its
    prompt_dirs point at real house paths, so only the parse and the table
    shape are asserted here."""
    import tomllib
    data = tomllib.loads(EXAMPLE_TOML.read_text(encoding="utf-8"))
    assert set(data["prompt_dirs"]) == {"res-gable", "res-claudette"}
    assert data["runner"]["command"][0] == "claude"
    assert data["runner"]["model"] == "claude-opus-5"
    assert data["apps"]["turn_max_sec"] == 1800
    assert data["apps"]["ponytail_mode"] == "full"
    assert data["apps"]["image"] == "localhost/disjorn-apps-builder:latest"


# ────────────────────────────────────────────── refusal happens before work ──

def test_refusal_writes_nothing_and_names_itself(seat):
    """Exit 64 before any privilege is used (spec §C), and the reason on
    stderr — the broker logs it as a flat sentence."""
    proc = refuse(seat, "run", "root", "12", "3", "abc234567xyz",
                  str(seat["prompt"]))
    assert proc.stderr.startswith("disjorn-apps-launch: REFUSED:")
    assert "/usr/bin/systemd-run" not in proc.stdout


# ──────────────────────────────────────────────────────── the image pin ─────

def test_apps_image_pins_the_same_claude_code_as_the_resident_image():
    """Both seats run the same pinned Claude Code CLI. The version is a value
    copied into a second file, which is this project's most expensive recurring
    defect shape — so the copy is allowed and this is the thing that notices.

    (The other half of the image drift test spec §F asks for — MANIFEST.toml
    hashes against the shelf files on disk — belongs with the shelf, which is
    the other hand's surface, and is not asserted here.)"""
    def pin(path: Path) -> str:
        m = re.search(r"^ARG CLAUDE_CODE_VERSION=(\S+)\s*$",
                      path.read_text(encoding="utf-8"), re.M)
        assert m, f"no ARG CLAUDE_CODE_VERSION in {path}"
        return m.group(1)

    resident = pin(CC_DIR / "Containerfile")
    apps = pin(CC_DIR / "Containerfile.apps")
    assert apps == resident, (
        f"Containerfile.apps pins claude-code {apps}, Containerfile pins "
        f"{resident} — bump both together or neither")


# ───────────────────────────────────────── the prompt rides stdin, root writes nothing ──

def test_prompt_on_stdin_puts_exactly_the_bytes_on_fd_zero():
    """Claudette's block on slice (i): root must never create or chown inside
    the seat-writable tree. The launcher feeds the bounded bytes to the unit
    through its own stdin (systemd-run --pipe inherits it) and touches no
    path. Run in a child so the test's own stdin is untouched."""
    import os, subprocess, sys
    code = (
        "import importlib.util, os, sys\n"
        f"src = open({str(LAUNCH)!r}).read()\n"
        "ns = {'__name__': 'launcher_under_test'}\n"
        "exec(compile(src, 'launcher', 'exec'), ns)\n"
        "ns['prompt_on_stdin'](b'hello prompt\\n' * 3)\n"
        "sys.stdout.write(os.read(0, 1 << 16).decode())\n"
    )
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert r.stdout == "hello prompt\n" * 3


# ═══════════════════════════════ stage 3: publish, revert, remix (D6) ═══════
#
# SPECS/2026-09-09-apps-serving-gate.md D6. Three modes whose caller is the
# HOUSE SERVER, not the broker, and whose whole job is moving directories under
# /srv/apps-www as res-appsbuilding. Two halves, tested two ways:
#
#   the ROOT half   — shapes, pre-flight refusals, and the systemd-run argv,
#                     asserted through the real program under DRY_RUN like
#                     every other mode here;
#   the SEAT half   — the rotate itself, called as plain functions against a
#                     tmp tree. No privilege, no systemd, and the ORDER of the
#                     renames — the only part of this that can eat a deploy —
#                     is asserted directly.

import importlib.util
import shutil as _shutil
import stat
import subprocess as _subprocess
from importlib.machinery import SourceFileLoader

APP_A = "abc234567xyz"
APP_B = "zzz234567abc"
HAVE_RSYNC = _shutil.which("rsync")
HAVE_GIT = _shutil.which("git")
SUDOERS = CC_DIR.parent / "keyboard" / "92-disjorn-apps.sudoers"


@pytest.fixture(scope="module")
def launcher():
    """The launcher imported as a module (the script has no .py extension), so
    the seat half's functions can be called directly. Importing it runs no
    code: everything below `if __name__ == "__main__"` is a def."""
    loader = SourceFileLoader("disjorn_apps_launch", str(LAUNCH))
    spec = importlib.util.spec_from_loader("disjorn_apps_launch", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


@pytest.fixture()
def www(tmp_path):
    """A scratch /srv/apps-www with one app that has a preview."""
    root = tmp_path / "apps-www"
    preview = root / APP_A / "preview"
    (preview / "sub").mkdir(parents=True)
    (preview / "index.html").write_text("<h1>v1</h1>\n", encoding="utf-8")
    (preview / "sub" / "a.txt").write_text("one\n", encoding="utf-8")
    return root


@pytest.fixture()
def apps(tmp_path):
    """A scratch /srv/apps with one real git repo holding one commit."""
    root = tmp_path / "apps"
    repo = root / APP_A
    repo.mkdir(parents=True)
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(tmp_path),
           "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null"}
    for args in (["init", "-q", "-b", "main"],
                 ["config", "user.name", "apps-builder"],
                 ["config", "user.email", "apps-builder@disjorn.local"]):
        _subprocess.run(["git", "-C", str(repo), *args], check=True, env=env)
    (repo / "index.html").write_text("<h1>v1</h1>\n", encoding="utf-8")
    _subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True, env=env)
    _subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "turn 1"],
                    check=True, env=env)
    return root


def op(seat, *args, www_root=None, apps_root=None, systemctl=None, self_path=None):
    """A serving mode through the real program, dry-run, with the two /srv
    roots pointed at a scratch tree."""
    extra = {}
    if www_root is not None:
        extra["DISJORN_APPS_LAUNCH_WWW_ROOT"] = str(www_root)
    if apps_root is not None:
        extra["DISJORN_APPS_LAUNCH_APPS_ROOT"] = str(apps_root)
    if systemctl is not None:
        extra["DISJORN_APPS_LAUNCH_SYSTEMCTL"] = str(systemctl)
    extra["DISJORN_APPS_LAUNCH_SELF"] = str(
        self_path or "/usr/local/lib/disjorn/disjorn-apps-launch")
    return run(seat, *args, env_extra=extra)


def op_argv(seat, *args, **kw):
    proc = op(seat, *args, **kw)
    assert proc.returncode == 0, f"expected exit 0:\n{proc.stderr}"
    return json.loads(proc.stdout)


def op_refuse(seat, *args, **kw):
    proc = op(seat, *args, **kw)
    assert proc.returncode == 64, (
        f"expected exit 64, got {proc.returncode}\n{proc.stderr}{proc.stdout}")
    assert proc.stderr.startswith("disjorn-apps-launch: REFUSED:")
    # ONE sentence, which is what the server puts in front of the user.
    assert len(proc.stderr.strip().splitlines()) == 1
    assert proc.stdout.strip() == ""
    return proc


# ───────────────────────────────────────────── the argv, byte for byte ──────

def test_publish_argv_byte_for_byte(seat, www):
    """The argv IS the contract, exactly as it is for `run`. Note what is NOT
    in it: no path, no www root, no rsync, no program the caller named. The
    unit re-enters THIS file in a mode no sudoers line can say, as the seat."""
    assert op_argv(seat, "publish", APP_A, www_root=www) == [
        "/usr/bin/systemd-run",
        f"--unit=disjorn-apps-publish-{APP_A}",
        f"--description=Disjorn apps publish {APP_A}",
        "--uid=res-appsbuilding",
        "--gid=res-appsbuilding",
        "--collect",
        "--wait",
        "--pipe",
        "--quiet",
        "--property=RuntimeMaxSec=300",
        "--property=MemoryMax=1G",
        "--property=LimitFSIZE=268435456",
        "--property=LimitCORE=0",
        "--property=TasksMax=64",
        "--setenv=HOME=/home/res-appsbuilding",
        "--",
        "/usr/local/lib/disjorn/disjorn-apps-launch", "publish-as-seat", APP_A,
    ]


def test_revert_argv_byte_for_byte(seat, www, launcher):
    launcher.publish_app(www, APP_A, rsync_bin=HAVE_RSYNC or "rsync")
    launcher.publish_app(www, APP_A, rsync_bin=HAVE_RSYNC or "rsync")
    assert op_argv(seat, "revert", APP_A, www_root=www) == [
        "/usr/bin/systemd-run",
        f"--unit=disjorn-apps-revert-{APP_A}",
        f"--description=Disjorn apps revert {APP_A}",
        "--uid=res-appsbuilding",
        "--gid=res-appsbuilding",
        "--collect", "--wait", "--pipe", "--quiet",
        "--property=RuntimeMaxSec=300",
        "--property=MemoryMax=1G",
        "--property=LimitFSIZE=268435456",
        "--property=LimitCORE=0",
        "--property=TasksMax=64",
        "--setenv=HOME=/home/res-appsbuilding",
        "--",
        "/usr/local/lib/disjorn/disjorn-apps-launch", "revert-as-seat", APP_A,
    ]


@pytest.mark.skipif(not HAVE_RSYNC, reason="rsync is not installed")
def test_revert_argv_needs_a_real_publish_first(seat, www, launcher):
    """Belt and braces on the fixture above: the argv test would pass on an
    empty tree if the pre-flight were not real."""
    launcher.publish_app(www, APP_A, rsync_bin=HAVE_RSYNC)
    op_refuse(seat, "revert", APP_A, www_root=www)   # one deploy, no previous


def test_remix_argv_byte_for_byte(seat, www, apps):
    assert op_argv(seat, "remix", APP_A, APP_B, www_root=www, apps_root=apps) == [
        "/usr/bin/systemd-run",
        f"--unit=disjorn-apps-remix-{APP_A}-{APP_B}",
        f"--description=Disjorn apps remix {APP_A} to {APP_B}",
        "--uid=res-appsbuilding",
        "--gid=res-appsbuilding",
        "--collect", "--wait", "--pipe", "--quiet",
        "--property=RuntimeMaxSec=300",
        "--property=MemoryMax=1G",
        "--property=LimitFSIZE=268435456",
        "--property=LimitCORE=0",
        "--property=TasksMax=64",
        "--setenv=HOME=/home/res-appsbuilding",
        "--",
        "/usr/local/lib/disjorn/disjorn-apps-launch", "remix-as-seat",
        APP_A, APP_B,
    ]


def test_the_seat_uid_is_never_an_argument_for_a_serving_mode(seat, www, apps):
    for argv in (op_argv(seat, "publish", APP_A, www_root=www),
                 op_argv(seat, "remix", APP_A, APP_B, www_root=www, apps_root=apps)):
        assert "--uid=res-appsbuilding" in argv
        assert not any(a.startswith("--uid=") and a != "--uid=res-appsbuilding"
                       for a in argv)
        assert not any(a.startswith("--property=Exec") for a in argv)


# ─────────────────────────────────────────────── every bad shape refused ────

BAD_IDS = ["", "aaaaaaaaaaa", "aaaaaaaaaaaaa", "ABCDEFGHIJKL", "abcdefghijk0",
           "abcdefghijk1", "abcdefghij-k", "../../etc/pa", "aaaaaaaaaaa/",
           "aaaa aaaaaaa", "aaaaaaaaaaa\n", "--uid=0", "aaaaaaaaaaaa;id"]


@pytest.mark.parametrize("app_id", BAD_IDS)
@pytest.mark.parametrize("mode", ["publish", "revert"])
def test_one_id_modes_refuse_every_bad_id(seat, www, mode, app_id):
    op_refuse(seat, mode, app_id, www_root=www)


@pytest.mark.parametrize("app_id", BAD_IDS)
def test_remix_refuses_a_bad_id_on_either_side(seat, www, apps, app_id):
    op_refuse(seat, "remix", app_id, APP_B, www_root=www, apps_root=apps)
    op_refuse(seat, "remix", APP_A, app_id, www_root=www, apps_root=apps)


@pytest.mark.parametrize("mode", ["publish", "revert"])
@pytest.mark.parametrize("args", [
    [],                              # missing
    [APP_A, APP_B],                  # one too many
    [APP_A, "--uid=0"],              # a flag riding behind a good id
    [f"{APP_A} {APP_B}"],            # sudo joins with spaces; the helper does not
])
def test_one_id_modes_refuse_wrong_argument_counts(seat, www, mode, args):
    op_refuse(seat, mode, *args, www_root=www)


@pytest.mark.parametrize("args", [
    [],
    [APP_A],                         # one short
    [APP_A, APP_B, APP_A],           # one long
    [APP_A, APP_B, "--uid=0"],
    [f"{APP_A} {APP_B}"],
])
def test_remix_refuses_wrong_argument_counts(seat, www, apps, args):
    op_refuse(seat, "remix", *args, www_root=www, apps_root=apps)


def test_remix_refuses_a_self_remix(seat, www, apps):
    proc = op_refuse(seat, "remix", APP_A, APP_A, www_root=www, apps_root=apps)
    assert "same app" in proc.stderr


def test_the_as_seat_modes_are_not_reachable_through_sudo():
    """The regexes are anchored on the mode word, so `publish-as-seat` — the
    only mode that moves a file — cannot be named by any caller of the sudoers
    file. This is the whole reason the split is safe."""
    text = SUDOERS.read_text(encoding="utf-8")
    for mode in ("publish", "revert", "remix"):
        assert f"^{mode} " in text
        assert f"{mode}-as-seat" not in text.split("Cmnd_Alias")[-1]


@pytest.mark.skipif(not HAVE_RSYNC or os.geteuid() == 0,
                    reason="rsync missing, or running as root")
def test_the_as_seat_half_is_wired_end_to_end(seat, www):
    """The mode the transient unit actually runs, run as an ordinary uid: it
    does the work and prints one JSON line, which is what comes back through
    `--pipe` to the server. The rotate's own properties are asserted against
    the functions below; this is the wiring."""
    proc = op(seat, "publish-as-seat", APP_A, www_root=www)
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout) == {
        "mode": "publish", "app": APP_A,
        "live": str(www / APP_A / "live"), "previous": False}
    assert (www / APP_A / "live" / "index.html").exists()


@pytest.mark.parametrize("args", [[], [APP_A, APP_B], ["AAAAAAAAAAAA"]])
def test_the_as_seat_half_checks_the_shapes_again(seat, www, args):
    """A second fence, not the fence — the same discipline run-apps.sh follows
    when it re-asserts the charsets the launcher already checked."""
    proc = op(seat, "publish-as-seat", *args, www_root=www)
    assert proc.returncode == 64 and "REFUSED" in proc.stderr


def test_the_as_seat_half_refuses_to_be_root():
    """Root has no business moving directories in the seat's own trees, which
    is the ruling the whole serving half is built on. Asserted by reading the
    guard: this suite must never need root to run."""
    src = LAUNCH.read_text(encoding="utf-8")
    assert "if os.geteuid() == 0:" in src
    assert "never runs as root" in src


# ───────────────────────────────────────────── the pre-flight refusals ──────

def test_publish_refuses_an_app_with_no_preview(seat, tmp_path):
    empty = tmp_path / "empty-www"
    (empty / APP_A).mkdir(parents=True)
    proc = op_refuse(seat, "publish", APP_A, www_root=empty)
    assert "no preview" in proc.stderr


def test_publish_refuses_a_preview_that_is_a_symlink(seat, tmp_path, www):
    """rsync's --no-links drops links INSIDE a transfer; a source path that is
    itself a link is followed. `preview -> /etc` must not become `live`."""
    app = www / "aaaaaaaaaaaa"
    app.mkdir()
    (app / "preview").symlink_to(tmp_path)
    op_refuse(seat, "publish", "aaaaaaaaaaaa", www_root=www)


def test_revert_refuses_when_there_is_nothing_to_go_back_to(seat, www):
    proc = op_refuse(seat, "revert", APP_A, www_root=www)
    assert "not live" in proc.stderr


def test_remix_refuses_a_parent_that_is_not_a_repo(seat, www, apps):
    (apps / APP_B).mkdir()           # a directory, but no .git
    proc = op_refuse(seat, "remix", APP_B, "aaaaaaaaaaaa",
                     www_root=www, apps_root=apps)
    assert "no app repository" in proc.stderr


def test_remix_refuses_an_existing_child(seat, www, apps):
    (apps / APP_B).mkdir()
    proc = op_refuse(seat, "remix", APP_A, APP_B, www_root=www, apps_root=apps)
    assert "already exists" in proc.stderr


def _fake_systemctl(tmp_path, description):
    """A systemctl that reports one active apps unit with the given
    Description. Two subcommands, the two this program uses."""
    path = tmp_path / "fake-systemctl"
    path.write_text(
        "#!/bin/sh\n"
        'case "$1" in\n'
        '  list-units) echo "disjorn-apps-12-3.service loaded active running x";;\n'
        f'  show) echo {description!r};;\n'
        "esac\n", encoding="utf-8")
    path.chmod(0o755)
    return path


def test_publish_refuses_while_a_turn_of_that_app_is_running(seat, www, tmp_path):
    """A turn's own harvest publishes by renaming `preview` into place, so an
    rsync out of it mid-turn can read a tree that is about to stop existing.
    The unit name carries no app id — `stop` is handed the session and turn —
    so the id is read off the unit's Description, which build_run_argv sets."""
    fake = _fake_systemctl(tmp_path, f"Disjorn apps turn 12/3 ({APP_A})")
    proc = op_refuse(seat, "publish", APP_A, www_root=www, systemctl=fake)
    assert "has a turn running" in proc.stderr and "disjorn-apps-12-3" in proc.stderr


def test_a_turn_of_a_DIFFERENT_app_does_not_block_a_publish(seat, www, tmp_path):
    fake = _fake_systemctl(tmp_path, f"Disjorn apps turn 12/3 ({APP_B})")
    op_argv(seat, "publish", APP_A, www_root=www, systemctl=fake)


def test_systemd_not_answering_is_not_evidence_of_a_running_turn(seat, www, tmp_path):
    """Advisory, deliberately: a failed query warns and publishes. Guessing
    'yes' would mean a Live button that never works on a box where the query
    is broken, which is worse than the race it closes."""
    broken = tmp_path / "broken-systemctl"
    broken.write_text("#!/bin/sh\nexit 3\n", encoding="utf-8")
    broken.chmod(0o755)
    proc = op(seat, "publish", APP_A, www_root=www, systemctl=broken)
    assert proc.returncode == 0
    assert "cannot ask systemd" in proc.stderr


# ═══════════════════════════════ the seat half: the rotate itself ═══════════

@pytest.mark.skipif(not HAVE_RSYNC, reason="rsync is not installed")
def test_first_publish_creates_live_and_no_previous(www, launcher):
    out = launcher.publish_app(www, APP_A, rsync_bin=HAVE_RSYNC)
    live = www / APP_A / "live"
    assert out["previous"] is False
    assert (live / "index.html").read_text() == "<h1>v1</h1>\n"
    assert (live / "sub" / "a.txt").read_text() == "one\n"
    assert not (www / APP_A / "live.prev").exists()
    # The preview is a COPY source, not a move source: the owner goes on
    # building after pressing Live.
    assert (www / APP_A / "preview" / "index.html").exists()


@pytest.mark.skipif(not HAVE_RSYNC, reason="rsync is not installed")
def test_the_published_tree_carries_the_modes_the_gate_serves(www, launcher):
    launcher.publish_app(www, APP_A, rsync_bin=HAVE_RSYNC)
    live = www / APP_A / "live"
    assert stat.S_IMODE(live.stat().st_mode) == 0o755
    assert stat.S_IMODE((live / "sub").stat().st_mode) == 0o755
    assert stat.S_IMODE((live / "index.html").stat().st_mode) == 0o644
    # The staging root is nobody's business but the seat's.
    assert stat.S_IMODE((www / ".staging").stat().st_mode) == 0o700


@pytest.mark.skipif(not HAVE_RSYNC, reason="rsync is not installed")
def test_second_publish_rotates_the_old_live_into_live_prev(www, launcher):
    launcher.publish_app(www, APP_A, rsync_bin=HAVE_RSYNC)
    (www / APP_A / "preview" / "index.html").write_text("<h1>v2</h1>\n",
                                                        encoding="utf-8")
    out = launcher.publish_app(www, APP_A, rsync_bin=HAVE_RSYNC)
    assert out["previous"] is True
    assert (www / APP_A / "live" / "index.html").read_text() == "<h1>v2</h1>\n"
    assert (www / APP_A / "live.prev" / "index.html").read_text() == "<h1>v1</h1>\n"


@pytest.mark.skipif(not HAVE_RSYNC, reason="rsync is not installed")
def test_only_one_deploy_is_kept_behind(www, launcher):
    """`revert` reaches back exactly one, so the third publish drops the
    first. A www root that grew a copy per deploy would fill the disk."""
    for n in range(1, 4):
        (www / APP_A / "preview" / "index.html").write_text(
            f"<h1>v{n}</h1>\n", encoding="utf-8")
        launcher.publish_app(www, APP_A, rsync_bin=HAVE_RSYNC)
    assert (www / APP_A / "live" / "index.html").read_text() == "<h1>v3</h1>\n"
    assert (www / APP_A / "live.prev" / "index.html").read_text() == "<h1>v2</h1>\n"
    assert sorted(p.name for p in (www / APP_A).iterdir()) == [
        "live", "live.prev", "preview"]


@pytest.mark.skipif(not HAVE_RSYNC, reason="rsync is not installed")
def test_publish_leaves_nothing_in_the_staging_root(www, launcher):
    launcher.publish_app(www, APP_A, rsync_bin=HAVE_RSYNC)
    launcher.publish_app(www, APP_A, rsync_bin=HAVE_RSYNC)
    assert list((www / ".staging" / APP_A).iterdir()) == []


def test_publish_refuses_before_it_copies_anything(www, launcher, tmp_path):
    """The refusal is the FIRST thing, so a publish of an app with no preview
    leaves no staging tree behind to be cleaned up later."""
    with pytest.raises(SystemExit) as exc:
        launcher.publish_app(www, "aaaaaaaaaaaa", rsync_bin="/bin/false")
    assert exc.value.code == 64
    assert not (www / ".staging").exists()


def test_a_failed_rsync_never_touches_the_live_tree(www, launcher):
    """`live` is only ever replaced by a rename of a tree that copied
    completely. A broken copy is a failure (exit 1), not a refusal, and the
    tree that was serving is still serving."""
    (www / APP_A / "live").mkdir()
    (www / APP_A / "live" / "index.html").write_text("served\n", encoding="utf-8")
    with pytest.raises(SystemExit) as exc:
        launcher.publish_app(www, APP_A, rsync_bin="/bin/false")
    assert exc.value.code == 1
    assert (www / APP_A / "live" / "index.html").read_text() == "served\n"
    assert not (www / APP_A / "live.prev").exists()


def test_the_snapshot_argv_is_the_harvests_flags(launcher):
    """The publisher's flags, verbatim: symlinks dropped as a class, no
    devices or FIFOs, modes applied by rsync itself. A dropped flag here is a
    symlink or a 0600 file in a world-served tree."""
    argv = launcher.snapshot_argv("/srv/apps-www/x/preview",
                                  "/srv/apps-www/.staging/x/live.tmp.1",
                                  rsync_bin="/usr/bin/rsync")
    assert argv == ["/usr/bin/rsync", "-a", "--no-links", "--no-D",
                    "--chmod=D0755,F0644",
                    "/srv/apps-www/x/preview/",
                    "/srv/apps-www/.staging/x/live.tmp.1/"]


# ───────────────────────────────────────────────────────────── revert ───────

@pytest.mark.skipif(not HAVE_RSYNC, reason="rsync is not installed")
def test_revert_swaps_live_and_live_prev(www, launcher):
    launcher.publish_app(www, APP_A, rsync_bin=HAVE_RSYNC)
    (www / APP_A / "preview" / "index.html").write_text("<h1>v2</h1>\n",
                                                        encoding="utf-8")
    launcher.publish_app(www, APP_A, rsync_bin=HAVE_RSYNC)
    launcher.revert_app(www, APP_A)
    assert (www / APP_A / "live" / "index.html").read_text() == "<h1>v1</h1>\n"
    assert (www / APP_A / "live.prev" / "index.html").read_text() == "<h1>v2</h1>\n"


@pytest.mark.skipif(not HAVE_RSYNC, reason="rsync is not installed")
def test_a_revert_is_its_own_undo(www, launcher):
    """The swap is symmetric — the superseded tree is kept, not deleted — so a
    user who reverts by mistake presses it again."""
    launcher.publish_app(www, APP_A, rsync_bin=HAVE_RSYNC)
    (www / APP_A / "preview" / "index.html").write_text("<h1>v2</h1>\n",
                                                        encoding="utf-8")
    launcher.publish_app(www, APP_A, rsync_bin=HAVE_RSYNC)
    launcher.revert_app(www, APP_A)
    launcher.revert_app(www, APP_A)
    assert (www / APP_A / "live" / "index.html").read_text() == "<h1>v2</h1>\n"


@pytest.mark.skipif(not HAVE_RSYNC, reason="rsync is not installed")
def test_revert_leaves_nothing_in_the_staging_root(www, launcher):
    launcher.publish_app(www, APP_A, rsync_bin=HAVE_RSYNC)
    launcher.publish_app(www, APP_A, rsync_bin=HAVE_RSYNC)
    launcher.revert_app(www, APP_A)
    assert list((www / ".staging" / APP_A).iterdir()) == []


@pytest.mark.parametrize("shape", ["neither", "live only", "prev only"])
def test_revert_refuses_unless_both_trees_exist(www, launcher, shape):
    app = www / APP_A
    if shape in ("live only",):
        (app / "live").mkdir()
    if shape in ("prev only",):
        (app / "live.prev").mkdir()
    with pytest.raises(SystemExit) as exc:
        launcher.revert_app(www, APP_A)
    assert exc.value.code == 64


# ────────────────────────────────────────────────────────────── remix ───────

@pytest.mark.skipif(not (HAVE_GIT and HAVE_RSYNC), reason="git/rsync missing")
def test_remix_clones_the_repo_and_records_the_lineage(www, apps, launcher):
    out = launcher.remix_app(apps, www, APP_A, APP_B,
                             git_bin=HAVE_GIT, rsync_bin=HAVE_RSYNC)
    child = apps / APP_B
    assert out["child"] == APP_B and out["preview"] is True
    assert (child / "index.html").read_text() == "<h1>v1</h1>\n"
    log = _subprocess.run(["git", "-C", str(child), "log", "--format=%s|%an|%ae"],
                          capture_output=True, text=True, check=True).stdout
    lines = log.strip().splitlines()
    # The lineage is the FIRST thing in the log and the parent's history is
    # under it: `git log` in the child says where it came from.
    assert lines[0] == f"remix of {APP_A}|apps-builder|apps-builder@disjorn.local"
    assert lines[-1].startswith("turn 1|")


@pytest.mark.skipif(not (HAVE_GIT and HAVE_RSYNC), reason="git/rsync missing")
def test_remix_shares_no_objects_with_its_parent(www, apps, launcher):
    """--no-hardlinks. Two separately writable apps must not share a byte on
    disk: `git gc` in one would be reaching into the other's history."""
    launcher.remix_app(apps, www, APP_A, APP_B,
                       git_bin=HAVE_GIT, rsync_bin=HAVE_RSYNC)
    child = apps / APP_B
    assert not (child / ".git" / "objects" / "info" / "alternates").exists()
    for obj in (child / ".git" / "objects").rglob("*"):
        if obj.is_file():
            assert obj.stat().st_nlink == 1, f"{obj} is hardlinked to the parent"


@pytest.mark.skipif(not (HAVE_GIT and HAVE_RSYNC), reason="git/rsync missing")
def test_remix_gives_the_child_the_parents_preview(www, apps, launcher):
    launcher.remix_app(apps, www, APP_A, APP_B,
                       git_bin=HAVE_GIT, rsync_bin=HAVE_RSYNC)
    child_preview = www / APP_B / "preview"
    assert (child_preview / "index.html").read_text() == "<h1>v1</h1>\n"
    assert (child_preview / "sub" / "a.txt").read_text() == "one\n"
    assert stat.S_IMODE((www / APP_B).stat().st_mode) == 0o755
    assert stat.S_IMODE(child_preview.stat().st_mode) == 0o755
    # A remix is a build session, not a deploy: nothing is live yet.
    assert not (www / APP_B / "live").exists()


@pytest.mark.skipif(not HAVE_GIT, reason="git is not installed")
def test_remix_of_an_app_that_has_never_deployed_still_clones(www, apps, launcher):
    """No preview is not a refusal: the app exists, it just has nothing served
    yet. The child gets the repo and no preview root at all."""
    _shutil.rmtree(www / APP_A / "preview")
    out = launcher.remix_app(apps, www, APP_A, APP_B, git_bin=HAVE_GIT)
    assert out["preview"] is False
    assert (apps / APP_B / ".git").is_dir()
    assert not (www / APP_B).exists()


@pytest.mark.skipif(not HAVE_GIT, reason="git is not installed")
def test_the_child_repo_is_the_seats_own(www, apps, launcher):
    launcher.remix_app(apps, www, APP_A, APP_B,
                       git_bin=HAVE_GIT, rsync_bin=HAVE_RSYNC or "rsync")
    assert stat.S_IMODE((apps / APP_B).stat().st_mode) == 0o750


@pytest.mark.parametrize("bad", ["parent missing", "parent not a repo",
                                 "child exists", "same id"])
def test_remix_refusals(www, apps, launcher, bad):
    parent, child = APP_A, APP_B
    if bad == "parent missing":
        parent = "aaaaaaaaaaaa"
    if bad == "parent not a repo":
        parent = "aaaaaaaaaaaa"
        (apps / parent).mkdir()
    if bad == "child exists":
        (apps / child).mkdir()
    if bad == "same id":
        child = APP_A
    with pytest.raises(SystemExit) as exc:
        launcher.remix_app(apps, www, parent, child, git_bin=HAVE_GIT or "git")
    assert exc.value.code == 64
    assert not (apps / APP_B / ".git").exists()


# ───────────────────────────────── the constants that live in two files ─────

def test_the_lineage_commit_is_authored_by_the_app_builder(launcher):
    """apps_harvest writes every turn's commit and the launcher writes the
    remix's; the two identities are the same string in two files, which is
    this project's most expensive defect shape, so it is pinned."""
    loader = SourceFileLoader("apps_harvest_pin", str(CC_DIR / "apps" / "apps_harvest.py"))
    spec = importlib.util.spec_from_loader("apps_harvest_pin", loader)
    harvest = importlib.util.module_from_spec(spec)
    loader.exec_module(harvest)
    assert launcher.GIT_USER_NAME == harvest.GIT_USER_NAME
    assert launcher.GIT_USER_EMAIL == harvest.GIT_USER_EMAIL
    assert launcher.STAGING_NAME == harvest.STAGING_NAME


def test_the_sudoers_file_grants_exactly_the_five_modes():
    """One alias per mode, anchored at both ends, and the plink line names all
    five. A sixth alias, or a wildcard, is a diff someone has to defend."""
    text = SUDOERS.read_text(encoding="utf-8")
    aliases = dict(re.findall(r"^Cmnd_Alias (\w+) = \\\n\s*(\S.*)$", text, re.M))
    assert set(aliases) == {"DISJORN_APPS_LAUNCH", "DISJORN_APPS_STOP",
                            "DISJORN_APPS_PUBLISH", "DISJORN_APPS_REVERT",
                            "DISJORN_APPS_REMIX"}
    assert aliases["DISJORN_APPS_PUBLISH"].endswith("^publish [a-z2-7]{12}$")
    assert aliases["DISJORN_APPS_REVERT"].endswith("^revert [a-z2-7]{12}$")
    assert aliases["DISJORN_APPS_REMIX"].endswith(
        "^remix [a-z2-7]{12} [a-z2-7]{12}$")
    for name, rule in aliases.items():
        assert rule.startswith("/usr/local/lib/disjorn/disjorn-apps-launch ")
        assert "*" not in rule, f"{name} has a wildcard"
    grant = re.search(r"^plink ALL=\(root\) NOPASSWD: (.+?)$",
                      text.replace("\\\n", " "), re.M).group(1)
    assert set(x.strip() for x in grant.split(",")) == set(aliases)


def test_the_publisher_is_installed_and_drift_checked_by_the_keyboard_script():
    """The gate's unit is the fifth thing 10-appsbuilding.sh installs and the
    fifth thing its drift block compares. A file that is deployed but not
    drift-checked is a stale deploy waiting to happen."""
    script = (CC_DIR.parent / "keyboard" / "10-appsbuilding.sh").read_text(
        encoding="utf-8")
    assert "deploy/disjorn-apps-gate.service" in script
    assert "/etc/systemd/system/disjorn-apps-gate.service" in script
    assert "systemctl daemon-reload" in script
    assert 'if [ -f "$GATE_UNIT_SRC" ]; then' in script      # guarded both ways
