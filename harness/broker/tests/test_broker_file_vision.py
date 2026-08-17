"""File vision — every branch in the mirror, one branch in the build seat.
SPECS/2026-08-14-file-vision.md, items 1 and 5.

The mirror could tell a resident what production runs and could not show them
a single branch they were being asked to review. Two things change here:

  1. `refresh-mirror` also fetches every ENTITLED gatehouse repo's branches
     into refs/gatehouse/<repo>/*, with --prune. Still zero caller args: the
     repo list is plink's config, the refspec is a constant, and a resident can
     refresh but can never aim git. A ref that vanished is named in the
     summary and called "harvested or deleted" — the mirror cannot tell those
     apart and does not pretend to.

  5. A build banner that names a sha refreshes the mirror BEFORE it posts, so
     the sha it names is one its audience can open. On failure it says so,
     because "in the gatehouse" and "readable by you" are different claims and
     a reviewer has to know which one they were handed.

WHAT IS NOT TESTED HERE, DELIBERATELY: that the fetch produces correct refs.
That is git's job, and a test that stubs git and then asserts git's semantics
is a test of the stub. What is asserted is the ARGV — the fixed shape, the
refspec, the prune flag, and the fact that no caller input reaches any of it.
"""

from __future__ import annotations

import json

import pytest

from broker_testlib import STUB_PUBLISHED, STUB_SHA, FakeBuildProc, build_out
from brokerd import Broker, format_mirror_note

GATEHOUSE = "/var/lib/disjorn-broker/gatehouse"


def _fetch_stub(tmp_path, record, *, stderr: str = "", rc: int = 0):
    """A stand-in for `git fetch --prune`: records its argv, then prints the
    ref chatter git prints, on the stream git prints it on (stderr)."""
    path = tmp_path / f"gh-fetch-{len(list(tmp_path.glob('gh-fetch-*')))}.py"
    path.write_text(
        "#!/usr/bin/env python3\n"
        "import json, sys\n"
        f"open({str(record)!r}, 'a').write(json.dumps(sys.argv[1:]) + '\\n')\n"
        f"sys.stderr.write({stderr!r})\n"
        f"raise SystemExit({rc})\n")
    path.chmod(0o755)
    return path


def _configure_gatehouse(harness, tmp_path, repos=("disjorn", "claudette"),
                         *, stderr: str = "", rc: int = 0):
    record = tmp_path / "gatehouse-fetch.jsonl"
    stub = _fetch_stub(tmp_path, record, stderr=stderr, rc=rc)
    harness.broker.commands["refresh_mirror_gatehouse_dir"] = GATEHOUSE
    harness.broker.commands["refresh_mirror_gatehouse_repos"] = list(repos)
    # The stub stands in for `git ... fetch`, and carries --prune the way the
    # real base argv does, so what the recorder sees is the whole shape a
    # deployment actually runs.
    harness.broker.commands["refresh_mirror_gatehouse_fetch"] = [
        harness_python(), str(stub), "--prune"]
    return record


def harness_python() -> str:
    import sys
    return sys.executable


def _recorded(record) -> list[list[str]]:
    if not record.exists():
        return []
    return [json.loads(line) for line in
            record.read_text().splitlines() if line.strip()]


# ======================================================================
# 1. THE GATEHOUSE FETCH
# ======================================================================

def test_unconfigured_gatehouse_is_exactly_todays_behaviour(harness):
    """An unmigrated /etc/disjorn-broker/broker.toml has neither key. It must
    keep working — main-only — rather than fail on a config it never had."""
    harness.set_verbs(**{"refresh-mirror": True})
    resp = harness.call("refresh-mirror", {})
    assert resp["ok"] is True
    assert resp["result"]["gatehouse"] == []
    assert harness.recorded_argv() == [
        ["rev-parse", "--short", "HEAD"],
        ["fetch", "origin"],
        ["merge", "--ff-only", "origin/main"],
        ["rev-parse", "--short", "HEAD"],
    ]


