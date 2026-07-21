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
    Broker,
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
    assert r == {"started": True, "branch": "loop/gif-picker",
                 "slug": "gif-picker", "pid": r["pid"],
                 "confirmed_by": "plink", "seq": 139}
    harness.broker.join_builds()
    # narration: exactly a 'started' then a 'done', state-transition only.
    bodies = [p["body"] for p in harness.proposals]
    assert any(b.startswith("build started | gif-picker -> loop/gif-picker")
               and "confirmed by plink (#custodian seq 139)" in b for b in bodies)
    assert any(b.startswith("build done | gif-picker -> loop/gif-picker")
               and "tier pending" in b and "nothing merged" in b for b in bodies)
    # the build actually ran and read the spec on stdin.
    (rec,) = harness.build_records()
    assert "--model" in rec["argv"] and "claude-opus-4-8" in rec["argv"]
    assert "gif-picker" in rec["argv"]
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
    assert "gable" in argv and "gif-picker" in argv
    assert "--output-format" in argv and "json" in argv
    # no spec content smuggled into argv
    assert not any("Confirm record" in a or "Verbatim" in a for a in argv)
    # spec rode on stdin instead
    assert b"## Confirm record" in spawn.procs[0].stdin_written


def test_start_build_slug_strips_date_and_branch_is_loop(harness):
    harness.set_verbs(**{"start-build": True})
    spawn = harness.use_fake_build()
    harness.write_spec("2026-12-01-voice-notes.md")
    resp = harness.call("start-build", {"spec": "2026-12-01-voice-notes.md"})
    assert resp["result"]["slug"] == "voice-notes"
    assert resp["result"]["branch"] == "loop/voice-notes"
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


def test_default_build_spawn_is_detached(monkeypatch):
    """_default_build_spawn must detach (own session) and pipe all three fds —
    the documented detachment contract, asserted at the exec boundary."""
    import subprocess
    captured = {}

    class _FakePopen:
        def __init__(self, argv, **kw):
            captured["argv"] = argv
            captured["kw"] = kw
            self.pid = 1

    monkeypatch.setattr(subprocess, "Popen", _FakePopen)
    broker = Broker({"broker": {"audit_log": "/dev/null"}}, "/nonexistent")
    broker._default_build_spawn(["run-build.sh", "gable", "x"])
    assert captured["kw"]["start_new_session"] is True
    assert captured["kw"]["stdin"] is subprocess.PIPE
    assert captured["kw"]["stdout"] is subprocess.PIPE
    assert captured["kw"]["stderr"] is subprocess.PIPE


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
    assert any(b.startswith("BUILD FAILED | boom -> loop/boom")
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
    assert slug_from_spec_filename("SPECS/2026-07-21-gif-picker.md") == "gif-picker"
    assert slug_from_spec_filename("2026-01-01-voice.md") == "voice"
    # basename is taken first, so a leading ../ is not the slug's concern (path
    # confinement is _resolve_spec_path's job) — but an uppercase/underscore/
    # empty slug is rejected here.
    for bad in ["2026-07-21-Bad_Slug.md", "2026-07-21-.md", "2026-07-21-a b.md"]:
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
    assert sb["model"] == "claude-opus-4-8"          # WP-L5 pin, no fallback
    assert sb["session_argv"][-1] == "build-session"  # argv0 for the "$@" pin
    assert sb["specs_dir"].endswith("/SPECS")
    assert sb["daily_build_cap"] == 2                  # ratified default
