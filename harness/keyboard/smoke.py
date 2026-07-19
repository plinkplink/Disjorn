#!/usr/bin/env python3
"""smoke.py — one-shot broker smoke test (used in the keyboard session).

Sends a single request to the broker socket and prints the JSON response.

    python3 smoke.py                       # read-metrics as whoever you are
    python3 smoke.py --verb read-own-log --args '{"lines": 5}'
    python3 smoke.py --socket /tmp/x.sock

What "healthy" looks like right after install:
  * run as plink        -> {"ok": false, "error": {"code": "unknown-caller"...}}
    (plink's uid is deliberately NOT in the [uids] map — only residents call)
  * run as a resident   -> {"ok": false, "error": {"code": "verb-disabled"...}}
    (every switch ships OFF)
Both prove the full path: socket up, peer-cred auth, kill switches consulted,
and the attempt lands in /var/log/disjorn-broker/audit.jsonl either way.
"""
import argparse
import json
import socket

DEFAULT_SOCKET = "/run/disjorn-broker/broker.sock"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--socket", default=DEFAULT_SOCKET)
    ap.add_argument("--verb", default="read-metrics")
    ap.add_argument("--args", default="{}", help="JSON object of verb args")
    ns = ap.parse_args()

    req = {"verb": ns.verb, "args": json.loads(ns.args)}
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.settimeout(15)
        s.connect(ns.socket)
        s.sendall(json.dumps(req).encode() + b"\n")
        buf = b""
        while b"\n" not in buf:
            chunk = s.recv(4096)
            if not chunk:
                break
            buf += chunk
    resp = json.loads(buf.split(b"\n", 1)[0] or b"{}")
    print(json.dumps(resp, indent=2))
    return 0 if resp else 1


if __name__ == "__main__":
    raise SystemExit(main())
