"""start-build verb + detached build session (WP-L4).

The MVP's long pole: a broker verb that validates a confirmed spec, enforces a
race-safe per-day build budget, and launches a DETACHED build to loop/<slug>
that merges/pushes/touches-prod nothing. Narration is state-transition-driven
(started / done / failed) — never timer-driven.

Path confinement, the confirm gate, budget (incl. the race guard), ships-OFF,
config-pure argv, narration shapes, and the detachment contract (mock the exec)
are all covered here. Everything runs over the real socket via the shared
harness; the exec is either the real build stub or an injected fake.
"""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path

import pytest

from broker_testlib import PY, FakeBuildProc, BrokerHarness
from brokerd import (
    MAX_BUILD_LOG_TAIL,
    Broker,
    _parse_build_report,
    format_build_done,
    format_build_failed,
    format_build_started,
    parse_confirm_record,
    parse_spec_status,
    slug_from_spec_filename,
)


# ---------------------------------------------------------------- ships OFF

def test_start_build_off_by_default(harness):
    harness.write_spec("2026-07-21-gif-picker.md")
    spawn = harness.use_fake_build()
    resp = harness.call("start-build", {"spec": "2026-07-21-gif-picker.md"})
    assert resp["ok"] is False
    assert resp["error"]["code"] == "verb-disabled"
    assert spawn.calls == []          # never launched
    assert harness.audit_lines()[-1]["allowed"] is False


# ------------------------------------------------------ path confinement

@pytest.mark.parametrize("spec", [
    "../../etc/passwd.md",
    "/etc/passwd.md",
    "../SPECS/x.md",
    "-oops.md",
    "sub/dir/x.md",          # not directly in SPECS/
    "notaspec.txt",          # missing under SPECS/ and not .md
])
def test_start_build_rejects_path_traversal(harness, spec):
    harness.set_verbs(**{"start-build": True})
    spawn = harness.use_fake_build()
    resp = harness.call("start-build", {"spec": spec})
    assert resp["ok"] is False, spec
    assert resp["error"]["code"] == "bad-args", spec
    assert spawn.calls == []
    assert harness.audit_lines()[-1]["allowed"] is False


def test_start_build_rejects_symlink_escape(harness, tmp_path):
    """A symlink planted in SPECS/ pointing outside must not be followed:
    realpath resolves the link, and the resolved file is not under SPECS/."""
    harness.set_verbs(**{"start-build": True})
    spawn = harness.use_fake_build()
    outside = tmp_path / "outside-2026-07-21-evil.md"
    outside.write_text("# not a spec\n## Status\n`confirmed`\n")
    link = harness.specs_dir / "2026-07-21-evil.md"
    os.symlink(outside, link)
    resp = harness.call("start-build", {"spec": "2026-07-21-evil.md"})
    assert resp["ok"] is False
    assert resp["error"]["code"] == "bad-args"
    assert spawn.calls == []


# ------------------------------------------------------------ confirm gate

def test_start_build_rejects_unconfirmed_spec(harness):
    """status draft -> no build, however filled the confirm record is."""
    harness.set_verbs(**{"start-build": True})
    spawn = harness.use_fake_build()
    harness.write_spec("2026-07-21-draft.md", status="draft")
    resp = harness.call("start-build", {"spec": "2026-07-21-draft.md"})
    assert resp["ok"] is False
    assert resp["error"]["code"] == "bad-args"
    assert "confirmed" in resp["error"]["message"]
    assert spawn.calls == []


@pytest.mark.parametrize("kwargs", [
    {"confirmed_by": "<username — any human>"},   # placeholder, unfilled
    {"confirmed_by": ""},                          # blank
    {"seq": "<seq of the confirm message>"},       # placeholder seq
    {"seq": ""},                                    # blank seq
])
def test_start_build_rejects_missing_confirm_record(harness, kwargs):
    harness.set_verbs(**{"start-build": True})
    spawn = harness.use_fake_build()
    harness.write_spec("2026-07-21-noconfirm.md", **kwargs)
    resp = harness.call("start-build", {"spec": "2026-07-21-noconfirm.md"})
    assert resp["ok"] is False
    assert resp["error"]["code"] == "bad-args"
    assert "confirm record" in resp["error"]["message"]
    assert spawn.calls == []


