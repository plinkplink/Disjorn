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
    "restart-disjorn", "run-server-tests", "refresh-mirror", "start-build",
    "classify-diff", "read-prod-logs", "read-own-log", "read-metrics",
    "file-proposal", "query-own-audit",
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
    # MIRRORS the REAL classify_diff.py argparse (WP-H4) — same flags, same
    # required-ness — so brokerd's argv is validated against the actual
    # contract, not an imagined one. (The original stub accepted a
    # --gates-json flag the real CLI never had, and omitted the required
    # --config: broker tests passed while every prod call died on argparse.
    # Keep this in lockstep with classify_diff.py main().)
    import argparse, json
    p = argparse.ArgumentParser()
    p.add_argument("--repo", default=".")
    spec = p.add_mutually_exclusive_group(required=True)
    spec.add_argument("--range", dest="range_spec")
    spec.add_argument("--staged", action="store_true")
    p.add_argument("--config", required=True)
    p.add_argument("--gates", default="{}")
    ns = p.parse_args()
    print(json.dumps({"tier": 1, "repo": ns.repo, "range": ns.range_spec,
                      "config": ns.config, "gates": json.loads(ns.gates)}))
""")

MIRROR_STUB = textwrap.dedent("""\
    #!/usr/bin/env python3
    # Stub for the refresh-mirror git argvs: record argv, answer like git.
    import json, sys
    record = sys.argv[1]
    with open(record, "a") as fh:
        fh.write(json.dumps(sys.argv[2:]) + "\\n")
    print("abc1234" if "rev-parse" in sys.argv else "stub-ok")
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

BUILD_STUB = textwrap.dedent("""\
    #!/usr/bin/env python3
    # Stub build session (stands in for run-build.sh + the headless CC build):
    # record the argv (after the record-file arg) AND the spec read from stdin,
    # then print a JSON report like a real build session would, exit 0.
    import json, sys
    record = sys.argv[1]
    payload = sys.stdin.read()
    with open(record, "a") as fh:
        fh.write(json.dumps({"argv": sys.argv[2:], "stdin": payload}) + "\\n")
    print(json.dumps({"files": ["server/app/x.py"], "tests": "12 passed",
                      "diff": "+40 -2", "branch": "loop/stub"}))
""")

# A minimal spec matching TEMPLATE.md's parseable structure. Callers override
# status / confirmed_by / seq to exercise the confirm gate (pass a `<...>`
# placeholder or a draft status to simulate an unconfirmed spec).
SPEC_BODY = textwrap.dedent("""\
    # Spec: test build

    ## Request
    - **Verbatim**: do the thing
    - **Requester**: usrda
    - **Origin**: #custodian / seq 100

    ## Agreed UX
    A thing happens.

    ## Confirm record
    - **Confirmed by**: {confirmed_by}
    - **#custodian seq**: {seq}
    - **Confirmed at**: 2026-07-21T12:00:00Z

    ## Status
    `{status}`
""")


class FakeBuildProc:
    """A stand-in for the detached Popen (mock the exec). communicate() records
    the spec fed on stdin and returns canned (out, err); returncode is settable
    to exercise the failed path. `block` gates communicate() on an event so a
    test can prove the verb returns BEFORE the build finishes (detachment)."""

    def __init__(self, out=b"", err=b"", rc=0, block=False, raise_timeout=False):
        self.pid = 4242
        self.returncode = rc
        self._out = out
        self._err = err
        self._raise_timeout = raise_timeout
        self.stdin_written = None
        self.killed = False
        self.release = threading.Event()
        if not block:
            self.release.set()

    def communicate(self, input=None, timeout=None):
        import subprocess as _sp
        self.release.wait(timeout=10)
        self.stdin_written = input
        if self._raise_timeout:
            raise _sp.TimeoutExpired(cmd="build", timeout=timeout)
        return self._out, self._err

    def kill(self):
        self.killed = True
        self.release.set()


class FakeBuildSpawn:
    """Injectable _build_spawn: records each argv and hands back a proc."""

    def __init__(self, proc_factory):
        self._factory = proc_factory
        self.calls: list[list[str]] = []
        self.procs: list = []

    def __call__(self, argv):
        self.calls.append(list(argv))
        proc = self._factory()
        self.procs.append(proc)
        return proc


