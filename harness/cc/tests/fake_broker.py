#!/usr/bin/env python3
"""Fake disjorn-broker for WP-H5 tests.

Serves PROTOCOL.md's transport on a unix socket: one connection = one
newline-terminated JSON request = one newline-terminated JSON response.
Canned per-verb results; no auth (tests exercise the CLIENT, the real
broker's SO_PEERCRED auth is covered by harness/broker/tests/).

Usage: fake_broker.py <socket-path> [--deny VERB=CODE ...]
    --deny read-metrics=verb-disabled   makes that verb return that error.

Runs until killed. Prints "READY" on stdout once listening.
"""

import argparse
import json
import os
import socket
import sys

CANNED = {
    "restart-disjorn": {"exit_code": 0, "output": "fake: restarted"},
    "run-server-tests": {"exit_code": 0, "summary": "148 passed in 0.01s"},
    "classify-diff": {"classification": {"tier": 1, "fake": True}},
    "read-prod-logs": {"lines": ["2026-07-19T00:00:00 fake journal line"]},
    "read-own-log": {"lines": ["fake log line 1", "fake log line 2"],
                     "path": "/home/resident/logs/fake.log"},
    "read-metrics": {"metrics": {"fake": True}},
    "file-proposal": {"posted": True, "seq": 1, "message_id": 1},
    "query-own-audit": {"entries": [], "count": 0, "truncated": False},
}


def handle(conn: socket.socket, denials: dict) -> None:
    buf = b""
    while not buf.endswith(b"\n") and len(buf) < 64 * 1024:
        chunk = conn.recv(65536)
        if not chunk:
            break
        buf += chunk
    try:
        req = json.loads(buf.decode("utf-8"))
        verb = req["verb"]
        assert isinstance(req.get("args", {}), dict)
    except Exception:
        resp = {"ok": False, "error": {"code": "bad-args",
                                       "message": "malformed request"}}
    else:
        if verb in denials:
            resp = {"ok": False, "error": {"code": denials[verb],
                                           "message": f"fake denial for {verb}"}}
        elif verb in CANNED:
            resp = {"ok": True, "verb": verb, "result": CANNED[verb]}
        else:
            resp = {"ok": False, "error": {"code": "unknown-verb",
                                           "message": f"no such verb: {verb}"}}
    conn.sendall((json.dumps(resp) + "\n").encode("utf-8"))
    conn.close()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("socket_path")
    ap.add_argument("--deny", action="append", default=[],
                    metavar="VERB=CODE")
    ns = ap.parse_args()
    denials = dict(d.split("=", 1) for d in ns.deny)

    if os.path.exists(ns.socket_path):
        os.unlink(ns.socket_path)
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(ns.socket_path)
    os.chmod(ns.socket_path, 0o666)  # test scaffolding only
    srv.listen(8)
    print("READY", flush=True)
    while True:
        conn, _ = srv.accept()
        try:
            handle(conn, denials)
        except (OSError, BrokenPipeError):
            pass


if __name__ == "__main__":
    main()