# --------------------------------------------------------- happy path + argv

def test_start_build_launches_confirmed_spec(harness):
    """A confirmed spec launches, narrates started+done, lands on loop/<slug>,
    and merges/pushes nothing. Uses the REAL detached-Popen path (the build
    stub), so the whole exec + reaper + narration chain is exercised."""
    harness.set_verbs(**{"start-build": True})
    harness.write_spec("2026-07-21-gif-picker.md")
    resp = harness.call("start-build", {"spec": "2026-07-21-gif-picker.md"})
    assert resp["ok"] is True
    r = resp["result"]
    # DELIBERATE ADDITION (WP-L4 open fork): the result now names the transient
    # systemd unit the build runs in. `pid` is the LOCAL sudo/systemd-run
    # process; the unit is what makes the build itself inspectable and stoppable
    # after this broker process is gone, so it belongs in the reply.
    assert r == {"started": True, "branch": "loop/2026-07-21-gif-picker",
                 "slug": "2026-07-21-gif-picker", "pid": r["pid"],
                 "unit": "disjorn-build-2026-07-21-gif-picker.service",
                 "confirmed_by": "plink", "seq": 139}
    harness.broker.join_builds()
    # narration: exactly a 'started' then a 'done', state-transition only.
    bodies = [p["body"] for p in harness.proposals]
    assert any(b.startswith("build started | 2026-07-21-gif-picker "
                            "-> loop/2026-07-21-gif-picker")
               and "confirmed by plink (#custodian seq 139)" in b for b in bodies)
    assert any(b.startswith("build done | 2026-07-21-gif-picker "
                            "-> loop/2026-07-21-gif-picker")
               and "tier pending" in b and "nothing merged" in b for b in bodies)
    # the build actually ran and read the spec on stdin.
    (rec,) = harness.build_records()
    assert "--model" in rec["argv"] and "claude-opus-4-8" in rec["argv"]
    assert "2026-07-21-gif-picker" in rec["argv"]
    assert "## Confirm record" in rec["stdin"]  # the spec text went on stdin


def test_start_build_argv_is_config_pure(harness):
    """argv = [*command, resident, slug, *session_argv, --model, model]; the
    spec (chat-derived) NEVER appears in argv — only on stdin."""
    harness.set_verbs(**{"start-build": True})
    spawn = harness.use_fake_build()
    harness.write_spec("2026-07-21-gif-picker.md")
    harness.call("start-build", {"spec": "2026-07-21-gif-picker.md"})
    harness.broker.join_builds()
    (argv,) = spawn.calls
    # tail is exactly the pin idiom (WP-L5)
    assert argv[-2:] == ["--model", "claude-opus-4-8"]
    assert "gable" in argv and "2026-07-21-gif-picker" in argv
    assert "--output-format" in argv and "json" in argv
    # no spec content smuggled into argv
    assert not any("Confirm record" in a or "Verbatim" in a for a in argv)
    # spec rode on stdin instead
    assert b"## Confirm record" in spawn.procs[0].stdin_written


def test_start_build_slug_keeps_date_and_branch_is_loop(harness):
    """BL-D4: the slug KEEPS the spec's date prefix, so the branch/container
    name is 1:1 with the spec file and two same-named specs cannot collide."""
    harness.set_verbs(**{"start-build": True})
    spawn = harness.use_fake_build()
    harness.write_spec("2026-12-01-voice-notes.md")
    resp = harness.call("start-build", {"spec": "2026-12-01-voice-notes.md"})
    assert resp["result"]["slug"] == "2026-12-01-voice-notes"
    assert resp["result"]["branch"] == "loop/2026-12-01-voice-notes"
    harness.broker.join_builds()


# ----------------------------------------------------------------- budget