class BrokerHarness:
    def __init__(self, broker: Broker, verbs_path: Path, record_file: Path,
                 proposals: list, specs_dir: Path | None = None,
                 build_record: Path | None = None) -> None:
        self.broker = broker
        self.verbs_path = verbs_path
        self.record_file = record_file
        self.proposals = proposals
        self.specs_dir = specs_dir
        self.build_record = build_record

    # -- client side ------------------------------------------------------
    def _connect(self) -> socket.socket:
        # The socket FILE appears at bind(), before listen() — a fast test can
        # land in that gap and get ConnectionRefusedError. Retry connects only;
        # no probe connections (they would pollute audit-completeness asserts).
        deadline = time.time() + 5
        while True:
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s.settimeout(10)
            try:
                s.connect(self.broker.socket_path)
                return s
            except (ConnectionRefusedError, FileNotFoundError):
                s.close()
                if time.time() > deadline:
                    raise
                time.sleep(0.02)

    def call(self, verb, args=None, raw: str | None = None) -> dict:
        with self._connect() as s:
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

    # -- start-build helpers ---------------------------------------------
    def write_spec(self, filename: str, *, status: str = "confirmed",
                   confirmed_by: str = "plink", seq="139") -> str:
        """Write a spec into the configured SPECS/ dir; return its filename.
        Pass a draft status or a `<...>` placeholder confirmed_by/seq to
        exercise the confirm gate."""
        assert self.specs_dir is not None
        (self.specs_dir / filename).write_text(
            SPEC_BODY.format(status=status, confirmed_by=confirmed_by, seq=seq))
        return filename

    def build_records(self) -> list[dict]:
        """The {argv, stdin} records the real build stub wrote."""
        if self.build_record is None or not self.build_record.exists():
            return []
        return [json.loads(ln) for ln in self.build_record.read_text().splitlines()]

    def use_fake_build(self, proc_factory=None) -> FakeBuildSpawn:
        """Swap in an injectable _build_spawn (mock the exec) and return it for
        inspection. Default factory yields a clean, immediately-returning proc
        with a valid JSON report."""
        if proc_factory is None:
            def proc_factory():
                return FakeBuildProc(
                    out=b'{"files": ["a.py"], "tests": "ok", "diff": "+1 -0"}')
        spawn = FakeBuildSpawn(proc_factory)
        self.broker._build_spawn = spawn
        return spawn


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
    _write_stub(stub_dir / "mirror.py", MIRROR_STUB)
    _write_stub(stub_dir / "journal.py", JOURNAL_STUB)
    _write_stub(stub_dir / "build.py", BUILD_STUB)
    record_file = tmp_path / "record.jsonl"
    build_record = tmp_path / "build.jsonl"
    specs_dir = tmp_path / "SPECS"
    specs_dir.mkdir()

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

        [residents.res-test.path_map]
        "/opt/disjorn" = "{tmp_path / 'mirror'}"
        "/home/plink" = "/home/plink"

        [residents.res-other]
        log_path = "{tmp_path / 'other.log'}"

        [commands]
        restart_disjorn = ["{PY}", "{stub_dir / 'record.py'}", "{record_file}"]
        run_server_tests = ["{PY}", "{stub_dir / 'tests.py'}"]
        run_server_tests_cwd = "{tmp_path}"
        read_prod_logs = ["{PY}", "{stub_dir / 'journal.py'}"]
        classify_diff = ["{PY}", "{stub_dir / 'classify.py'}"]
        refresh_mirror_fetch = ["{PY}", "{stub_dir / 'mirror.py'}", "{record_file}", "fetch", "origin"]
        refresh_mirror_update = ["{PY}", "{stub_dir / 'mirror.py'}", "{record_file}", "merge", "--ff-only", "origin/main"]
        refresh_mirror_head = ["{PY}", "{stub_dir / 'mirror.py'}", "{record_file}", "rev-parse", "--short", "HEAD"]

        [start_build]
        command = ["{PY}", "{stub_dir / 'build.py'}", "{build_record}"]
        resident = "gable"
        session_argv = ["--output-format", "json"]
        model = "claude-opus-4-8"
        specs_dir = "{specs_dir}"
        timeout_sec = 30
        daily_build_cap = 2

        [paths]
        metrics_json = "{metrics}"
        protected_paths = "{tmp_path / 'protected-paths.toml'}"

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
    h = BrokerHarness(broker, verbs_path, record_file, proposals,
                      specs_dir=specs_dir, build_record=build_record)
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