def test_one_pruned_fetch_per_entitled_repo_into_its_own_namespace(harness,
                                                                   tmp_path):
    harness.set_verbs(**{"refresh-mirror": True})
    record = _configure_gatehouse(harness, tmp_path)
    resp = harness.call("refresh-mirror", {})
    assert resp["ok"] is True
    assert _recorded(record) == [
        ["--prune", f"{GATEHOUSE}/disjorn.git",
         "+refs/heads/*:refs/gatehouse/disjorn/*"],
        ["--prune", f"{GATEHOUSE}/claudette.git",
         "+refs/heads/*:refs/gatehouse/claudette/*"],
    ]
    # main still comes from origin — TWO SOURCES, so mirror-main can never LEAD
    # what production runs (plink + Claudette, 2026-08-15).
    assert ["merge", "--ff-only", "origin/main"] in harness.recorded_argv()


def test_main_is_updated_before_the_branches_are_fetched(harness, tmp_path):
    """Ordering is not cosmetic: the ff-only update is the step that is allowed
    to FAIL and stop everything, and it must do so before anything else has
    moved in the mirror."""
    harness.set_verbs(**{"refresh-mirror": True})
    fail = tmp_path / "ff-fail.py"
    fail.write_text("#!/usr/bin/env python3\nimport sys\n"
                    "sys.stderr.write('fatal: Not possible to fast-forward')\n"
                    "raise SystemExit(128)\n")
    fail.chmod(0o755)
    record = _configure_gatehouse(harness, tmp_path)
    harness.broker.commands["refresh_mirror_update"] = [harness_python(),
                                                        str(fail)]
    resp = harness.call("refresh-mirror", {})
    assert resp["error"]["code"] == "exec-failure"
    assert _recorded(record) == [], "a diverged mirror must fetch nothing"


def test_the_verb_still_takes_no_caller_args(harness, tmp_path):
    """The whole safety story: a resident can refresh, and can never aim git.
    Nothing a caller sends may reach an argv, including now that there are more
    argvs to aim."""
    harness.set_verbs(**{"refresh-mirror": True})
    record = _configure_gatehouse(harness, tmp_path)
    for hostile in ({"repo": "../../etc"}, {"remote": "https://evil.example"},
                    {"refspec": "+refs/*:refs/*"}, {"gatehouse_repos": ["x"]}):
        resp = harness.call("refresh-mirror", hostile)
        assert resp["error"]["code"] == "bad-args", hostile
    assert _recorded(record) == []
    assert harness.recorded_argv() == []


@pytest.mark.parametrize("repos", [
    ["-upload-pack=/bin/sh"],          # a name that is a flag
    ["../../../etc/passwd"],           # a name that is a path
    ["disjorn.git;rm -rf /"],          # a name with punctuation
    [".."],                            # the parent directory
    [""],                              # nothing at all
    [17],                              # not even a string
])
def test_a_hostile_repo_name_in_config_is_refused_before_git_runs(harness,
                                                                  tmp_path,
                                                                  repos):
    """This list is plink's own config, so the check is a backstop rather than
    a wall — the same reasoning disjorn-build-launch gives for re-validating
    its slug. A config typo must not become a git flag, and the cost of being
    sure is one regex."""
    harness.set_verbs(**{"refresh-mirror": True})
    record = _configure_gatehouse(harness, tmp_path, repos=repos)
    resp = harness.call("refresh-mirror", {})
    assert resp["ok"] is False
    assert resp["error"]["code"] == "internal"
    assert _recorded(record) == []


def test_a_failed_gatehouse_fetch_is_loud(harness, tmp_path):
    """Half a mirror that reports success is worse than one that says it
    failed: the next reader trusts refs that were never updated."""
    harness.set_verbs(**{"refresh-mirror": True})
    _configure_gatehouse(harness, tmp_path, stderr="fatal: not a repository",
                         rc=128)
    resp = harness.call("refresh-mirror", {})
    assert resp["ok"] is False
    assert resp["error"]["code"] == "exec-failure"
    assert "not a repository" in resp["error"]["message"]


def test_vanished_refs_are_named_and_not_interpreted(harness, tmp_path):
    """--prune is the point: a branch that left the gatehouse leaves the
    mirror. WHY it left — harvested into main, or deleted — is unknowable from
    here, so the summary says both and names the ref rather than guessing."""
    harness.set_verbs(**{"refresh-mirror": True})
    chatter = (
        "From /var/lib/disjorn-broker/gatehouse/disjorn\n"
        " - [deleted]         (none)     -> gatehouse/disjorn/loop/2026-08-09-old\n"
        " * [new branch]      loop/2026-08-14-file-vision -> "
        "gatehouse/disjorn/loop/2026-08-14-file-vision\n")
    _configure_gatehouse(harness, tmp_path, repos=("disjorn",), stderr=chatter)
    resp = harness.call("refresh-mirror", {})
    assert resp["ok"] is True
    rec, = resp["result"]["gatehouse"]
    assert rec["repo"] == "disjorn"
    assert rec["vanished"] == ["gatehouse/disjorn/loop/2026-08-09-old"]
    assert rec["arrived"] == ["gatehouse/disjorn/loop/2026-08-14-file-vision"]
    summary = harness.audit_lines()[-1]["result_summary"]
    assert "disjorn: +1 new, -1 harvested or deleted" in summary


