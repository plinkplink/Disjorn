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
from pathlib import Path

import pytest

CC_DIR = Path(__file__).resolve().parent.parent
LAUNCH = CC_DIR / "apps" / "disjorn-apps-launch"
EXAMPLE_TOML = CC_DIR / "apps" / "launch.toml.example"

# The seat as the launcher would resolve it on the house box: uid, home, group.
FAKE_SEAT = "997:/home/res-appsbuilding:res-appsbuilding"
FAKE_UID = 997

GOOD = ("keyboard", "12", "3", "abc234567xyz")     # principal, session, turn, app


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
keyboard = "{prompts}"
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


def test_the_seat_uid_is_never_an_argument(seat):
    """--uid is res-appsbuilding whoever asked. The <resident> argument names a
    PROMPT DIRECTORY and nothing else (spec §C)."""
    for principal in ("keyboard", "res-gable"):
        argv = accept(seat, "run", principal, "12", "3", "abc234567xyz",
                      str(seat["prompt"]))
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


def test_stop_mode_does_not_exist(seat):
    """The build launcher has run|stop; this one has run only. A turn is
    bounded by RuntimeMaxSec, and killing one early is a keyboard act."""
    refuse(seat, "stop", *GOOD, str(seat["prompt"]))


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
    "res-gable", "res-claudette", "res-a", "res-" + "a" * 24, "keyboard",
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
    "KEYBOARD",               # the literal is exact, not case-folded
    "keyboard2",              # ... and not a prefix
    "keyboards",
    "-keyboard",
    "res-gable;id",
    "--uid=0",
])
def test_principal_refused(seat, principal):
    refuse(seat, "run", principal, "12", "3", "abc234567xyz", str(seat["prompt"]))


def test_principal_with_no_mapped_directory_is_refused(seat):
    """`res-claudette` is a perfectly legal principal and still has no entry in
    THIS table. The charset says the name is well formed; the table says
    whether it may launch. Both must pass — which is what makes deleting the
    `keyboard` line from launch.toml a complete removal of that principal."""
    refuse(seat, "run", "res-claudette", "12", "3", "abc234567xyz",
           str(seat["prompt"]))


# ───────────────────────────────────────────── charsets: session and turn ────

@pytest.mark.parametrize("session", ["1", "9", "12", "999999999"])
def test_session_accepted(seat, session):
    accept(seat, "run", "keyboard", session, "3", "abc234567xyz",
           str(seat["prompt"]))


@pytest.mark.parametrize("session", [
    "", "0", "01", "-1", "+1", "1.0", "1e3", "1234567890",   # 10 digits
    " 1", "1 ", "1\n2", "1;id", "abc", "0x10", "١٢",          # arabic-indic
])
def test_session_refused(seat, session):
    refuse(seat, "run", "keyboard", session, "3", "abc234567xyz",
           str(seat["prompt"]))


@pytest.mark.parametrize("turn", ["1", "9", "42", "9999"])
def test_turn_accepted(seat, turn):
    accept(seat, "run", "keyboard", "12", turn, "abc234567xyz",
           str(seat["prompt"]))


@pytest.mark.parametrize("turn", [
    "", "0", "007", "10000", "-1", "1.5", " 2", "2 ", "2;id", "two", "٣",
])
def test_turn_refused(seat, turn):
    refuse(seat, "run", "keyboard", "12", turn, "abc234567xyz",
           str(seat["prompt"]))


# ────────────────────────────────────────────────── charsets: the app id ─────

@pytest.mark.parametrize("app_id", [
    "aaaaaaaaaaaa", "abc234567xyz", "234567234567", "zzzzzzzzzzzz",
])
def test_app_id_accepted(seat, app_id):
    accept(seat, "run", "keyboard", "12", "3", app_id, str(seat["prompt"]))


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
    refuse(seat, "run", "keyboard", "12", "3", app_id, str(seat["prompt"]))


def test_app_id_is_the_only_thing_that_names_a_directory(seat):
    """The app id lands in three /srv paths and a podman --name. Its charset is
    the reason none of them can be talked into a traversal — asserted here by
    reading it back out of the argv the launcher built."""
    argv = accept(seat, "run", "keyboard", "12", "3", "abc234567xyz",
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


# ─────────────────────────────────────────────────── the config table itself ──

def test_missing_config_is_refused(seat, tmp_path):
    refuse(seat, "run", *GOOD, str(seat["prompt"]),
           config=tmp_path / "nope.toml")


def test_malformed_config_is_refused(seat, tmp_path):
    bad = tmp_path / "bad.toml"
    bad.write_text("[prompt_dirs\nkeyboard = ", encoding="utf-8")
    refuse(seat, "run", *GOOD, str(seat["prompt"]), config=bad)


def test_config_without_prompt_dirs_is_refused(seat, tmp_path):
    bad = tmp_path / "empty.toml"
    bad.write_text('[runner]\nmodel = "claude-opus-5"\n', encoding="utf-8")
    refuse(seat, "run", *GOOD, str(seat["prompt"]), config=bad)


def test_relative_mapped_directory_is_refused(seat, tmp_path):
    bad = tmp_path / "rel.toml"
    bad.write_text('[prompt_dirs]\nkeyboard = "apps-prompts"\n', encoding="utf-8")
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
    defaults, same three principals. Its prompt_dirs point at real house paths,
    so only the parse and the table shape are asserted here."""
    import tomllib
    data = tomllib.loads(EXAMPLE_TOML.read_text(encoding="utf-8"))
    assert set(data["prompt_dirs"]) == {"res-gable", "res-claudette", "keyboard"}
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
