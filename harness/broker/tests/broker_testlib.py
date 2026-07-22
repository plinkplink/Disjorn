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

# WP-L4 open fork: the build now runs in a TRANSIENT SYSTEMD UNIT under the
# resident's uid, so the broker talks to systemd about it twice — reading the
# unit's state (unprivileged) and stopping it (through the sudo helper). Both
# are fixed argvs in [start_build], stubbed here: no test ever calls sudo,
# systemctl or systemd-run for real.
UNIT_STATE_STUB = textwrap.dedent("""\
    #!/usr/bin/env python3
    # Emulates `systemctl show --property=ActiveState --value <unit>`.
    # argv[1] = a JSON file mapping unit name -> state (missing = inactive,
    # which is exactly what systemd answers for a unit it has forgotten).
    import json, os, sys
    state_file = sys.argv[1]
    unit = sys.argv[-1]
    states = {}
    if os.path.exists(state_file):
        with open(state_file) as fh:
            states = json.load(fh)
    print(states.get(unit, "inactive"))
""")

BUILD_STOP_STUB = textwrap.dedent("""\
    #!/usr/bin/env python3
    # Stands in for `sudo -n disjorn-build-launch stop <resident> <slug>`:
    # record the argv AND flip the unit's state, the way a real stop would.
    import json, os, sys
    record, state_file = sys.argv[1], sys.argv[2]
    rest = sys.argv[3:]
    with open(record, "a") as fh:
        fh.write(json.dumps(rest) + "\\n")
    states = {}
    if os.path.exists(state_file):
        with open(state_file) as fh:
            states = json.load(fh)
    if len(rest) >= 3:
        states[f"disjorn-build-{rest[-1]}.service"] = "inactive"
    with open(state_file, "w") as fh:
        json.dump(states, fh)
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

FLOOD_BUILD_STUB = textwrap.dedent("""\
    #!/usr/bin/env python3
    # BL-D2: a build session that FLOODS stdout (and stderr) before printing
    # its report — the shape that used to balloon the privileged broker's RSS
    # when output was piped. argv[1] = record file, argv[2] = MB to emit.
    import json, sys
    record = sys.argv[1]
    megabytes = int(sys.argv[2])
    payload = sys.stdin.read()
    with open(record, "a") as fh:
        fh.write(json.dumps({"argv": sys.argv[3:], "stdin": payload}) + "\\n")
    chunk = "x" * 1024
    for _ in range(megabytes * 1024):
        sys.stdout.write(chunk + "\\n")
    sys.stderr.write("noise\\n" * 1000)
    print(json.dumps({"files": ["big.py"], "tests": "1 passed", "diff": "+1 -0"}))
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
    """A stand-in for the detached Popen (mock the exec).

    BL-D2 CONTRACT UPDATE (deliberate): the real build no longer pipes its
    output to the broker — stdout/stderr are files the broker opens and hands
    to the spawn, and `communicate()` therefore returns (None, None). This fake
    matches that: it WRITES its canned out/err into the handed-over files (its
    own dup'd descriptors, exactly as a real child would hold) and returns
    (None, None). Tests that used to read the return value now read the file
    tail through the broker, which is the code path production takes.

    communicate() still records the spec fed on stdin and honours `timeout`;
    returncode is settable to exercise the failed path. `block` gates
    communicate() on an event so a test can prove the verb returns BEFORE the
    build finishes (detachment)."""

    def __init__(self, out=b"", err=b"", rc=0, block=False, raise_timeout=False):
        self.pid = 4242
        self.returncode = rc
        self._out = out
        self._err = err
        self._raise_timeout = raise_timeout
        self.stdin_written = None
        self.killed = False
        self.out_fh = None
        self.err_fh = None
        self.release = threading.Event()
        if not block:
            self.release.set()

    def attach_logs(self, out_fh, err_fh):
        """Take our OWN descriptors for the broker's output files, the way a
        real forked child does — so the broker closing its copies right after
        spawn (it must not hold them) cannot break the build's writes."""
        self.out_fh = out_fh
        self.err_fh = err_fh

    def _emit(self):
        for fh, data in ((self.out_fh, self._out), (self.err_fh, self._err)):
            if fh is None:
                continue
            try:
                if data:
                    fh.write(data)
                fh.flush()
                fh.close()
            except ValueError:      # already closed on a second communicate()
                pass

    def communicate(self, input=None, timeout=None):
        import subprocess as _sp
        self.release.wait(timeout=10)
        if input is not None:
            self.stdin_written = input
        if self._raise_timeout:
            raise _sp.TimeoutExpired(cmd="build", timeout=timeout)
        self._emit()
        return None, None

    def kill(self):
        self.killed = True
        self.release.set()


class FakeBuildSpawn:
    """Injectable _build_spawn: records each argv and hands back a proc.
    Mirrors the real signature — the broker passes the build's stdout/stderr
    FILES as keyword args (BL-D2) — and dups them into the proc so the fake
    child owns its descriptors."""

    def __init__(self, proc_factory):
        self._factory = proc_factory
        self.calls: list[list[str]] = []
        self.procs: list = []
        self.log_paths: list[tuple[str, str]] = []
        # The handles the BROKER opened. It must close its copies right after
        # spawn (the child holds dups) — asserted by test.
        self.parent_handles: list[tuple] = []

    def __call__(self, argv, *, stdout, stderr):
        self.calls.append(list(argv))
        self.log_paths.append((_fh_path(stdout), _fh_path(stderr)))
        self.parent_handles.append((stdout, stderr))
        proc = self._factory()
        proc.attach_logs(os.fdopen(os.dup(stdout.fileno()), "wb"),
                         os.fdopen(os.dup(stderr.fileno()), "wb"))
        self.procs.append(proc)
        return proc


def _fh_path(fh) -> str:
    """The on-disk path behind an open file handle (for cleanup assertions)."""
    return os.readlink(f"/proc/self/fd/{fh.fileno()}")


class BrokerHarness:
    def __init__(self, broker: Broker, verbs_path: Path, record_file: Path,
                 proposals: list, specs_dir: Path | None = None,
                 build_record: Path | None = None,
                 build_log_dir: Path | None = None,
                 stub_dir: Path | None = None,
                 unit_state_file: Path | None = None,
                 stop_record: Path | None = None) -> None:
        self.broker = broker
        self.verbs_path = verbs_path
        self.record_file = record_file
        self.proposals = proposals
        self.specs_dir = specs_dir
        self.build_record = build_record
        self.build_log_dir = build_log_dir
        self.stub_dir = stub_dir
        self.unit_state_file = unit_state_file
        self.stop_record = stop_record

    # -- transient build unit (WP-L4 open fork) ---------------------------
    def set_unit_state(self, slug: str, state: str) -> None:
        """Drive what the stubbed `systemctl show` reports for a build's unit."""
        assert self.unit_state_file is not None
        states = {}
        if self.unit_state_file.exists():
            states = json.loads(self.unit_state_file.read_text())
        states[f"disjorn-build-{slug}.service"] = state
        self.unit_state_file.write_text(json.dumps(states))

    def stop_calls(self) -> list[list[str]]:
        """Every `disjorn-build-launch stop …` the broker made."""
        if self.stop_record is None or not self.stop_record.exists():
            return []
        return [json.loads(ln) for ln in
                self.stop_record.read_text().splitlines() if ln.strip()]

    def use_flood_build(self, megabytes: int = 8) -> None:
        """Point start-build at the FLOOD stub: a real detached subprocess that
        writes `megabytes` of stdout before its report (BL-D2)."""
        assert self.stub_dir is not None and self.build_record is not None
        self.broker.start_build["command"] = [
            PY, str(self.stub_dir / "flood.py"), str(self.build_record),
            str(megabytes)]

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

    def build_log_files(self) -> list[Path]:
        """Leftover build stdout/stderr temp files (BL-D2 cleanup assertions).
        Must be empty once every reaper has finished."""
        if self.build_log_dir is None:
            return []
        return sorted(self.build_log_dir.iterdir())

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
    _write_stub(stub_dir / "flood.py", FLOOD_BUILD_STUB)
    _write_stub(stub_dir / "unitstate.py", UNIT_STATE_STUB)
    _write_stub(stub_dir / "buildstop.py", BUILD_STOP_STUB)
    record_file = tmp_path / "record.jsonl"
    build_record = tmp_path / "build.jsonl"
    unit_state_file = tmp_path / "unit-state.json"
    stop_record = tmp_path / "stop.jsonl"
    specs_dir = tmp_path / "SPECS"
    specs_dir.mkdir()
    # BL-D2: the detached build's stdout/stderr temp files. In production this
    # is the daemon's PrivateTmp; here it is a scratch dir so the tests can
    # assert they are created 0600 and REMOVED on every exit path.
    build_logs = tmp_path / "build-logs"
    build_logs.mkdir()

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
        build_log_dir = "{build_logs}"

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
        stop_command = ["{PY}", "{stub_dir / 'buildstop.py'}", "{stop_record}", "{unit_state_file}", "stop"]
        unit_state_command = ["{PY}", "{stub_dir / 'unitstate.py'}", "{unit_state_file}"]

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
                      specs_dir=specs_dir, build_record=build_record,
                      build_log_dir=build_logs, stub_dir=stub_dir,
                      unit_state_file=unit_state_file, stop_record=stop_record)
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
