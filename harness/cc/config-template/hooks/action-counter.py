#!/usr/bin/env python3
"""PostToolUse hook (WP-H5) — one JSON line per tool call, for WP-H12.

Appends to /home/resident/.action-log (the resident's home volume, so it
survives container restarts and is readable from the host by plink/audit
tooling as /home/res-<name>/…/.action-log).

Line shape (stable contract for the WP-H12 budget/audit surface):
  {"ts": "<UTC ISO8601>", "session_id": "...", "tool_name": "...", "ok": bool}

`ok` is a coarse success signal derived from the tool_response; absence of
an error marker counts as success. Never blocks (always exits 0): counting
must not be able to break a session — enforcement is pre-tool-use.py's job.
"""

import datetime
import json
import os
import sys
from pathlib import Path

ACTION_LOG = Path(os.environ.get("HOME", "/home/resident")) / ".action-log"


def main() -> None:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        sys.exit(0)

    response = payload.get("tool_response")
    ok = True
    if isinstance(response, dict):
        ok = not (response.get("is_error") or response.get("error"))

    line = json.dumps({
        "ts": datetime.datetime.now(datetime.timezone.utc)
                               .strftime("%Y-%m-%dT%H:%M:%SZ"),
        "session_id": payload.get("session_id", ""),
        "tool_name": payload.get("tool_name", ""),
        "ok": ok,
    }, ensure_ascii=False)

    try:
        # O_APPEND: single-line writes are atomic enough for jsonl counting.
        with open(ACTION_LOG, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass  # counting must never kill the session

    sys.exit(0)


if __name__ == "__main__":
    main()
