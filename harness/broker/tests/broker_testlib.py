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

import gates  # noqa: E402
from brokerd import Broker, VerbError, load_config  # noqa: E402

PY = sys.executable
ALL_VERBS = [
    "restart-disjorn", "run-server-tests", "refresh-mirror", "start-build",
    "classify-diff", "read-prod-logs", "read-own-log", "read-metrics",
    "file-proposal", "query-own-audit",
    "board-list", "board-card", "board-search", "board-flag", "board-comment",
    "summon-hop", "apps-build", "build", "merge",
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
    #
    # argv[1] is a TEST control file (a JSON object, or absent): {tier, reasons,
    # protected_hits}. A red gate answers Tier 2 without being asked, the way
    # the real classifier fails closed.
    import argparse, json, os, sys
    control = {}
    if len(sys.argv) > 1 and os.path.exists(sys.argv[1]):
        control = json.loads(open(sys.argv[1]).read() or "{}")
    del sys.argv[1:2]
    p = argparse.ArgumentParser()
    p.add_argument("--repo", default=".")
    spec = p.add_mutually_exclusive_group(required=True)
    spec.add_argument("--range", dest="range_spec")
    spec.add_argument("--staged", action="store_true")
    p.add_argument("--config", required=True)
    p.add_argument("--gates", default="{}")
    ns = p.parse_args()
    gates = json.loads(ns.gates)
    tier = control.get("tier", 1)
    reasons = control.get("reasons", ["code diff within size cap, gates pass"])
    if any(v is False for v in gates.values()):
        tier, reasons = 2, ["gate failed: tests"]
    print(json.dumps({"tier": tier, "repo": ns.repo, "range": ns.range_spec,
                      "config": ns.config, "gates": gates, "reasons": reasons,
                      "protected_hits": control.get("protected_hits", []),
                      "stats": {"files": 1, "lines_added": 2,
                                "lines_removed": 0}}))
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

# 2026-08-13 publish path (SPECS/2026-08-13-build-publish-path.md item 3): the
# session commits and never pushes; run-build.sh harvests HOST-side after the
# container exits and prints one machine-readable line per entitled repo. THOSE
# LINES are what the reaper derives its banner from — the session's JSON report
# is enrichment now, not evidence. So every stub standing in for a SUCCESSFUL
# build has to print one, and a stub that prints only a report is a build whose
# harvest never reported (which the broker fails closed, on purpose — several
# tests exercise exactly that).
STUB_SHA = "a1b2c3d4e5f60718293a4b5c6d7e8f9012345678"
STUB_PUBLISHED = f"PUBLISHED disjorn.git {STUB_SHA}"
STUB_REPORT = json.dumps({"files": ["server/app/x.py"], "tests": "12 passed",
                          "diff": "+40 -2", "branch": "loop/stub"})


def build_out(report: str = '{"files": [], "tests": "ok", "diff": ""}',
              *, publish: str = STUB_PUBLISHED) -> bytes:
    """Canned build stdout: the wrapper's harvest line(s) after the session's
    report, in the order production prints them (the harvest runs once the
    container has exited). Pass publish="" for a build whose harvest never
    reported."""
    lines = [report] + ([publish] if publish else [])
    return ("\n".join(lines) + "\n").encode()


BUILD_STUB = textwrap.dedent(f"""\
    #!/usr/bin/env python3
    # Stub build session (stands in for run-build.sh + the headless CC build):
    # record the argv (after the record-file arg) AND the spec read from stdin,
    # print a JSON report like a real build session would, then print the
    # wrapper's post-exit harvest line, exit 0.
    import json, sys
    record = sys.argv[1]
    payload = sys.stdin.read()
    with open(record, "a") as fh:
        fh.write(json.dumps({{"argv": sys.argv[2:], "stdin": payload}}) + "\\n")
    print({STUB_REPORT!r})
    print({STUB_PUBLISHED!r})
""")

FLOOD_BUILD_STUB = textwrap.dedent(f"""\
    #!/usr/bin/env python3
    # BL-D2: a build session that FLOODS stdout (and stderr) before printing
    # its report — the shape that used to balloon the privileged broker's RSS
    # when output was piped. argv[1] = record file, argv[2] = MB to emit.
    #
    # It also prints a QUARANTINED line FIRST (provisioning, before the session
    # runs) and its harvest lines LAST, which is the real ordering — and the
    # reason the reaper reads the log's head as well as its tail: with 8MB in
    # between, a tail-only reaper would never see the quarantine notice.
    import json, sys
    record = sys.argv[1]
    megabytes = int(sys.argv[2])
    payload = sys.stdin.read()
    with open(record, "a") as fh:
        fh.write(json.dumps({{"argv": sys.argv[3:], "stdin": payload}}) + "\\n")
    print("QUARANTINED disjorn /home/res-gable/work/quarantine/disjorn-0813")
    chunk = "x" * 1024
    for _ in range(megabytes * 1024):
        sys.stdout.write(chunk + "\\n")
    sys.stderr.write("noise\\n" * 1000)
    print(json.dumps({{"files": ["big.py"], "tests": "1 passed", "diff": "+1 -0"}}))
    print({STUB_PUBLISHED!r})
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
                 stop_record: Path | None = None,
                 spec_repo: Path | None = None,
                 gatehouse: Path | None = None,
                 planroom_calls: list | None = None,
                 planroom_state: dict | None = None,
                 channel_posts: list | None = None,
                 message_db: Path | None = None,
                 classify_control: Path | None = None) -> None:
        # What the stubbed classifier is told to answer; see set_tier.
        self.classify_control = classify_control
        self._gate_work: Path | None = None
        self.gate_calls: list = []
        # Every post the broker made to a NAMED channel (the `/build` banner).
        self.channel_posts = channel_posts if channel_posts is not None else []
        self.message_db = message_db
        self.spec_repo = spec_repo
        self.gatehouse = gatehouse
        # Every /planroom call the broker made, and the fake board it talked to.
        self.planroom_calls = planroom_calls if planroom_calls is not None else []
        self.planroom_state = planroom_state if planroom_state is not None else {}
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

    # -- the server principal (`/build`) ----------------------------------
    def become_server(self, unit: str = "disjorn-test.service") -> None:
        """Make this process's uid resolve to `server`: map it to the identity
        the unit check sits behind, and answer the cgroup probe with `unit`."""
        self.broker.uid_map[os.getuid()] = "plink"
        self.broker.server_unit = unit
        self.broker._read_peer_cgroup = lambda pid: f"0::/system.slice/{unit}\n"

    def add_message(self, channel_id: int, seq: int, content: str, *,
                    author: str = "plink", author_type: str = "user",
                    flags: str = "{}", deleted: str | None = None) -> None:
        """One row in the scratch server DB the build verb reads."""
        import sqlite3
        assert self.message_db is not None
        db = sqlite3.connect(self.message_db)
        with db:
            db.execute("insert or ignore into users (id, username) values (?, ?)",
                       (abs(hash(author)) % 100000 + 1, author))
            uid_row = db.execute("select id from users where username = ?",
                                 (author,)).fetchone()
            db.execute("insert into messages (channel_id, seq, author_type, "
                       "author_id, content, privacy_flags, deleted_at) "
                       "values (?, ?, ?, ?, ?, ?, ?)",
                       (channel_id, seq, author_type,
                        uid_row[0] if author_type == "user" else 5,
                        content, flags, deleted))
        db.close()

    def build_ledger_lines(self) -> list[dict]:
        path = Path(self.broker.build_ledger)
        if not path.exists():
            return []
        return [json.loads(ln) for ln in path.read_text().splitlines()
                if ln.strip()]

    # -- the broker's own gate run (`merge`, and the end of a chat build) ---
    def stub_gates(self, *, tests: bool | None = True,
                   typecheck: bool | None = None, build: bool | None = None,
                   exit_code: int = 0, summary: str = "server 12 passed",
                   log_path: str = "/var/lib/disjorn-broker/gate-logs/x.log",
                   on_run=None) -> list[dict]:
        """Answer gates.run_gates without launching anything. Returns the list
        every call lands in, so a test can assert the argv prefix and the
        timeout the broker asked for. `on_run` runs WHILE the gates are up —
        the only window in which main can move under a merge."""
        calls: list[dict] = []

        def fake(argv_prefix, seat, slug, *, timeout, log_dir):
            calls.append({"argv_prefix": list(argv_prefix), "seat": seat,
                          "slug": slug, "timeout": timeout, "log_dir": log_dir})
            if on_run is not None:
                on_run()
            return gates.GateResult(tests, typecheck, build, exit_code,
                                    log_path, summary)

        gates.run_gates = fake
        self.gate_calls = calls
        return calls

    def finish_merges(self, timeout: float = 30) -> None:
        """Wait for every background `/merge`. Production never waits: the room
        hears the outcome as a post."""
        for thread in list(self.broker._merge_threads):
            thread.join(timeout=timeout)
            assert not thread.is_alive(), "a merge thread never finished"

    def merge_outcomes(self) -> list[list[str]]:
        """Every `merge: …` outcome post, split into its two lines."""
        return [c["body"].splitlines() for c in self.channel_posts
                if c["body"].startswith("merge: ")]

    def merge_denials(self) -> list[tuple[str, str]]:
        """(reason, message) for every merge the audit log records as denied."""
        out = []
        for entry in self.audit_lines():
            if entry["verb"] != "merge" or entry["allowed"] is not False:
                continue
            summary = entry["result_summary"][len("denied: "):]
            message, _, reason = summary.rpartition(" (")
            out.append((reason.rstrip(")"), message))
        return out

    # -- #custodian, where a reviewer's PASS lives ------------------------
    def add_custodian_post(self, seq: int, content: str, *,
                           author: str = "Claudette", created_at: str | None = None,
                           author_type: str = "bot",
                           channel_id: int | None = None) -> None:
        """One bot post in #custodian — the shape a PASS arrives in."""
        import sqlite3
        assert self.message_db is not None
        channel = (channel_id if channel_id is not None
                   else int(self.broker.disjorn["custodian_channel_id"]))
        db = sqlite3.connect(self.message_db)
        with db:
            db.execute("insert or ignore into bots (id, name) values (?, ?)",
                       (abs(hash(author)) % 100000 + 1, author))
            row = db.execute("select id from bots where name = ?",
                             (author,)).fetchone()
            db.execute("insert into messages (channel_id, seq, author_type, "
                       "author_id, content, privacy_flags, created_at) "
                       "values (?, ?, ?, ?, ?, '{}', ?)",
                       (channel, seq, author_type, row[0], content,
                        created_at or _utc_now()))
        db.close()

    # -- a REAL gatehouse ---------------------------------------------------
    def make_gatehouse(self) -> Path:
        """A real BARE repo at [gate].canonical_repo holding `main`, so the
        merge path is exercised against git and not against a mock."""
        assert self.gatehouse is not None
        bare = self.gatehouse
        _git_run(["git", "init", "-q", "--bare", "-b", "main", str(bare)])
        work = bare.parent / f"{bare.name}-seed"
        _git_run(["git", "clone", "-q", str(bare), str(work)])
        self._gate_work = work
        (work / "docs").mkdir(exist_ok=True)
        (work / "docs" / "a.md").write_text("one\n")
        _git(work, "add", "-A")
        _git(work, "commit", "-q", "-m", "init")
        _git(work, "push", "-q", "origin", "main")
        return bare

    def push_branch(self, slug: str, path: str = "docs/new.md",
                    content: str = "two\n") -> str:
        """One `loop/<slug>` branch off main, touching `path`. Returns its tip."""
        work = self._gate_work
        _git(work, "fetch", "-q", "origin")
        _git(work, "checkout", "-q", "-B", f"loop/{slug}", "origin/main")
        target = work / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
        _git(work, "add", "-A")
        _git(work, "commit", "-q", "-m", slug)
        tip = _git(work, "rev-parse", "HEAD").strip()
        _git(work, "push", "-q", "-f", "origin", f"loop/{slug}")
        _git(work, "checkout", "-q", "main")
        return tip

    def move_main(self, path: str = "docs/a.md", content: str = "ours\n") -> None:
        """Move main under a branch's feet. The default path is one every
        branch here also touches, which is the conflict the broker refuses."""
        work = self._gate_work
        _git(work, "checkout", "-q", "-B", "main", "origin/main")
        target = work / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
        _git(work, "add", "-A")
        _git(work, "commit", "-q", "-m", "main moves")
        _git(work, "push", "-q", "origin", "main")
        _git(work, "fetch", "-q", "origin")

    def main_subjects(self) -> list[str]:
        """Every commit subject on the gatehouse's main, newest first."""
        assert self.gatehouse is not None
        return _git(self.gatehouse, "log", "--format=%s", "main").splitlines()

    def commit_message(self, ref: str = "main") -> str:
        assert self.gatehouse is not None
        return _git(self.gatehouse, "log", "-1", "--format=%B", ref)

    def set_tier(self, tier: int, reasons: list[str] | None = None,
                 protected_hits: list[str] | None = None) -> None:
        """What the stubbed classifier answers for the next run."""
        assert self.classify_control is not None
        self.classify_control.write_text(json.dumps({
            "tier": tier,
            "reasons": reasons or [f"stub says tier {tier}", "second reason",
                                   "third reason"],
            "protected_hits": protected_hits or []}))

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
                   confirmed_by: str = "plink", seq="139",
                   body: str | None = None) -> str:
        """Write a spec into the configured SPECS/ dir (the mirror the gate
        reads) AND commit the same text to the canonical spec repo's main (the
        repo the broker stamps Status lines into); return its filename. Pass a
        draft status or a `<...>` placeholder confirmed_by/seq to exercise the
        confirm gate."""
        assert self.specs_dir is not None
        text = body if body is not None else SPEC_BODY.format(
            status=status, confirmed_by=confirmed_by, seq=seq)
        (self.specs_dir / filename).write_text(text)
        if self.spec_repo is not None:
            (self.spec_repo / "SPECS" / filename).write_text(text)
            self._git("add", "--", f"SPECS/{filename}")
            self._git("commit", "-q", "-m", f"spec {filename}")
        return filename

    # -- canonical spec repo (Status stamping) ----------------------------
    def _git(self, *args: str) -> str:
        assert self.spec_repo is not None
        import subprocess as _sp
        cp = _sp.run(["git", "-C", str(self.spec_repo), *args],
                     capture_output=True, text=True, check=True,
                     env={**os.environ, "GIT_AUTHOR_NAME": "t",
                          "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
                          "GIT_COMMITTER_EMAIL": "t@t"})
        return cp.stdout

    def spec_text_on_main(self, filename: str) -> str:
        """The spec as COMMITTED on the canonical repo's main — what the
        broker's stamp changes and what the mirror would fast-forward to."""
        return self._git("show", f"main:SPECS/{filename}")

    def spec_status_on_main(self, filename: str) -> str | None:
        from brokerd import parse_spec_status
        return parse_spec_status(self.spec_text_on_main(filename))

    def main_log(self) -> list[str]:
        return [ln for ln in self._git("log", "--format=%s", "main").splitlines()]

    # -- the local coverage log (spec 2026-08-27) --------------------------
    @property
    def local_log(self) -> Path:
        assert self.gatehouse is not None
        return self.gatehouse / "hooks" / "disjorn-local-log"

    def local_log_lines(self) -> list[str]:
        if not self.local_log.exists():
            return []
        return self.local_log.read_text(encoding="utf-8").splitlines()

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
        with a valid JSON report AND the wrapper's PUBLISHED line — a build with
        no publish line is a FAILED build now, so the default has to carry one
        or every test that just wants a successful build gets a failure."""
        if proc_factory is None:
            def proc_factory():
                return FakeBuildProc(out=build_out(
                    '{"files": ["a.py"], "tests": "ok", "diff": "+1 -0"}'))
        spawn = FakeBuildSpawn(proc_factory)
        self.broker._build_spawn = spawn
        return spawn


_GIT_ENV = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
            "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}


def _utc_now() -> str:
    import datetime as dt
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _git_run(argv: list[str]) -> str:
    import subprocess as sp
    cp = sp.run(argv, check=True, capture_output=True, text=True,
                env={**os.environ, **_GIT_ENV})
    return cp.stdout


def _git(repo: Path, *args: str) -> str:
    return _git_run(["git", "-C", str(repo), *args])


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
    classify_control = tmp_path / "classify.json"
    classify_control.write_text("{}")
    record_file = tmp_path / "record.jsonl"
    build_record = tmp_path / "build.jsonl"
    unit_state_file = tmp_path / "unit-state.json"
    stop_record = tmp_path / "stop.jsonl"
    specs_dir = tmp_path / "SPECS"
    specs_dir.mkdir()
    # The CANONICAL repo whose SPECS/ the mirror follows: a real git repo, so
    # the broker's Status stamps (plumbing commits on its main) are exercised
    # for real. specs_dir above stands in for the mirror the gate reads; the
    # refresh argvs that would fast-forward it are stubs (mirror.py).
    spec_repo = tmp_path / "canon"
    (spec_repo / "SPECS").mkdir(parents=True)
    import subprocess as _sp
    _env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
            "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    _sp.run(["git", "-C", str(spec_repo), "init", "-q", "-b", "main"],
            check=True, env=_env)
    (spec_repo / "SPECS" / "README.md").write_text("specs\n")
    _sp.run(["git", "-C", str(spec_repo), "add", "."], check=True, env=_env)
    _sp.run(["git", "-C", str(spec_repo), "commit", "-q", "-m", "init"],
            check=True, env=_env)
    # BL-D2: the detached build's stdout/stderr temp files. In production this
    # is the daemon's PrivateTmp; here it is a scratch dir so the tests can
    # assert they are created 0600 and REMOVED on every exit path.
    # The GATEHOUSE git-dir stand-in: `hooks/` is where the push log and its
    # sibling coverage log live. Deliberately NOT created here — the writer
    # has to make its own way, the way it will on a box where the log has
    # never been written before.
    gatehouse = tmp_path / "gatehouse.git"

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

    # The server DB the `build` verb reads its message out of: just the two
    # tables it joins, so no server import is needed to exercise the wire.
    message_db = tmp_path / "disjorn.db"
    import sqlite3 as _sq
    _db = _sq.connect(message_db)
    with _db:
        _db.execute("create table users (id integer primary key, "
                    "username text not null unique)")
        _db.execute("create table bots (id integer primary key, "
                    "name text not null unique)")
        _db.execute("create table messages (id integer primary key autoincrement, "
                    "channel_id integer not null, seq integer not null, "
                    "author_type text not null, author_id integer not null, "
                    "content text not null, privacy_flags text not null "
                    "default '{}', deleted_at text, created_at text)")
    _db.close()
    build_ledger = tmp_path / "build-ledger.jsonl"

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
        classify_diff = ["{PY}", "{stub_dir / 'classify.py'}", "{classify_control}"]
        refresh_mirror_fetch = ["{PY}", "{stub_dir / 'mirror.py'}", "{record_file}", "fetch", "origin"]
        refresh_mirror_update = ["{PY}", "{stub_dir / 'mirror.py'}", "{record_file}", "merge", "--ff-only", "origin/main"]
        refresh_mirror_head = ["{PY}", "{stub_dir / 'mirror.py'}", "{record_file}", "rev-parse", "--short", "HEAD"]

        [start_build]
        command = ["{PY}", "{stub_dir / 'build.py'}", "{build_record}"]
        session_argv = ["--output-format", "json"]
        model = "claude-opus-4-8"
        specs_dir = "{specs_dir}"
        spec_repo = "{spec_repo}"
        timeout_sec = 30
        daily_build_cap = 2
        stop_command = ["{PY}", "{stub_dir / 'buildstop.py'}", "{stop_record}", "{unit_state_file}", "stop"]
        unit_state_command = ["{PY}", "{stub_dir / 'unitstate.py'}", "{unit_state_file}"]

        [summon_hops]
        state_path = "{tmp_path / 'summon-hops.json'}"
        hop_cap = 8
        daily_hop_cap = 24

        [paths]
        metrics_json = "{metrics}"
        protected_paths = "{tmp_path / 'protected-paths.toml'}"

        [gate]
        # Only the two keys brokerd reads. The DETECTOR half of this block
        # (mirror, deploy_tree, the digest) is exercised in harness/metrics;
        # what is under test here is the WRITER: a Status stamp leaves a
        # local-stamp record naming the sha, because that commit never meets
        # the pre-receive hook and can never have a push-log line.
        canonical_repo = "{gatehouse}"
        message_db = "{message_db}"

        [disjorn]
        url = "http://127.0.0.1:1"
        api_key_path = "{tmp_path / 'no-key'}"
        custodian_channel_id = 3

        [server]
        unit = "disjorn-test.service"

        [planroom.lane_owners]
        "docs/" = "Claudette"
        "server/" = "Gable"

        [build]
        humans = ["plink"]
        seat = "test"
        ledger = "{build_ledger}"
        daily_build_cap = 2
        gate_timeout_sec = 1320
        gate_log_dir = "{tmp_path / 'gate-logs'}"
        merge_work_dir = "{tmp_path / 'merge-work'}"
    """))

    proposals: list = []

    def stub_transport(disjorn_cfg: dict, body: str) -> dict:
        proposals.append({"cfg": dict(disjorn_cfg), "body": body})
        return {"seq": 99, "message_id": 1234}

    planroom_calls: list = []
    # The server's answer about a build session. A chat build asks before it
    # launches; `sessions` overrides the default per session id.
    planroom_state: dict = {"session": {"open": True, "mode": "repo",
                                        "owner_username": "plink"},
                            "sessions": {},
                            "face": {"available": True,
                                     "derived_at": "2026-08-23T00:00:00+00:00",
                                     "mirror_head": "abc1234deadbeef",
                                     "deploy": {"badge": "green"}, "notes": []},
                            "cards": [], "comments": {}, "http_error": None}

    def stub_planroom(disjorn_cfg: dict, method: str, path: str,
                      payload: dict | None = None) -> dict:
        """A fake /planroom surface. Records every call, so tests can assert
        that a WRITE verb only ever hits a board-native endpoint."""
        planroom_calls.append({"method": method, "path": path,
                               "payload": payload, "cfg": dict(disjorn_cfg)})
        if planroom_state.get("http_error"):
            raise VerbError("exec-failure", planroom_state["http_error"])
        if path.endswith("/stage"):
            return {"ok": True}
        if path.endswith("/harness-view"):
            session = int(path.split("/")[3])
            view = planroom_state["sessions"].get(session)
            if view is None:
                view = planroom_state["session"]
            if view is False:
                raise VerbError("apps-refused", "no such build session",
                                status=404)
            return {"session_id": session, **view}
        cards = planroom_state["cards"]
        by_slug = {c["slug"]: c for c in cards}
        face = planroom_state["face"]
        if path.startswith("/planroom/board"):
            return {"face": face, "cards": cards,
                    "counts": {c["column"]: 1 for c in cards}}
        if path.startswith("/planroom/search"):
            return {"face": face, "cards": cards, "truncated": False}
        if path.startswith("/planroom/cards/"):
            rest = path[len("/planroom/cards/"):]
            slug = rest.split("/")[0]
            if path.endswith("/comment"):
                planroom_state["comments"].setdefault(slug, []).append(payload)
                return {"comment": {"id": 1, "slug": slug, "text": payload["text"],
                                    "author_label": payload.get("author")}}
            if path.endswith("/flag"):
                card = by_slug.get(slug, {"slug": slug, "column": "Ready"})
                card["blocked"] = bool(payload["blocked"])
                card["blocked_reason"] = payload.get("reason")
                return {"card": card}
            return {"face": face, "card": by_slug.get(slug),
                    "comments": planroom_state["comments"].get(slug, [])}
        raise VerbError("exec-failure", f"unstubbed plan room path {path}")

    channel_posts: list = []

    def stub_channel_transport(disjorn_cfg: dict, channel_id: int,
                               body: str) -> dict:
        channel_posts.append({"channel_id": channel_id, "body": body})
        return {"seq": 1, "message_id": 1}

    config = load_config(str(broker_toml))
    broker = Broker(config, str(verbs_path), transport=stub_transport,
                    planroom_api=stub_planroom,
                    channel_transport=stub_channel_transport)
    h = BrokerHarness(broker, verbs_path, record_file, proposals,
                      specs_dir=specs_dir, build_record=build_record,
                      build_log_dir=build_logs, stub_dir=stub_dir,
                      unit_state_file=unit_state_file, stop_record=stop_record,
                      spec_repo=spec_repo, gatehouse=gatehouse,
                      planroom_calls=planroom_calls,
                      planroom_state=planroom_state,
                      channel_posts=channel_posts, message_db=message_db,
                      classify_control=classify_control)
    h.set_verbs()  # everything explicitly OFF to start

    t = threading.Thread(target=broker.serve_forever, daemon=True)
    t.start()
    deadline = time.time() + 5
    while not os.path.exists(sock):
        if time.time() > deadline:
            raise RuntimeError("broker socket never appeared")
        time.sleep(0.01)
    real_run_gates = gates.run_gates
    yield h
    gates.run_gates = real_run_gates
    broker.shutdown()
    t.join(timeout=5)