def test_a_quiet_fetch_says_nothing_extra(harness, tmp_path):
    """No news is no line. The summary is read in #custodian, and a verb that
    prints '+0 new, -0 gone' every time is a verb nobody reads."""
    harness.set_verbs(**{"refresh-mirror": True})
    _configure_gatehouse(harness, tmp_path, repos=("disjorn",))
    resp = harness.call("refresh-mirror", {})
    assert resp["ok"] is True
    assert "gatehouse" not in harness.audit_lines()[-1]["result_summary"]


def test_the_shipped_default_fetch_prunes(harness, tmp_path):
    """--prune is asserted on the DEFAULT argv, not just on a test stub: the
    flag is the whole reason a harvested branch stops being reviewable, and a
    deployment that omits it looks identical until someone reviews a ref that
    was merged a week ago."""
    harness.broker.commands.pop("refresh_mirror_gatehouse_fetch", None)
    harness.broker.commands["refresh_mirror_gatehouse_dir"] = GATEHOUSE
    harness.broker.commands["refresh_mirror_gatehouse_repos"] = ["disjorn"]
    (repo, argv), = harness.broker._gatehouse_fetch_argvs()
    assert repo == "disjorn"
    assert argv == ["git", "-C", "/srv/disjorn-ro", "fetch", "--prune",
                    f"{GATEHOUSE}/disjorn.git",
                    "+refs/heads/*:refs/gatehouse/disjorn/*"]


def test_the_parser_reads_git_chatter_and_ignores_the_rest():
    arrived, vanished = Broker._parse_fetch_refs(
        "From /var/lib/disjorn-broker/gatehouse/gable\n"
        "   abc1234..def5678  main       -> gatehouse/gable/main\n"
        " * [new branch]      loop/a     -> gatehouse/gable/loop/a\n"
        " - [deleted]         (none)     -> gatehouse/gable/loop/b\n"
        "\n")
    assert arrived == ["gatehouse/gable/loop/a"]
    assert vanished == ["gatehouse/gable/loop/b"]


# ======================================================================
# 5. THE BANNER NAMES A SHA ITS AUDIENCE CAN OPEN
# ======================================================================

def test_the_note_points_at_the_ref_a_reviewer_should_open():
    note = format_mirror_note("loop/2026-08-14-file-vision",
                              [("disjorn.git", STUB_SHA)], None)
    assert "gatehouse/disjorn/loop/2026-08-14-file-vision" in note
    assert note.startswith("\nmirror: refreshed")


def test_a_failed_refresh_is_said_out_loud_not_swallowed():
    note = format_mirror_note("loop/x", [("disjorn.git", STUB_SHA)],
                              "fatal: could not read from remote")
    assert "NOT refreshed" in note
    assert "could not read from remote" in note
    assert "run refresh-mirror" in note


def test_nothing_published_means_no_mirror_line_at_all():
    """A NO-COMMITS build has no sha to open, so the banner says nothing about
    the mirror. Every line on a banner has to be worth its space."""
    assert format_mirror_note("loop/x", [], None) == ""
    assert format_mirror_note("loop/x", [], "boom") == ""


def _run_build(harness, slug, out, *, rc=0):
    harness.set_verbs(**{"start-build": True})
    harness.use_fake_build(proc_factory=lambda: FakeBuildProc(out=out, rc=rc))
    harness.write_spec(f"{slug}.md")
    assert harness.call("start-build", {"spec": f"{slug}.md"})["ok"] is True
    harness.broker.join_builds()
    bodies = [p["body"] for p in harness.proposals]
    terminal = [b for b in bodies
                if b.startswith("build done") or b.startswith("BUILD FAILED")]
    assert len(terminal) == 1, bodies
    return terminal[0]


