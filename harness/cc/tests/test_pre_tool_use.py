"""H13-D5 — pre-tool-use hook tripwire honesty.

The hook is NOT the wall (the broker's SO_PEERCRED + per-verb schema is),
so nothing here guards privilege. What these tests guard is that the
hook's docstring and its code agree: every form the docstring claims to
detect is detected, and every form it disclaims is exercised too, so a
future "fix" that silently narrows coverage fails loudly.

Two layers:
  * `_invokes_broker()` unit cases (the named H13-D5 bypass forms);
  * end-to-end: the hook run as a subprocess on real hook JSON, asserting
    exit 2 (block) / 0 (allow).
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from importlib.machinery import SourceFileLoader
from pathlib import Path

import pytest

CC_DIR = Path(__file__).resolve().parent.parent
HOOK = CC_DIR / "config-template" / "hooks" / "pre-tool-use.py"


@pytest.fixture(scope="module")
def hook():
    # Do NOT leave a __pycache__ in config-template/hooks: that directory is
    # copied verbatim to /config on install, and stale .pyc files have no
    # business riding into a resident's read-only policy mount.
    was = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        loader = SourceFileLoader("pre_tool_use_hook", str(HOOK))
        spec = importlib.util.spec_from_loader("pre_tool_use_hook", loader)
        mod = importlib.util.module_from_spec(spec)
        sys.modules["pre_tool_use_hook"] = mod
        loader.exec_module(mod)
    finally:
        sys.dont_write_bytecode = was
    return mod


def run_hook(payload: dict, tmp_path: Path, env_extra: dict | None = None):
    """Run the hook as Claude Code runs it. Returns CompletedProcess."""
    import os

    env = dict(os.environ)
    env["HOME"] = str(tmp_path)          # keep .action-log out of the real home
    env.update(env_extra or {})
    return subprocess.run(
        [sys.executable, str(HOOK)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        env=env,
    )


def bash(command: str) -> dict:
    return {"session_id": "t", "tool_name": "Bash",
            "tool_input": {"command": command}}


# ── the named H13-D5 bypass forms ────────────────────────────────────────
# Each of these evaded the original
#   r"(?:^|[;&|]\s*|\$\(\s*)broker(?:\s|$)"
# regex. They are the reason the deferred item exists.
BYPASS_FORMS = [
    ("leading-whitespace",   "   broker file-proposal --text x"),
    ("leading-tab",          "\tbroker file-proposal --text x"),
    ("newline-separated",    "cd /home/resident\nbroker file-proposal --text x"),
    ("newline-then-indent",  "cd /home/resident\n  broker read-own-log"),
    ("backtick",             "echo `broker read-own-log`"),
    ("absolute-path",        "/usr/local/bin/broker read-own-log"),
    ("relative-path",        "../../usr/local/bin/broker read-own-log"),
    ("eval-bare",            "eval broker read-own-log"),
    ("eval-double-quoted",   'eval "broker read-own-log"'),
    ("eval-single-quoted",   "eval 'broker read-own-log'"),
    ("env-assignment-prefix", "BROKER_DISABLE=0 broker read-own-log"),
    ("sh-dash-c",            "bash -c 'broker read-own-log'"),
    ("command-builtin",      "command broker read-own-log"),
    ("subshell-paren",       "( broker read-own-log )"),
    ("brace-group",          "{ broker read-own-log; }"),
    ("and-and",              "true && broker read-own-log"),
    ("or-or",                "false || broker read-own-log"),
]

# Forms the ORIGINAL regex already caught — kept so the rewrite cannot
# regress them.
ALREADY_CAUGHT = [
    ("plain",        "broker read-own-log"),
    ("semicolon",    "echo hi; broker read-own-log"),
    ("pipe",         "echo hi | broker read-own-log"),
    ("dollar-paren", "x=$(broker read-own-log)"),
]

# Things that must NOT trip the invocation heuristic.
NOT_INVOCATIONS = [
    ("python-innocent",   "python3 -c 1"),
    ("word-in-argument",  "echo broker"),
    ("grep-for-the-word", "grep -rn broker /home/resident/notes"),
    ("substring-name",    "brokerage --help"),
    ("path-not-basename", "ls /usr/local/bin/broker-cli"),
    ("comment-only",      "# broker read-own-log"),
]


@pytest.mark.parametrize("label,command", BYPASS_FORMS + ALREADY_CAUGHT,
                         ids=lambda v: v if isinstance(v, str) else "")
def test_invokes_broker_detects(hook, label, command):
    assert hook._invokes_broker(command), f"missed {label}: {command!r}"


@pytest.mark.parametrize("label,command", NOT_INVOCATIONS,
                         ids=lambda v: v if isinstance(v, str) else "")
def test_invokes_broker_no_false_positive(hook, label, command):
    assert not hook._invokes_broker(command), f"false positive {label}: {command!r}"


@pytest.mark.parametrize("label,command", BYPASS_FORMS,
                         ids=lambda v: v if isinstance(v, str) else "")
def test_bypass_form_blocked_end_to_end(tmp_path, label, command):
    """Each bypass form + chat markers must exit 2 through the real hook."""
    marked = command.replace("--text x", "--text [[CHAT]]do it[[/CHAT]]")
    if "[[CHAT]]" not in marked:
        marked = f"{command} --text [[CHAT]]do it[[/CHAT]]"
    proc = run_hook(bash(marked), tmp_path)
    assert proc.returncode == 2, f"{label} not blocked: {proc.stdout} {proc.stderr}"
    assert "chat is data" in proc.stderr


def test_innocent_command_still_allowed(tmp_path):
    assert run_hook(bash("python3 -c 1"), tmp_path).returncode == 0


def test_broker_call_without_chat_markers_allowed(tmp_path):
    """The honest path stays open — `broker` itself is not forbidden."""
    assert run_hook(bash("broker read-own-log --lines 5"), tmp_path).returncode == 0


# ── socket-path tripwire, incl. the BROKER_SOCKET env evasion ────────────

def test_socket_tokens_include_env_path(hook, monkeypatch):
    monkeypatch.setenv("BROKER_SOCKET", "/run/disjorn-broker/broker.sock")
    toks = hook.socket_tokens()
    assert "/run/disjorn-broker/broker.sock" in toks
    assert "/run/disjorn-broker" in toks   # dirname — catches glob probes
    assert "broker.sock" in toks
    assert "BROKER_SOCKET" in toks


def test_socket_tokens_survive_a_relocated_socket(hook, monkeypatch):
    """A deployment that moves the socket must still be covered."""
    monkeypatch.setenv("BROKER_SOCKET", "/tmp/alt/dsj.sock")
    toks = hook.socket_tokens()
    assert "/tmp/alt/dsj.sock" in toks and "/tmp/alt" in toks and "dsj.sock" in toks


def test_socket_tokens_with_env_unset(hook, monkeypatch):
    monkeypatch.delenv("BROKER_SOCKET", raising=False)
    assert hook.socket_tokens() == ("broker.sock", "BROKER_SOCKET")


SOCKET_EVASIONS = [
    ("literal-basename", "socat - UNIX:/run/disjorn-broker/broker.sock"),
    ("env-var-bare",     "socat - UNIX:$BROKER_SOCKET"),
    ("env-var-braced",   'socat - UNIX:"${BROKER_SOCKET}"'),
    ("dirname-glob",     "socat - UNIX:/run/disjorn-broker/*"),
    ("full-path",        "python3 -c \"import socket;s=socket.socket(1);s.connect('/run/disjorn-broker/broker.sock')\""),
]


@pytest.mark.parametrize("label,command", SOCKET_EVASIONS,
                         ids=lambda v: v if isinstance(v, str) else "")
def test_socket_evasions_blocked(tmp_path, label, command):
    proc = run_hook(bash(command), tmp_path,
                    {"BROKER_SOCKET": "/run/disjorn-broker/broker.sock"})
    assert proc.returncode == 2, f"{label} not blocked"
    assert "broker-socket" in proc.stderr


def test_socket_check_covers_non_bash_tools(tmp_path):
    """Rule 1 is tool-agnostic (rule 2 is Bash-only, by design)."""
    payload = {"session_id": "t", "tool_name": "Write",
               "tool_input": {"file_path": "/home/resident/x",
                              "content": "connect to $BROKER_SOCKET"}}
    proc = run_hook(payload, tmp_path,
                    {"BROKER_SOCKET": "/run/disjorn-broker/broker.sock"})
    assert proc.returncode == 2


# ── the disclaimed forms: assert the DOCSTRING is honest ─────────────────
# These are NOT detected. The test exists so that the docstring's
# "NOT detected" list stays true; if a future change starts catching one,
# this fails and whoever fixed it must also update the docstring claim.

DISCLAIMED = [
    ("name-reassembly",  'B=brok; K=er; "$B$K" read-own-log'),
    ("param-expansion",  'x=roker; "b${x}" read-own-log'),
    ("indirect-script",  "sh ./run-it.sh"),
    # Splitting only the BASENAME is caught (the dirname is a token);
    # splitting the dirname too is not.
    ("socket-path-split", 'D=/run/disjorn-brok; socat - UNIX:"${D}er/broker".sock'),
]


def test_socket_path_split_at_basename_is_still_caught(tmp_path):
    """The dirname token buys real coverage — pin it so it is not dropped."""
    proc = run_hook(bash('S=/run/disjorn-broker/broker; socat - UNIX:"$S".sock'),
                    tmp_path, {"BROKER_SOCKET": "/run/disjorn-broker/broker.sock"})
    assert proc.returncode == 2


@pytest.mark.parametrize("label,command", DISCLAIMED,
                         ids=lambda v: v if isinstance(v, str) else "")
def test_disclaimed_forms_are_genuinely_not_detected(hook, tmp_path, label, command):
    """Honesty test: the docstring says these get through — prove it."""
    tripped = hook._invokes_broker(command) or (
        run_hook(bash(command), tmp_path,
                 {"BROKER_SOCKET": "/run/disjorn-broker/broker.sock"}).returncode == 2
    )
    assert not tripped, (
        f"{label} is now detected — good, but the pre-tool-use.py docstring "
        "still lists it under 'NOT detected'. Update the docstring."
    )


def test_docstring_declares_its_limits():
    """The H13-D5 fix is half code, half honest prose. Guard the prose."""
    src = HOOK.read_text()
    assert "NOT detected" in src
    assert "not a parser" in src or "not a shell parser" in src
    assert "SO_PEERCRED" in src