def test_start_build_budget_enforced_and_audited(harness):
    harness.set_verbs(**{"start-build": True})
    harness.broker.start_build["daily_build_cap"] = 2
    spawn = harness.use_fake_build()
    for i in range(2):
        harness.write_spec(f"2026-07-21-feat{i}.md")
        assert harness.call("start-build", {"spec": f"2026-07-21-feat{i}.md"})["ok"] is True
    harness.write_spec("2026-07-21-feat9.md")
    resp = harness.call("start-build", {"spec": "2026-07-21-feat9.md"})
    assert resp["ok"] is False
    assert resp["error"]["code"] == "over-budget"
    last = harness.audit_lines()[-1]
    assert last["allowed"] is False and "budget" in last["result_summary"]
    assert len(spawn.calls) == 2          # the over-budget build never launched
    harness.broker.join_builds()


def test_start_build_budget_race_guard(harness):
    """H13-D4: concurrent start-builds can NEVER both slip past the cap. Fire
    many at once against cap=3; exactly 3 launch, the rest are over-budget."""
    harness.set_verbs(**{"start-build": True})
    harness.broker.start_build["daily_build_cap"] = 3
    spawn = harness.use_fake_build()
    for i in range(10):
        harness.write_spec(f"2026-07-21-race{i}.md")

    results: list[dict] = []
    lock = threading.Lock()

    def fire(i):
        r = harness.call("start-build", {"spec": f"2026-07-21-race{i}.md"})
        with lock:
            results.append(r)

    threads = [threading.Thread(target=fire, args=(i,)) for i in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    ok = [r for r in results if r["ok"]]
    over = [r for r in results if not r["ok"] and r["error"]["code"] == "over-budget"]
    assert len(ok) == 3, [r for r in results]
    assert len(over) == 7
    assert len(spawn.calls) == 3          # never launched more than the cap
    harness.broker.join_builds()


def test_start_build_default_cap_is_two(harness):
    """With no daily_build_cap configured the ratified default (2) applies —
    builds are capped by DEFAULT, unlike the WP-H12 action budget."""
    harness.set_verbs(**{"start-build": True})
    harness.broker.start_build.pop("daily_build_cap", None)
    spawn = harness.use_fake_build()
    for i in range(2):
        harness.write_spec(f"2026-07-21-d{i}.md")
        assert harness.call("start-build", {"spec": f"2026-07-21-d{i}.md"})["ok"] is True
    harness.write_spec("2026-07-21-d9.md")
    assert harness.call("start-build", {"spec": "2026-07-21-d9.md"})["error"]["code"] == "over-budget"
    harness.broker.join_builds()


# --------------------------------------------------------- detachment

def test_start_build_outlives_the_request(harness):
    """The verb must RETURN while the build is still running — detachment. A
    blocking fake proc gates communicate(); the call returns started=True
    before we release it."""
    harness.set_verbs(**{"start-build": True})
    proc = FakeBuildProc(out=b'{"files": [], "tests": "n/a", "diff": ""}',
                         block=True)
    spawn = harness.use_fake_build(proc_factory=lambda: proc)
    harness.write_spec("2026-07-21-slow.md")
    resp = harness.call("start-build", {"spec": "2026-07-21-slow.md"})
    assert resp["ok"] is True and resp["result"]["started"] is True
    # the reaper is still blocked in communicate(): no 'done' line yet.
    assert not any("build done" in p["body"] for p in harness.proposals)
    proc.release.set()                    # let the build finish
    harness.broker.join_builds()
    assert any("build done" in p["body"] for p in harness.proposals)


def test_default_build_spawn_is_detached(monkeypatch, tmp_path):
    """_default_build_spawn must detach (own session), keep stdin a PIPE (the
    spec is delivered there), and send stdout/stderr to the FILES it is given.

    DELIBERATE CHANGE (BL-D2): this test used to assert stdout/stderr were
    subprocess.PIPE. Piping a resident-influenced build session into the
    privileged broker is exactly the OOM the red-team measured (180MB stdout ->
    540MB broker RSS), so the contract is now "files in, bounded tail out". The
    detachment half of the contract — own session, un-waited, stdin piped — is
    unchanged and still asserted here."""
    import subprocess
    captured = {}

    class _FakePopen:
        def __init__(self, argv, **kw):
            captured["argv"] = argv
            captured["kw"] = kw
            self.pid = 1

    monkeypatch.setattr(subprocess, "Popen", _FakePopen)
    broker = Broker({"broker": {"audit_log": "/dev/null"}}, "/nonexistent")
    with open(tmp_path / "o", "wb") as out_fh, open(tmp_path / "e", "wb") as err_fh:
        broker._default_build_spawn(["run-build.sh", "gable", "x"],
                                    stdout=out_fh, stderr=err_fh)
        assert captured["kw"]["start_new_session"] is True
        assert captured["kw"]["stdin"] is subprocess.PIPE
        assert captured["kw"]["stdout"] is out_fh
        assert captured["kw"]["stderr"] is err_fh
        assert captured["kw"]["stdout"] is not subprocess.PIPE


# ------------------------------------------------------ failed narration

def test_start_build_failed_narration_is_loud(harness):
    harness.set_verbs(**{"start-build": True})
    proc = FakeBuildProc(out=b"", err=b"boom on line 12", rc=1)
    harness.use_fake_build(proc_factory=lambda: proc)
    harness.write_spec("2026-07-21-boom.md")
    resp = harness.call("start-build", {"spec": "2026-07-21-boom.md"})
    assert resp["ok"] is True            # launched fine; the RUN failed
    harness.broker.join_builds()
    bodies = [p["body"] for p in harness.proposals]
    assert any(b.startswith("BUILD FAILED | 2026-07-21-boom "
                            "-> loop/2026-07-21-boom")
               and "boom on line 12" in b for b in bodies)
    assert not any("build done" in b for b in bodies)


def test_start_build_timeout_narration(harness):
    harness.set_verbs(**{"start-build": True})
    proc = FakeBuildProc(raise_timeout=True)
    harness.use_fake_build(proc_factory=lambda: proc)
    harness.write_spec("2026-07-21-hang.md")
    assert harness.call("start-build", {"spec": "2026-07-21-hang.md"})["ok"] is True
    harness.broker.join_builds()
    bodies = [p["body"] for p in harness.proposals]
    assert any(b.startswith("BUILD FAILED") and "timed out" in b for b in bodies)
    assert proc.killed is True           # the cap killed the session


# ----------------------------------------------- pure formatter/parse shapes

def test_narration_formatter_shapes():
    started = format_build_started(slug="gif-picker", branch="loop/gif-picker",
                                   confirmed_by="usrda", seq=141, eta_sec=3600)
    assert started.startswith("build started | gif-picker -> loop/gif-picker")
    assert "confirmed by usrda (#custodian seq 141)" in started
    assert "ETA <= 60m (guess)" in started
    assert "no merge, no push" in started

    done = format_build_done(slug="x", branch="loop/x", files="a.py, b.py",
                             tests="12 passed", diff="+40 -2")
    assert "tier pending" in done and "files: a.py, b.py" in done
    assert "nothing merged" in done

    failed = format_build_failed(slug="x", branch="loop/x", reason="exit 1: boom")
    assert failed.startswith("BUILD FAILED | x -> loop/x | exit 1: boom")


def test_spec_parsers():
    text = (
        "# Spec\n\n## Status\n`confirmed`\n<!-- comment -->\n\n"
        "## Confirm record\n- **Confirmed by**: plink\n"
        "- **#custodian seq**: 139\n")
    assert parse_spec_status(text) == "confirmed"
    cr = parse_confirm_record(text)
    assert cr == {"confirmed_by": "plink", "seq": 139}
    # placeholders read as unfilled
    ph = parse_confirm_record(
        "## Confirm record\n- **Confirmed by**: <username>\n"
        "- **#custodian seq**: <seq>\n")
    assert ph == {"confirmed_by": None, "seq": None}
    assert parse_spec_status("# no status here") is None


def test_slug_derivation():
    """BL-D4: the date prefix is REQUIRED and KEPT — it is the deterministic,
    human-readable disambiguator, and it makes branch == spec basename."""
    assert (slug_from_spec_filename("SPECS/2026-07-21-gif-picker.md")
            == "2026-07-21-gif-picker")
    assert slug_from_spec_filename("2026-01-01-voice.md") == "2026-01-01-voice"
    # basename is taken first, so a leading ../ is not the slug's concern (path
    # confinement is _resolve_spec_path's job) — but an uppercase/underscore/
    # empty name, a missing date prefix, or an impossible date is rejected here.
    for bad in ["2026-07-21-Bad_Slug.md", "2026-07-21-.md", "2026-07-21-a b.md",
                "voice-notes.md", "202-07-21-x.md", "2026-13-45-x.md"]:
        with pytest.raises(Exception):
            slug_from_spec_filename(bad)


# --------------------------------------------------- config / template

def test_start_build_template_section_parses():
    import tomllib
    tmpl_dir = Path(__file__).resolve().parent.parent
    with open(tmpl_dir / "broker.toml", "rb") as fh:
        tmpl = tomllib.load(fh)
    sb = tmpl["start_build"]
    assert isinstance(sb["command"], list) and sb["command"]
    # WP-L4 open fork: the launch is privileged (it drops to the resident's uid),
    # so it goes through sudo and the ONE validating helper — never run-build.sh
    # directly (that ran the build as plink) and never systemd-run (which cannot
    # be granted through sudoers without granting root).
    assert sb["command"][:2] == ["sudo", "-n"]
    assert sb["command"][2].endswith("/disjorn-build-launch")
    assert sb["command"][3] == "run"
    assert sb["stop_command"][2:] == [sb["command"][2], "stop"]
    assert sb["unit_state_command"][0] == "systemctl"   # unprivileged read
    assert "sudo" not in sb["unit_state_command"]
    assert sb["model"] == "claude-opus-4-8"          # WP-L5 pin, no fallback
    assert sb["session_argv"][-1] == "build-session"  # argv0 for the "$@" pin
    assert sb["specs_dir"].endswith("/SPECS")
    assert sb["daily_build_cap"] == 2                  # ratified default


# ======================================================================
# BUILD-LOOP red-team regressions (BL-D2 / BL-D3 / BL-D4)
# Each test below is the test that would have caught the original bug.
# ======================================================================

# ------------------------------------ BL-D2: bounded build output on disk

def test_build_output_is_bounded_and_never_buffered_in_the_broker(harness):
    """A build that floods stdout must NOT stream into the privileged broker.
    The original code called proc.communicate() on a pipe, so 180MB of stdout
    became ~540MB of broker RSS and could OOM the gateway for every resident.
    Now the output lands in a temp FILE and only a bounded tail is read back.

    Runs the REAL detached-subprocess path with a stub that writes 8MB before
    its report: the done narration must still carry the report (parsed from the
    tail), and the tail the broker ever holds is <= MAX_BUILD_LOG_TAIL."""
    harness.set_verbs(**{"start-build": True})
    harness.use_flood_build(megabytes=8)
    harness.write_spec("2026-07-21-flood.md")

    # Spy on the tail read: record how big the file on disk got, and how much
    # of it the broker actually pulled into memory.
    on_disk: dict[str, int] = {}
    tails: list[int] = []
    real_tail = harness.broker._read_build_tail

    def spy_tail(path, limit=MAX_BUILD_LOG_TAIL):
        on_disk[path] = os.path.getsize(path)
        out = real_tail(path, limit)
        tails.append(len(out))
        return out

    harness.broker._read_build_tail = spy_tail
    assert harness.call("start-build", {"spec": "2026-07-21-flood.md"})["ok"] is True
    harness.broker.join_builds(timeout=60)
    # the flood really landed on DISK...
    assert max(on_disk.values()) > 4 * 1024 * 1024, on_disk
    # ...and at most a bounded tail of it ever entered the broker.
    assert tails and max(tails) <= MAX_BUILD_LOG_TAIL
    bodies = [p["body"] for p in harness.proposals]
    done = [b for b in bodies if b.startswith("build done")]
    assert done, bodies
    assert "files: big.py" in done[0] and "tests: 1 passed" in done[0]
    # the whole narration stays small — nothing resident-influenced is echoed
    # into a privileged path unbounded.
    assert len(done[0]) < 2000
    assert harness.build_log_files() == []      # and the flood was cleaned up


def test_read_build_tail_is_capped(tmp_path):
    """The tail reader is bounded BY CONSTRUCTION (seek to end), not by trust
    in the child: a 5MB file yields exactly MAX_BUILD_LOG_TAIL bytes."""
    big = tmp_path / "big.out"
    big.write_bytes(b"A" * (5 * 1024 * 1024) + b"TAILMARK")
    tail = Broker._read_build_tail(str(big))
    assert len(tail) == MAX_BUILD_LOG_TAIL
    assert tail.endswith("TAILMARK")


def test_build_log_files_are_0600_and_removed_after_the_build(harness):
    """Restrictive perms while running (no other local user reads a build's
    output), and gone afterwards — no unbounded on-disk growth either."""
    harness.set_verbs(**{"start-build": True})
    proc = FakeBuildProc(out=b'{"files": [], "tests": "ok", "diff": ""}',
                         block=True)
    spawn = harness.use_fake_build(proc_factory=lambda: proc)
    harness.write_spec("2026-07-21-perms.md")
    assert harness.call("start-build", {"spec": "2026-07-21-perms.md"})["ok"] is True
    (out_path, err_path), = spawn.log_paths
    for path in (out_path, err_path):
        assert os.stat(path).st_mode & 0o777 == 0o600, path
        assert os.path.dirname(path) == str(harness.build_log_dir)
    proc.release.set()
    harness.broker.join_builds()
    assert harness.build_log_files() == []


def test_broker_does_not_hold_the_build_output_handles(harness):
    """The broker must close ITS copies of the output files immediately after
    spawn — the detached child holds its own dups. Otherwise every build leaks
    two fds in the long-lived privileged daemon."""
    harness.set_verbs(**{"start-build": True})
    proc = FakeBuildProc(out=b"{}", block=True)
    spawn = harness.use_fake_build(proc_factory=lambda: proc)
    harness.write_spec("2026-07-21-fds.md")
    harness.call("start-build", {"spec": "2026-07-21-fds.md"})
    assert all(fh.closed for pair in spawn.parent_handles for fh in pair)
    proc.release.set()
    harness.broker.join_builds()


@pytest.mark.parametrize("factory,expect", [
    (lambda: FakeBuildProc(out=b"", err=b"boom", rc=1), "BUILD FAILED"),
    (lambda: FakeBuildProc(raise_timeout=True), "BUILD FAILED"),
    (lambda: FakeBuildProc(out=b'{"files": [], "tests": "ok", "diff": ""}'),
     "build done"),
])
def test_build_logs_are_cleaned_up_on_every_exit_path(harness, factory, expect):
    """done, failed AND timed-out all delete both temp files."""
    harness.set_verbs(**{"start-build": True})
    harness.use_fake_build(proc_factory=factory)
    harness.write_spec("2026-07-21-cleanup.md")
    assert harness.call("start-build", {"spec": "2026-07-21-cleanup.md"})["ok"] is True
    harness.broker.join_builds()
    assert any(b.startswith(expect) for b in
               [p["body"] for p in harness.proposals])
    assert harness.build_log_files() == []


def test_build_logs_are_cleaned_up_when_the_spawn_fails(harness):
    """A launch that never happens must leave no temp files behind either."""
    harness.set_verbs(**{"start-build": True})

    def _boom(argv, *, stdout, stderr):
        raise OSError("no such file")

    harness.broker._build_spawn = _boom
    harness.write_spec("2026-07-21-nospawn.md")
    resp = harness.call("start-build", {"spec": "2026-07-21-nospawn.md"})
    assert resp["error"]["code"] == "exec-failure"
    assert harness.build_log_files() == []


def test_report_is_parsed_from_the_last_line_of_a_truncated_tail():
    """A bounded tail can begin mid-line; the report is the LAST line, so it
    must still be found."""
    report = _parse_build_report(
        "…truncated garbage without a newline\n"
        '{"files": ["a.py"], "tests": "3 passed", "diff": "+9 -1"}')
    assert report == {"files": "a.py", "tests": "3 passed", "diff": "+9 -1"}


# --------------------- BL-D3: never-started builds must not consume a slot

def _restarted_count(harness, resident="res-test"):
    """What a FRESH broker (same audit log) would seed the day's build count
    to — i.e. what survives a restart."""
    today = time.strftime("%Y-%m-%d", time.gmtime())
    return harness.broker._count_builds_today(resident, today)


def test_never_started_build_does_not_survive_a_restart(harness):
    """BL-D3: a spawn OSError refunds the in-memory slot but audits
    allowed=True (exec-failure is not a denial). Counting allowed=True lines on
    reseed therefore charged a resident for a build that never ran. Only lines
    carrying the `build_started` marker count now."""
    harness.set_verbs(**{"start-build": True})

    def _boom(argv, *, stdout, stderr):
        raise OSError("exec format error")

    harness.broker._build_spawn = _boom
    harness.write_spec("2026-07-21-ghost.md")
    resp = harness.call("start-build", {"spec": "2026-07-21-ghost.md"})
    assert resp["error"]["code"] == "exec-failure"
    line = harness.audit_lines()[-1]
    assert line["allowed"] is True              # authorized, and it errored
    assert "build_started" not in line          # but nothing ever started
    assert _restarted_count(harness) == 0       # so a restart charges nothing


def test_started_then_failed_build_does_survive_a_restart(harness):
    """The other ordering: a build that RAN and then failed keeps its slot —
    it burned the attempt, and the marker records that."""
    harness.set_verbs(**{"start-build": True})
    harness.use_fake_build(proc_factory=lambda: FakeBuildProc(err=b"boom", rc=1))
    harness.write_spec("2026-07-21-ran.md")
    assert harness.call("start-build", {"spec": "2026-07-21-ran.md"})["ok"] is True
    harness.broker.join_builds()
    line = harness.audit_lines()[-1]
    assert line["allowed"] is True and line["build_started"] is True
    assert _restarted_count(harness) == 1


@pytest.mark.parametrize("order", ["ghost-first", "real-first"])
def test_reseed_counts_only_started_builds_in_either_order(harness, order):
    """Both orderings of (never-started, ran) reseed to exactly 1."""
    harness.set_verbs(**{"start-build": True})
    harness.broker.start_build["daily_build_cap"] = 5
    good = harness.use_fake_build()

    def _boom(argv, *, stdout, stderr):
        raise OSError("nope")

    def fire_ghost(i):
        harness.broker._build_spawn = _boom
        harness.write_spec(f"2026-07-21-ghost{i}.md")
        harness.call("start-build", {"spec": f"2026-07-21-ghost{i}.md"})

    def fire_real(i):
        harness.broker._build_spawn = good
        harness.write_spec(f"2026-07-21-real{i}.md")
        assert harness.call("start-build",
                            {"spec": f"2026-07-21-real{i}.md"})["ok"] is True

    if order == "ghost-first":
        fire_ghost(1)
        fire_real(1)
    else:
        fire_real(1)
        fire_ghost(1)
    harness.broker.join_builds()
    assert _restarted_count(harness) == 1


# ------------------------------- BL-D4: slug collisions (branch / container)

def test_same_name_specs_on_different_dates_do_not_collide(harness):
    """The footgun: two specs with the same name but different dates used to
    derive the SAME loop/<slug> branch and the same `podman --name
    disjorn-build-<slug>` — concurrent runs clashed, sequential ones clobbered
    the branch. The date prefix now rides along, so they are distinct."""
    harness.set_verbs(**{"start-build": True})
    harness.broker.start_build["daily_build_cap"] = 5
    spawn = harness.use_fake_build()
    branches = []
    for date in ("2026-07-21", "2026-09-02"):
        harness.write_spec(f"{date}-gif-picker.md")
        resp = harness.call("start-build", {"spec": f"{date}-gif-picker.md"})
        assert resp["ok"] is True, resp
        branches.append(resp["result"]["branch"])
    assert branches == ["loop/2026-07-21-gif-picker", "loop/2026-09-02-gif-picker"]
    # ...and the container/argv positional differs too (run-build.sh names the
    # container disjorn-build-<slug>).
    slugs = [argv[argv.index("gable") + 1] for argv in spawn.calls]
    assert slugs == ["2026-07-21-gif-picker", "2026-09-02-gif-picker"]
    harness.broker.join_builds()


def test_same_spec_cannot_build_twice_at_once(harness):
    """Same date + same name IS the same spec; two in flight would still fight
    over podman --name and the branch. The second is refused LOUDLY, and — as a
    denial — burns no budget."""
    harness.set_verbs(**{"start-build": True})
    harness.broker.start_build["daily_build_cap"] = 2
    proc = FakeBuildProc(out=b"{}", block=True)
    spawn = harness.use_fake_build(proc_factory=lambda: proc)
    harness.write_spec("2026-07-21-twice.md")
    assert harness.call("start-build", {"spec": "2026-07-21-twice.md"})["ok"] is True
    resp = harness.call("start-build", {"spec": "2026-07-21-twice.md"})
    assert resp["ok"] is False
    assert resp["error"]["code"] == "bad-args"
    assert "already running" in resp["error"]["message"]
    assert len(spawn.calls) == 1
    last = harness.audit_lines()[-1]
    assert last["allowed"] is False and "already running" in last["result_summary"]
    proc.release.set()
    harness.broker.join_builds()
    # the refusal cost no budget: the second slot is still there afterwards.
    harness.write_spec("2026-07-22-after.md")
    assert harness.call("start-build", {"spec": "2026-07-22-after.md"})["ok"] is True
    harness.broker.join_builds()


def test_slug_claim_is_released_when_the_build_ends(harness):
    """The in-flight claim is not a permanent lock — once the build reaches a
    terminal state the same spec may be rebuilt (budget permitting)."""
    harness.set_verbs(**{"start-build": True})
    harness.broker.start_build["daily_build_cap"] = 3
    harness.use_fake_build()
    harness.write_spec("2026-07-21-again.md")
    assert harness.call("start-build", {"spec": "2026-07-21-again.md"})["ok"] is True
    harness.broker.join_builds()
    assert harness.call("start-build", {"spec": "2026-07-21-again.md"})["ok"] is True
    harness.broker.join_builds()


def test_concurrent_same_slug_launches_exactly_one(harness):
    """Ten threads, one spec: exactly one build may be in flight for a slug."""
    harness.set_verbs(**{"start-build": True})
    harness.broker.start_build["daily_build_cap"] = 10
    proc = FakeBuildProc(out=b"{}", block=True)
    spawn = harness.use_fake_build(proc_factory=lambda: proc)
    harness.write_spec("2026-07-21-storm.md")

    results: list[dict] = []
    lock = threading.Lock()

    def fire():
        r = harness.call("start-build", {"spec": "2026-07-21-storm.md"})
        with lock:
            results.append(r)

    threads = [threading.Thread(target=fire) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len([r for r in results if r["ok"]]) == 1
    assert len(spawn.calls) == 1
    proc.release.set()
    harness.broker.join_builds()


def test_build_log_dir_defaults_next_to_the_audit_log_not_tmpfs(tmp_path):
    """BL-D2 default placement: the spool must NOT land in the process temp
    dir, which is tmpfs on this host — spooling there would move the flood
    back into RAM. It goes beside the audit log (LogsDirectory, on disk)."""
    broker = Broker({"broker": {"audit_log": str(tmp_path / "logs" / "audit.jsonl")}},
                    "/nonexistent")
    (tmp_path / "logs").mkdir()
    d = broker._build_log_dir()
    assert d == str(tmp_path / "logs" / "build-logs")   # NOT tempfile.gettempdir()
    assert os.path.isdir(d)
    assert os.stat(d).st_mode & 0o777 == 0o700
    # an explicit config value still wins
    broker.config["broker"]["build_log_dir"] = str(tmp_path / "elsewhere")
    assert broker._build_log_dir() == str(tmp_path / "elsewhere")