def test_a_published_build_refreshes_the_mirror_before_it_banners(harness,
                                                                  tmp_path):
    record = _configure_gatehouse(harness, tmp_path, repos=("disjorn",))
    body = _run_build(harness, "2026-08-14-vision", build_out(
        '{"files": ["a.py"], "tests": "4 passed", "diff": "+9 -1"}',
        publish=STUB_PUBLISHED))
    assert f"published: disjorn.git {STUB_SHA}" in body
    # THE fetch happened, and it is the same one refresh-mirror runs.
    assert _recorded(record) == [
        ["--prune", f"{GATEHOUSE}/disjorn.git",
         "+refs/heads/*:refs/gatehouse/disjorn/*"],
    ]
    assert "mirror: refreshed" in body
    assert "gatehouse/disjorn/loop/2026-08-14-vision" in body


def test_a_build_that_published_nothing_does_not_touch_the_mirror(harness,
                                                                  tmp_path):
    record = _configure_gatehouse(harness, tmp_path, repos=("disjorn",))
    body = _run_build(harness, "2026-08-14-empty", build_out(
        '{"files": [], "tests": "n/a", "diff": "n/a"}',
        publish="NO-COMMITS disjorn.git"))
    assert "no commits produced" in body
    assert _recorded(record) == []
    assert "mirror:" not in body


def test_a_mirror_failure_never_swallows_the_banner(harness, tmp_path):
    """The banner is the only thing anyone hears about a build. A fetch that
    fails must degrade the banner's CLAIM, never its delivery."""
    _configure_gatehouse(harness, tmp_path, repos=("disjorn",),
                         stderr="fatal: could not read from remote", rc=128)
    body = _run_build(harness, "2026-08-14-mirrorfail", build_out(
        '{"files": ["a.py"], "tests": "ok", "diff": "+1"}',
        publish=STUB_PUBLISHED))
    assert body.startswith("build done")
    assert f"published: disjorn.git {STUB_SHA}" in body
    assert "mirror: NOT refreshed" in body
    assert "could not read from remote" in body


def test_a_failed_build_that_published_anyway_still_refreshes(harness,
                                                              tmp_path):
    """The banner names a sha on the failure path too (a container can die
    clean and the push still land), so the same promise has to hold there."""
    record = _configure_gatehouse(harness, tmp_path, repos=("disjorn",))
    body = _run_build(harness, "2026-08-14-failpub", build_out(
        '{"files": ["a.py"], "tests": "1 failed", "diff": "+1"}',
        publish=STUB_PUBLISHED), rc=1)
    assert body.startswith("BUILD FAILED")
    assert "published anyway" in body
    assert len(_recorded(record)) == 1
    assert "mirror: refreshed" in body


def test_the_adopted_reaper_makes_the_same_promise(harness, tmp_path):
    """A broker restart must not change whether the sha in a banner is
    readable — both reapers post through the same wrapper."""
    record = _configure_gatehouse(harness, tmp_path, repos=("disjorn",))
    slug = "2026-08-14-adopted"
    out_p, err_p, out_fh, err_fh = harness.broker._open_build_logs(slug)
    out_fh.write(build_out('{"files": ["a.py"], "tests": "ok", "diff": "+1"}',
                           publish=STUB_PUBLISHED))
    err_fh.write(b"")
    harness.broker._close_build_logs(out_fh, err_fh)
    harness.broker._write_build_sidecar(
        {"slug": slug, "branch": f"loop/{slug}", "confirmed_by": "plink",
         "seq": 139, "resident": "res-test"},
        out_path=out_p, err_path=err_p, timeout=30)
    assert harness.broker.adopt_inflight_builds() == []
    body, = [p["body"] for p in harness.proposals
             if p["body"].startswith("build done")]
    assert "mirror: refreshed" in body
    assert len(_recorded(record)) == 1


def test_an_unconfigured_gatehouse_leaves_the_banner_exactly_as_it_was(harness):
    """No gatehouse config = no fetch and no mirror line, so a deployment that
    has not migrated its broker.toml sees the 08-13 banner unchanged."""
    body = _run_build(harness, "2026-08-14-nogh", build_out(
        '{"files": ["a.py"], "tests": "ok", "diff": "+1"}',
        publish=STUB_PUBLISHED))
    assert body.startswith("build done")
    assert "mirror:" not in body
