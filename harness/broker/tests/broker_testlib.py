"""Fixtures for socket-level broker tests (WP-H3).

The broker runs in-process on a background thread, listening on a scratch
unix socket with scratch configs; tests connect as real socket clients, so
SO_PEERCRED auth, dispatch, audit and the verb handlers are all exercised
end-to-end. Every subprocess-backed verb points at a stub script (fixed argv
recorded to a file); the file-proposal transport is an injected stub.
Nothing here touches the real service, port 8399, or /etc.
"""

from __future__ import annotations

import json
import os
import socket
import sys
import textwrap
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from brokerd import Broker, load_config  # noqa: E402

PY = sys.executable
ALL_VERBS = [
    "restart-disjorn", "run-server-tests", "classify-diff", "read-prod-logs",
    "read-own-log", "read-metrics", "file-proposal", "query-own-audit",
]

RECORD_STUB = textwrap.dedent("""\
    #!/usr/bin/env python3
    # Test stub: append our argv (after the record-file arg) to the record
    # file, then behave like the named command.
    import json, sys
    record = sys.argv[1]
    with open(record, "a") as fh:
        fh.write(json.dumps(sys.argv[2:]) + "\\n")
    print("stub-ok")
""")

TESTS_STUB = textwrap.dedent("""\
    #!/usr/bin/env python3
    print("...........")
    print("148 passed in 0.01s")
""")

CLASSIFY_STUB = textwrap.dedent("""\
    #!/usr/bin/env python3
    # Emulates the WP-H4 contract: --repo/--range/--gates-json in, one JSON
    # object out on stdout.
    import argparse, json
    p = argparse.ArgumentParser()
    p.add_argument("--repo", required=True)
    p.add_argument("--range", required=True)
    p.add_argument("--gates-json", required=True)
    ns = p.parse_args()
    print(json.dumps({"tier": 1, "repo": ns.repo, "range": ns.range,
                      "gates": json.loads(ns.gates_json)}))
""")

JOURNAL_STUB = textwrap.dedent("""\
    #!/usr/bin/env python3
    # Emulates `journalctl ... -n N`.
    import sys
    args = sys.argv[1:]
    n = int(args[args.index("-n") + 1]) if "-n" in args else 10
    for i in range(n):
        print(f"2026-07-19T00:00:{i:02d} disjorn line {i}")
""")


class BrokerHarness:
    def __init__(self, broker: Broker, verbs_path: Path, record_file: Path,
                 proposals: list) -> None:
        self.broker = broker
        self.verbs_path = verbs_path
        self.record_file = record_file
        self.proposals = proposals

    # -- client side ------------------------------------------------------
    def call(self, verb, args=None, raw: str | None = None) -> dict:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(10)
            s.connect(self.broker.socket_path)
            if raw is not None:
                payload = raw.encode()
            else:
                req: dict = {"verb": verb}
                if args is not None:
                    req["args"] = args
                payload = json.dumps(req).encode()
            s.sendall(payload + b"\n")
            buf = b""
            while b"\n" not in buf:
                chunk = s.recv(4096)
                if not chunk:
                    break
                buf += chunk
            return json.loads(buf.split(b"\n", 1)[0])

    # -- config side ------------------------------------------------------
    def set_verbs(self, resident: str = "res-test", **flags: bool) -> None:
        """Rewrite verbs.toml: every verb explicit, default False."""
        lines = [f"[{resident}]"]
        for verb in ALL_VERBS:
            lines.append(f'"{verb}" = {str(flags.get(verb, False)).lower()}')
        self.verbs_path.write_text("\n".join(lines) + "\n")

    def enable_all(self, resident: str = "res-test") -> None:
        self.set_verbs(resident, **{v: True for v in ALL_VERBS})

    # -- inspection -------------------------------------------------------
    def audit_lines(self) -> list[dict]:
        path = Path(self.broker.audit_path)
        if not path.exists():
            return []
        return [json.loads(ln) for ln in path.read_text().splitlines() if ln.strip()]

    def recorded_argv(self) -> list[list[str]]:
        if not self.record_file.exists():
            return []
        return [json.loads(ln) for ln in self.record_file.read_text().splitlines()]


def _write_stub(path: Path, content: str) -> None:
    path.write_text(content)
    path.chmod(0o755)


@pytest.fixture()
def harness(tmp_path: Path):
    """A running broker on a scratch socket, current uid mapped to res-test."""
    stub_dir = tmp_path / "stubs"
    stub_dir.mkdir()
    _write_stub(stub_dir / "record.py", RECORD_STUB)
    _write_stub(stub_dir / "tests.py", TESTS_STUB)
    _write_stub(stub_dir / "classify.py", CLASSIFY_STUB)
    _write_stub(stub_dir / "journal.py", JOURNAL_STUB)
    record_file = tmp_path / "record.jsonl"

    own_log = tmp_path / "res-test.log"
    own_log.write_text("".join(
        f"line {i}" + (" ERROR boom" if i % 7 == 0 else "") + "\n"
        for i in range(300)))

    metrics = tmp_path / "metrics.json"
    metrics.write_text(json.dumps({"retrieval": {"hits": 42}, "spine": {"entries": 7}}))

    audit = tmp_path / "audit.jsonl"
    sock = tmp_path / "b.sock"
    verbs_path = tmp_path / "verbs.toml"

    broker_toml = tmp_path / "broker.toml"
    broker_toml.write_text(textwrap.dedent(f"""\
        [broker]
        socket_path = "{sock}"
        audit_log = "{audit}"

        [uids]
        "{os.getuid()}" = "res-test"

        [residents.res-test]
        log_path = "{own_log}"

        [residents.res-other]
        log_path = "{tmp_path / 'other.log'}"

        [commands]
        restart_disjorn = ["{PY}", "{stub_dir / 'record.py'}", "{record_file}"]
        run_server_tests = ["{PY}", "{stub_dir / 'tests.py'}"]
        run_server_tests_cwd = "{tmp_path}"
        read_prod_logs = ["{PY}", "{stub_dir / 'journal.py'}"]
        classify_diff = ["{PY}", "{stub_dir / 'classify.py'}"]

        [paths]
        metrics_json = "{metrics}"

        [disjorn]
        url = "http://127.0.0.1:1"
        api_key_path = "{tmp_path / 'no-key'}"
        custodian_channel_id = 3
    """))

    proposals: list = []

    def stub_transport(disjorn_cfg: dict, body: str) -> dict:
        proposals.append({"cfg": dict(disjorn_cfg), "body": body})
        return {"seq": 99, "message_id": 1234}

    config = load_config(str(broker_toml))
    broker = Broker(config, str(verbs_path), transport=stub_transport)
    h = BrokerHarness(broker, verbs_path, record_file, proposals)
    h.set_verbs()  # everything explicitly OFF to start

    t = threading.Thread(target=broker.serve_forever, daemon=True)
    t.start()
    deadline = time.time() + 5
    while not os.path.exists(sock):
        if time.time() > deadline:
            raise RuntimeError("broker socket never appeared")
        time.sleep(0.01)
    yield h
    broker.shutdown()
    t.join(timeout=5)
