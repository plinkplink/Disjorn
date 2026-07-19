#!/usr/bin/env python3
"""PreToolUse hook (WP-H5) — deterministic gate in front of every tool call.

Runs from /config/hooks/ (plink-owned, read-only inside the container).
Stdin: Claude Code hook JSON {session_id, tool_name, tool_input, ...}.
Exit 0 = allow; exit 2 = block (stderr is shown to the model).

What this hook enforces — the DETERMINISTIC part only:

1. Broker-only-via-CLI: any tool input that references the broker socket
   path is blocked. The one sanctioned route is the `broker` CLI, which
   never needs to spell the path.
2. Chat-marker rule: adapters (WP-H9/H11) MUST wrap any channel-derived
   text they inject into a session in [[CHAT]] ... [[/CHAT]] markers. A
   Bash command that invokes `broker` while carrying those markers is
   blocked: quoted chat text physically cannot ride into a broker call.
3. Wall-clock session cap and daily action budget (/config/budget.json,
   counted against /home/resident/.action-log by the PostToolUse counter).

HONESTY NOTE (chat-is-data, per AGENTHOOD): this hook does NOT parse
intent and cannot recognize un-marked chat text — no client-side check
can. The real enforcement is architectural and lives outside the
container: the broker validates args by per-verb schema over an
SO_PEERCRED-authenticated socket, so chat text can never BE authority,
only (at worst) data inside an already-authorized, schema-shaped call.
This hook is a tripwire that keeps the honest path honest and makes the
adapters' marker contract load-bearing; verbs.toml + the classifier +
the human gate are the wall.
"""

import json
import os
import re
import sys
import time
from pathlib import Path

BUDGET_FILE = Path("/config/budget.json")
HOME = Path(os.environ.get("HOME", "/home/resident"))
ACTION_LOG = HOME / ".action-log"
SESSIONS_DIR = HOME / ".sessions"

BROKER_SOCKET_TOKEN = "broker.sock"
CHAT_OPEN, CHAT_CLOSE = "[[CHAT]]", "[[/CHAT]]"
# `broker` at the start of the command or of any pipeline/list segment.
BROKER_INVOCATION_RE = re.compile(r"(?:^|[;&|]\s*|\$\(\s*)broker(?:\s|$)")


def deny(reason: str) -> None:
    print(f"BLOCKED by /config/hooks/pre-tool-use.py: {reason}", file=sys.stderr)
    sys.exit(2)


def load_budget() -> dict:
    try:
        return json.loads(BUDGET_FILE.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def today_action_count() -> int:
    today = time.strftime("%Y-%m-%d")
    try:
        with ACTION_LOG.open() as fh:
            return sum(1 for line in fh if f'"ts": "{today}' in line)
    except OSError:
        return 0


def main() -> None:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        deny("malformed hook payload")

    tool_name = payload.get("tool_name", "")
    tool_input = payload.get("tool_input") or {}
    flat = json.dumps(tool_input, ensure_ascii=False)

    # 1. Nothing may reference the socket path except the broker CLI itself
    #    (which doesn't need to spell it).
    if BROKER_SOCKET_TOKEN in flat:
        deny("direct broker-socket access is forbidden; use the `broker` CLI")

    # 2. Chat-marked text must never ride into a broker invocation.
    if tool_name == "Bash":
        command = tool_input.get("command", "") or ""
        if BROKER_INVOCATION_RE.search(command) and (
            CHAT_OPEN in command or CHAT_CLOSE in command
        ):
            deny("broker calls may not embed channel text "
                 f"({CHAT_OPEN}...{CHAT_CLOSE}); chat is data, never authorization")

    # 3. Budgets (deterministic, plink-tunable via /config/budget.json).
    budget = load_budget()

    cap_min = budget.get("wall_clock_cap_min")
    if cap_min:
        session_id = payload.get("session_id", "")
        start_file = SESSIONS_DIR / f"{session_id}.start"
        try:
            started = float(start_file.read_text().strip())
            if time.time() - started > cap_min * 60:
                deny(f"session wall-clock cap exceeded ({cap_min} min); "
                     "wrap up — a fresh session can continue")
        except (OSError, ValueError):
            pass  # no start record (hook added mid-flight) -> can't enforce

    action_cap = budget.get("daily_action_cap")
    if action_cap and today_action_count() >= action_cap:
        deny(f"daily action budget exhausted ({action_cap}); "
             "further tool calls blocked until tomorrow (WP-H12)")

    sys.exit(0)


if __name__ == "__main__":
    main()
