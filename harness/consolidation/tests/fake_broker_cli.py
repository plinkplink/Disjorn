#!/usr/bin/env python3
"""A fake `broker` CLI for consolidation posting tests.

Mimics the real resident broker CLI's file-proposal surface: accepts
`file-proposal --text <body>` and records the call to $FAKE_BROKER_RECORD (one
JSON line per call). NEVER touches a socket. `--fail` makes it exit non-zero.

This is a test double for the subprocess path only; the real broker's socket,
auth, and #custodian posting are covered elsewhere (harness/broker, harness/cc).
"""

import argparse
import json
import os
import sys


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("verb")
    ap.add_argument("--text", required=True)
    ns = ap.parse_args()

    record = os.environ.get("FAKE_BROKER_RECORD")
    if record:
        with open(record, "a", encoding="utf-8") as f:
            f.write(json.dumps({"verb": ns.verb, "text": ns.text}) + "\n")

    if os.environ.get("FAKE_BROKER_FAIL"):
        sys.stderr.write("fake broker: verb-disabled\n")
        return 12
    sys.stdout.write(json.dumps({"ok": True, "verb": ns.verb, "result": {"posted": True}}) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
