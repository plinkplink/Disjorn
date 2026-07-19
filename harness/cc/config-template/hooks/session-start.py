#!/usr/bin/env python3
"""SessionStart hook (WP-H5) — kernel + budget status into context.

Stdout from a SessionStart hook is appended to the session's context, so
every session opens knowing (a) which kernel it is running on and (b) how
much budget is left today. Also records the session start timestamp that
pre-tool-use.py uses for the wall-clock cap.
"""

import hashlib
import json
import os
import sys
import time
from pathlib import Path

HOME = Path(os.environ.get("HOME", "/home/resident"))
KERNEL = HOME / ".claude" / "CLAUDE.md"          # assembled by WP-H7 loader
ACTION_LOG = HOME / ".action-log"
SESSIONS_DIR = HOME / ".sessions"
BUDGET_FILE = Path("/config/budget.json")


def main() -> None:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        payload = {}

    # Record session start for the wall-clock cap (best effort).
    session_id = payload.get("session_id") or "unknown"
    try:
        SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
        (SESSIONS_DIR / f"{session_id}.start").write_text(str(time.time()))
    except OSError:
        pass

    # Kernel status.
    if KERNEL.is_file():
        data = KERNEL.read_bytes()
        digest = hashlib.sha256(data).hexdigest()[:12]
        mtime = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(KERNEL.stat().st_mtime))
        kernel_line = (f"kernel: {KERNEL} sha256:{digest} "
                       f"({len(data)} bytes, assembled {mtime})")
    else:
        kernel_line = (f"kernel: NOT ASSEMBLED at {KERNEL} — running on the "
                       "/config/CLAUDE.md placeholder; spine loader (WP-H7) "
                       "has not run")

    # Budget status.
    try:
        budget = json.loads(BUDGET_FILE.read_text())
    except (OSError, json.JSONDecodeError):
        budget = {}
    cap = budget.get("daily_action_cap")
    wall = budget.get("wall_clock_cap_min")
    today = time.strftime("%Y-%m-%d")
    used = 0
    try:
        with ACTION_LOG.open() as fh:
            used = sum(1 for line in fh if f'"ts": "{today}' in line)
    except OSError:
        pass
    cap_str = str(cap) if cap else "unlimited (no cap set)"
    wall_str = f"{wall} min" if wall else "uncapped"

    print("[resident harness status]")
    print(kernel_line)
    print(f"actions today: {used} of {cap_str}; session wall-clock cap: {wall_str}")
    print("all tool calls are counted (~/.action-log) and broker calls are "
          "audit-logged outside the container; actions are public in #custodian.")
    sys.exit(0)


if __name__ == "__main__":
    main()
