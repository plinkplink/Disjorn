#!/usr/bin/env python3
"""wake.py — wake a resident with a task (SPECS/2026-08-25-agentic-residents.md).

    wake.py res-gable "read the digest and open a card for what looks wrong"
    wake.py res-gable --task-file /tmp/task.md

RUN THIS AS PLINK, from the keyboard, and nowhere else. The wake is a broker
verb whose caller is authenticated by SO_PEERCRED, so the uid this process runs
under IS the authorization: the broker maps it through `[uids]`, refuses the
verb to every resident identity, and refuses this identity every other verb.
There is no token to pass and none to leak. `sudo -u res-gable wake.py` does
not work, and that is the whole design — nothing self-wakes.

It speaks harness/broker/PROTOCOL.md directly (one connection, one JSON line,
one response) rather than going through harness/cc/broker-cli/broker: that CLI
is the RESIDENT seat's surface, generated from the seat sections of verbs.toml,
and `wake` is deliberately in none of them.

What happens after this returns: nothing, here. The broker records the wake;
the seat's own runner picks it up, runs one headless session under the seat's
uid, and posts the result to #custodian. This command prints the wake id —
which is the string to grep in #custodian, in the broker audit log, and in the
seat's action log if the post never arrives.

Exit codes: 0 ok, 2 usage, 3 transport, 4 the broker refused.
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import sys

DEFAULT_SOCKET = "/run/disjorn-broker/broker.sock"
TIMEOUT_SECONDS = 20
MAX_REQUEST_BYTES = 64 * 1024

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_TRANSPORT = 3
EXIT_REFUSED = 4


def call_broker(socket_path: str, request: dict) -> dict:
    payload = (json.dumps(request, ensure_ascii=False) + "\n").encode("utf-8")
    if len(payload) > MAX_REQUEST_BYTES:
        raise ValueError(f"request exceeds {MAX_REQUEST_BYTES} bytes")
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(TIMEOUT_SECONDS)
        sock.connect(socket_path)
        sock.sendall(payload)
        chunks: list[bytes] = []
        while True:
            chunk = sock.recv(65536)
            if not chunk:
                break
            chunks.append(chunk)
            if chunk.endswith(b"\n"):
                break
    raw = b"".join(chunks)
    if not raw:
        raise ConnectionError("broker closed the connection without a response")
    return json.loads(raw.decode("utf-8"))


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Wake a Disjorn resident with one task (run as plink).")
    p.add_argument("resident", help="the seat to wake, e.g. res-gable")
    p.add_argument("task", nargs="?", default=None,
                   help="the work, in plain words")
    p.add_argument("--task-file", default=None,
                   help="read the task from a file instead (- for stdin)")
    p.add_argument("--socket", default=None,
                   help=f"broker socket (default {DEFAULT_SOCKET}, "
                        "or $BROKER_SOCKET)")
    p.add_argument("--json", action="store_true",
                   help="print the broker's raw response instead of a summary")
    return p


def read_task(ns: argparse.Namespace) -> str:
    if ns.task and ns.task_file:
        raise ValueError("give a task or --task-file, not both")
    if ns.task_file == "-":
        return sys.stdin.read()
    if ns.task_file:
        with open(ns.task_file, "r", encoding="utf-8") as fh:
            return fh.read()
    if ns.task:
        return ns.task
    raise ValueError("no task: a wake names the work it is for")


def main(argv: "list[str] | None" = None, environ: dict = os.environ) -> int:
    ns = build_parser().parse_args(argv)
    try:
        task = read_task(ns)
    except (ValueError, OSError) as exc:
        print(f"wake: {exc}", file=sys.stderr)
        return EXIT_USAGE

    socket_path = ns.socket or environ.get("BROKER_SOCKET", DEFAULT_SOCKET)
    request = {"verb": "wake",
               "args": {"resident": ns.resident, "task": task}}
    try:
        response = call_broker(socket_path, request)
    except (OSError, ValueError, ConnectionError, json.JSONDecodeError) as exc:
        print(f"wake: {type(exc).__name__}: {exc} (socket: {socket_path})",
              file=sys.stderr)
        return EXIT_TRANSPORT

    if ns.json:
        print(json.dumps(response, indent=2, ensure_ascii=False))
    if response.get("ok") is not True:
        err = response.get("error") or {}
        if not ns.json:
            print(f"wake refused [{err.get('code')}]: {err.get('message')}",
                  file=sys.stderr)
        return EXIT_REFUSED
    result = response.get("result") or {}
    if not ns.json:
        print(f"{result.get('wake_id')} -> {result.get('resident')} "
              f"(cap {result.get('session_cap_sec')}s). "
              "The seat posts the result in #custodian when it exits.")
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
