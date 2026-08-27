"""The spec's `## Status` line moves WITH its build (2026-08-17).

SPECS/README.md: "state lives in the file — Status moves draft -> confirmed ->
building -> built@<branch> -> merged (or failed), updated in place; the next
summon reads the file". Nothing ever wrote the middle words. On 2026-08-17 a
spec under construction still said `confirmed`, the board listed it as
buildable, and a resident set out to build one another build had already
claimed. So:

  * start-build stamps `building` (a plumbing commit on the canonical repo's
    main + a fast-forward of the mirror) BEFORE the started line;
  * the terminal banner stamps `built@<branch>` / `failed` / back to
    `confirmed`, from the same ladder the banner is narrated from;
  * a launch that never ran (spawn error, preflight refusal) puts the word back;
  * a word the keyboard already moved is never overwritten;
  * the keyboard's working tree is never touched unless it is provably clean —
    and the commit lands on main whatever branch the keyboard is on;
  * every banner says what happened to the word, especially when it did not
    move.
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

import pytest

from broker_testlib import (STUB_PUBLISHED, STUB_SHA, FakeBuildProc,
                            build_out)
from brokerd import (NO_HARVEST_REASON, build_outcome_class, parse_spec_status,
                     replace_spec_status, spec_status_after_build)

SLUG = "2026-08-17-status-moves"
SPEC = f"{SLUG}.md"


def _launch(harness, proc_factory=None, *, filename=SPEC, **spec_kw):
    harness.set_verbs(**{"start-build": True})
    harness.use_fake_build(proc_factory=proc_factory)
    harness.write_spec(filename, **spec_kw)
    return harness.call("start-build", {"spec": filename})


def _bodies(harness):
    return [p["body"] for p in harness.proposals]


def _status_lines(harness):
    return [ln for b in _bodies(harness) for ln in b.splitlines()
            if ln.startswith("spec status:")]


# ======================================================================
# 1. THE HAPPY PATH — building, then built@<branch>
# ======================================================================

def test_start_build_stamps_building_before_it_narrates_started(harness):
    proc = FakeBuildProc(out=build_out(), block=True)
    resp = _launch(harness, lambda: proc)
    assert resp["ok"] is True
    try:
        # While the build runs the FILE says so — on main, in a commit.
        assert harness.spec_status_on_main(SPEC) == "building"
        text = harness.spec_text_on_main(SPEC)
        assert f"disjorn-build-{SLUG}.service" in text
        assert f"loop/{SLUG}" in text
        assert "confirmed by plink" in text and "seq 139" in text
        # The confirm record above it is untouched, byte for byte.
        assert "- **Confirmed by**: plink" in text
        assert "- **#custodian seq**: 139" in text
        # The commit is the broker's, on main, and says what it did.
        assert harness.main_log()[0] == f"{SLUG}: Status -> building"
        # The started line carries the result.
        started = [b for b in _bodies(harness) if b.startswith("build started")]
        assert len(started) == 1
        assert "\nspec status: building (commit " in started[0]
        # The result names it too, so a caller can act on a failed stamp.
        st = resp["result"]["spec_status"]
        assert st["ok"] is True and st["status"] == "building"
        assert st["commit"] and st["why"] == ""
    finally:
        proc.release.set()
        harness.broker.join_builds()


def test_a_published_build_lands_on_built_at_branch(harness):
    _launch(harness)
    harness.broker.join_builds()
    assert harness.spec_status_on_main(SPEC) == f"built@loop/{SLUG}"
    text = harness.spec_text_on_main(SPEC)
    assert f"disjorn.git {STUB_SHA}" in text
    assert "board --mark-merged" in text
    assert harness.main_log()[:2] == [f"{SLUG}: Status -> built@loop/{SLUG}",
                                      f"{SLUG}: Status -> building"]
    done = [b for b in _bodies(harness) if b.startswith("build done")]
    assert len(done) == 1
    assert f"\nspec status: built@loop/{SLUG} (commit " in done[0]


def test_the_keyboards_worktree_is_synced_when_it_is_clean(harness):
    """HEAD is main and the file has no local edits: the checkout follows the
    commit, so `git status` in the keyboard's repo stays quiet."""
    _launch(harness)
    harness.broker.join_builds()
    wt = (harness.spec_repo / "SPECS" / SPEC).read_text()
    assert parse_spec_status(wt) == f"built@loop/{SLUG}"
    assert harness._git("status", "--porcelain").strip() == ""


# ======================================================================
# 2. THE OTHER ENDINGS — failed, no-commits, refused, never spawned
# ======================================================================

def test_a_failed_build_stamps_failed_with_the_reason(harness):
    _launch(harness, lambda: FakeBuildProc(out=b"", err=b"boom on line 12", rc=1))
    harness.broker.join_builds()
    assert harness.spec_status_on_main(SPEC) == "failed"
    text = harness.spec_text_on_main(SPEC)
    assert "boom on line 12" in text
    assert "set this back to `confirmed`" in text
    assert any("spec status: failed (commit" in b for b in _bodies(harness))


def test_a_build_that_published_nothing_is_confirmed_again(harness):
    _launch(harness, lambda: FakeBuildProc(
        out=build_out(publish="NO-COMMITS disjorn.git")))
    harness.broker.join_builds()
    assert harness.spec_status_on_main(SPEC) == "confirmed"
    assert "produced no commits" in harness.spec_text_on_main(SPEC)
    # And it is buildable again through the gate — the confirm record stands.
    harness.proposals.clear()
    # (the mirror stand-in still says `confirmed`; the gate reads that.)
    resp = harness.call("start-build", {"spec": SPEC})
    assert resp["ok"] is True, resp
    harness.broker.join_builds()


def test_a_timed_out_build_is_failed(harness):
    _launch(harness, lambda: FakeBuildProc(raise_timeout=True))
    harness.broker.join_builds()
    assert harness.spec_status_on_main(SPEC) == "failed"
    assert "timed out" in harness.spec_text_on_main(SPEC)


def test_a_preflight_refusal_puts_confirmed_back(harness):
    _launch(harness, lambda: FakeBuildProc(
        out=b"", err=b"PREFLIGHT-FAILED: python cannot import chromadb", rc=78))
    harness.broker.join_builds()
    assert harness.spec_status_on_main(SPEC) == "confirmed"
    assert "preflight" in harness.spec_text_on_main(SPEC)
    refused = [b for b in _bodies(harness) if b.startswith("build refused")]
    assert len(refused) == 1 and "spec status: confirmed (commit" in refused[0]


def test_a_launch_that_never_spawned_puts_confirmed_back(harness):
    harness.set_verbs(**{"start-build": True})
    harness.write_spec(SPEC)

    def broken_spawn(argv, *, stdout, stderr):
        raise OSError("no such helper")

    harness.broker._build_spawn = broken_spawn
    resp = harness.call("start-build", {"spec": SPEC})
    assert resp["ok"] is False and resp["error"]["code"] == "exec-failure"
    assert harness.spec_status_on_main(SPEC) == "confirmed"
    assert "launch failed" in harness.spec_text_on_main(SPEC)
    # building, then back: both commits are in the history, honestly.
    assert harness.main_log()[:2] == [f"{SLUG}: Status -> confirmed",
                                      f"{SLUG}: Status -> building"]


# ======================================================================
# 3. THE ADOPTED REAPER stamps the same way
# ======================================================================

def test_an_adopted_build_stamps_its_terminal_word_too(harness):
    # A previous broker stamped `building` and died; the spool has the harvest.
    harness.write_spec(SPEC, status="building")
    out_p, err_p, out_fh, err_fh = harness.broker._open_build_logs(SLUG)
    out_fh.write(build_out())
    harness.broker._close_build_logs(out_fh, err_fh)
    harness.broker._write_build_sidecar(
        {"slug": SLUG, "branch": f"loop/{SLUG}", "confirmed_by": "plink",
         "seq": 139, "resident": "res-test"},
        out_path=out_p, err_path=err_p, timeout=30)
    assert harness.broker.adopt_inflight_builds() == []
    assert harness.spec_status_on_main(SPEC) == f"built@loop/{SLUG}"


# ======================================================================
# 4. NEVER OVERWRITE THE KEYBOARD, NEVER TOUCH ITS DIRTY WORKTREE
# ======================================================================

def test_a_word_the_keyboard_already_moved_is_left_alone(harness):
    """The keyboard superseded the spec while the build ran: the terminal
    stamp expects `building`, finds `superseded`, and says so — loudly, in the
    banner — instead of clobbering a human decision."""
    proc = FakeBuildProc(out=build_out(), block=True)
    _launch(harness, lambda: proc)
    assert harness.spec_status_on_main(SPEC) == "building"
    text = harness.spec_text_on_main(SPEC)
    (harness.spec_repo / "SPECS" / SPEC).write_text(
        replace_spec_status(text, "superseded", "by hand"))
    harness._git("commit", "-q", "-am", "keyboard: superseded")
    proc.release.set()
    harness.broker.join_builds()
    assert harness.spec_status_on_main(SPEC) == "superseded"
    done = [b for b in _bodies(harness) if b.startswith("build done")]
    assert "spec status: NOT updated" in done[0]
    assert "'superseded'" in done[0] and "expected one of ['building']" in done[0]


def test_the_commit_lands_on_main_even_when_the_keyboard_is_elsewhere(harness):
    """Plumbing, not porcelain: the keyboard is on a feature branch with its
    own uncommitted work; main still gets the stamp and nothing of the
    keyboard's is read or written."""
    harness.write_spec(SPEC)
    harness._git("checkout", "-q", "-b", "wip")
    scratch = harness.spec_repo / "scratch.txt"
    scratch.write_text("keyboard mid-thought\n")
    harness.set_verbs(**{"start-build": True})
    harness.use_fake_build()
    resp = harness.call("start-build", {"spec": SPEC})
    assert resp["ok"] is True
    harness.broker.join_builds()
    assert harness.spec_status_on_main(SPEC) == f"built@loop/{SLUG}"
    # The keyboard's world is exactly as it left it.
    assert harness._git("symbolic-ref", "--short", "HEAD").strip() == "wip"
    assert scratch.read_text() == "keyboard mid-thought\n"
    assert parse_spec_status(
        (harness.spec_repo / "SPECS" / SPEC).read_text()) == "confirmed"
    assert harness._git("status", "--porcelain").strip() == "?? scratch.txt"


def test_a_dirty_worktree_file_is_not_touched_but_the_commit_still_lands(harness):
    harness.write_spec(SPEC)
    path = harness.spec_repo / "SPECS" / SPEC
    path.write_text(path.read_text() + "\n## Notes\nkeyboard is editing this\n")
    harness.set_verbs(**{"start-build": True})
    harness.use_fake_build()
    resp = harness.call("start-build", {"spec": SPEC})
    assert resp["ok"] is True
    st = resp["result"]["spec_status"]
    assert st["ok"] is True and "local edits" in st["why"]
    harness.broker.join_builds()
    assert harness.spec_status_on_main(SPEC) == f"built@loop/{SLUG}"
    # The keyboard's edit is intact and its file still carries the OLD word —
    # the diff it will see is the stamp it has to fold in, not a lost edit.
    wt = path.read_text()
    assert "keyboard is editing this" in wt
    assert parse_spec_status(wt) == "confirmed"


# ======================================================================
# 5. THE STAMP IS SAID OUT LOUD WHEN IT CANNOT HAPPEN
# ======================================================================

def test_no_spec_repo_configured_still_builds_but_says_so(harness):
    harness.broker.start_build.pop("spec_repo")
    resp = _launch(harness)
    assert resp["ok"] is True
    st = resp["result"]["spec_status"]
    assert st["ok"] is False and "spec_repo is not configured" in st["why"]
    harness.broker.join_builds()
    lines = _status_lines(harness)
    assert lines and all(ln.startswith("spec status: NOT updated") for ln in lines)
    assert harness.spec_status_on_main(SPEC) == "confirmed"      # untouched


def test_the_mirror_is_fast_forwarded_after_each_stamp(harness):
    """The word has to reach the file the gate and the residents READ, which
    is the mirror; the stamp runs the SAME two argvs refresh-mirror does."""
    _launch(harness)
    harness.broker.join_builds()
    argv = harness.recorded_argv()
    fetches = [a for a in argv if a[:2] == ["fetch", "origin"]]
    merges = [a for a in argv if a[:3] == ["merge", "--ff-only", "origin/main"]]
    # one per stamp: building, then built@
    assert len(fetches) >= 2 and len(merges) >= 2


def test_a_broken_git_never_sinks_the_build(harness):
    harness.broker.commands["spec_repo_git"] = ["/nonexistent/git"]
    resp = _launch(harness)
    assert resp["ok"] is True
    st = resp["result"]["spec_status"]
    assert st["ok"] is False and "git failed to start" in st["why"]
    harness.broker.join_builds()
    assert any(b.startswith("build done") for b in _bodies(harness))


# ======================================================================
# 6. THE GATE reads the words this writes
# ======================================================================

@pytest.mark.parametrize("word", ["building", f"built@loop/{SLUG}", "failed"])
def test_the_gate_refuses_every_word_but_confirmed(harness, word):
    resp = _launch(harness, filename=SPEC, status=word)
    assert resp["ok"] is False and resp["error"]["code"] == "bad-args"
    assert f"spec status is {word!r}" in resp["error"]["message"]


# ======================================================================
# 7. PURE PIECES
# ======================================================================

def test_replace_spec_status_only_touches_the_status_line():
    text = ("# Spec\n\n## Confirm record\n- **Confirmed by**: plink\n"
            "- **#custodian seq**: 139\n\n## Status\n"
            "`confirmed`\n<!-- earlier note -->\n\n## Notes\nkeep me\n")
    out = replace_spec_status(text, "building", "why")
    assert out is not None
    assert parse_spec_status(out) == "building"
    assert out.startswith("# Spec\n\n## Confirm record\n- **Confirmed by**: plink\n")
    assert out.endswith("<!-- earlier note -->\n\n## Notes\nkeep me\n")
    assert "\n## Status\nbuilding\n<!-- why -->\n" in out
    # No parseable Status line -> None, file left alone by callers.
    assert replace_spec_status("# Spec\n\n## Status\n\n## Next\nx\n", "x", "y") is None
    assert replace_spec_status("# Spec\nno status here\n", "x", "y") is None


def test_hostile_build_output_cannot_write_a_status_line():
    """A failure reason comes from the build's own output. `-->` would close
    the comment and put a line of the attacker's choosing where the parser
    reads the status; a newline would do the same. Neither survives."""
    hostile = "boom --> \nconfirmed\n<!-- \n"
    status, comment = spec_status_after_build(
        branch="loop/x", publish={}, unit_reason=hostile)
    assert status == "failed"
    text = ("## Confirm record\n- **Confirmed by**: plink\n"
            "- **#custodian seq**: 1\n\n## Status\nbuilding\n")
    out = replace_spec_status(text, status, comment)
    assert parse_spec_status(out) == "failed"
    assert "-->" not in comment and "\n" not in comment
    # Exactly one status token line + one comment line were written.
    tail = out.split("## Status\n", 1)[1]
    assert tail == f"failed\n<!-- {comment} -->\n"


@pytest.mark.parametrize("publish,unit_reason,expect", [
    ({"published": [("disjorn.git", STUB_SHA)]}, None, "built@loop/x"),
    ({"no_commits": ["disjorn.git"]}, None, "confirmed"),
    ({"published": [("disjorn.git", STUB_SHA)]}, "exit 1: boom", "failed"),
    ({"failed": [("disjorn.git", "rejected")]}, None, "failed"),
    ({}, None, "failed"),
])
def test_status_after_build_follows_the_banner_ladder(publish, unit_reason, expect):
    status, comment = spec_status_after_build(
        branch="loop/x", publish=publish, unit_reason=unit_reason)
    assert status == expect
    verdict = build_outcome_class(publish, unit_reason)
    assert (verdict == "failed") == (status == "failed")
    if not publish and unit_reason is None:
        assert NO_HARVEST_REASON.split(" (")[0][:40] in comment
    if publish.get("published") and unit_reason:
        assert STUB_SHA in comment           # where the work is, even on failure


# ======================================================================
# THE LOCAL COVERAGE RECORD (spec 2026-08-27, confirmed seq 2067)
#
# A stamp is git plumbing straight onto the canonical branch. There is no
# push, so the pre-receive hook never sees it, so it can never have a
# push-log line. The digest used to report exactly that as an uncovered
# commit and assert a cause it had not measured — on the same morning its
# own liveness line said the hook MATCHED, with the count growing by a row
# per build forever. So the actor leaves a positive record instead.
# ======================================================================

def _metrics():
    """The DETECTOR's reader, imported by path. This is the pin that matters:
    the two halves are a writer and a reader of one grammar in two programs,
    and a test that only checked this side's own string would let them drift
    apart the first time either was edited."""
    path = (Path(__file__).resolve().parents[2] / "metrics" / "metrics.py")
    spec = importlib.util.spec_from_file_location("_metrics_for_pin", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_a_stamp_leaves_a_local_stamp_record_naming_the_commit(harness):
    proc = FakeBuildProc(out=build_out(), block=True)
    resp = _launch(harness, lambda: proc)
    try:
        assert resp["ok"] is True
        sha = harness._git("rev-parse", "main").strip()
        lines = harness.local_log_lines()
        assert len(lines) == 1
        fields = lines[0].split()
        assert fields[0] == "LOCAL"
        assert fields[2] == sha          # the FULL sha, not the short one
        assert fields[3] == "local-stamp"
        # A record that lands and a stamp that reports trouble are different
        # things; nothing went wrong here.
        assert resp["result"]["spec_status"]["why"] == ""
    finally:
        proc.release.set()
        harness.broker.join_builds()


def test_the_record_is_exactly_what_the_digest_reads(harness):
    """One grammar, two programs. The reader must classify the line this
    writer just wrote, with no adjustment on either side."""
    M = _metrics()
    proc = FakeBuildProc(out=build_out(), block=True)
    _launch(harness, lambda: proc)
    try:
        sha = harness._git("rev-parse", "main").strip()
        doc = M.parse_local_log(str(harness.local_log))
        assert doc["present"] is True and doc["malformed"] == 0
        assert doc["records"][sha]["outcome"] == M.LOCAL_STAMP
        # And the class the digest would give that commit, with no push line
        # and no declared-local committer to fall back on.
        cls = M.classify_coverage("", [sha], set(), doc["records"], [])
        assert cls[M.LOCAL_STAMP] == [sha]
        assert cls[M.UNEXPLAINED] == []
    finally:
        proc.release.set()
        harness.broker.join_builds()


def test_the_writer_and_the_detector_resolve_the_same_path(harness):
    """PINNED ON BOTH SIDES: `[gate].local_log`, else
    <[gate].canonical_repo>/hooks/disjorn-local-log. Set one side only and the
    broker writes records nobody reads while the digest calls its own stamps
    unexplained."""
    M = _metrics()
    gate = dict(harness.broker.config["gate"])
    detector = M.gate_paths({"gate": {**gate, "mirror": "/nowhere"}})
    assert harness.broker._local_coverage_log() == detector["local_log"]
    assert harness.broker._local_coverage_log() == str(harness.local_log)


def test_the_log_is_append_only_across_stamps(harness):
    """building, then built@<branch>: two commits, two lines, in order. A
    read-modify-write would be a way to lose one."""
    _launch(harness)
    harness.broker.join_builds()
    lines = harness.local_log_lines()
    assert len(lines) == 2
    shas = [ln.split()[2] for ln in lines]
    assert shas[1] == harness._git("rev-parse", "main").strip()
    assert shas[0] == harness._git("rev-parse", "main~1").strip()
    assert all(ln.split()[3] == "local-stamp" for ln in lines)


def test_a_refused_stamp_records_nothing(harness):
    """No commit, no record. A record naming a commit that was never made
    would be a lie of exactly the kind this whole change is against."""
    harness.write_spec(SPEC, status="merged")
    out = harness.broker._stamp_spec_status(SLUG, "building", "on with it",
                                            expect=("confirmed",))
    assert out["ok"] is False and out["commit"] is None
    assert "left as is" in out["why"]
    assert harness.local_log_lines() == []


def test_an_unwritable_log_does_not_sink_a_stamp_that_landed(harness):
    """Fail-open, the hook's reasoning inverted: the commit already exists.
    The stamp reports ok, `why` says the record is missing, and the next
    digest will call that commit unexplained — which is the correct answer
    for a record that was never written."""
    harness.local_log.parent.mkdir(parents=True, exist_ok=True)
    harness.local_log.write_text("", encoding="utf-8")
    harness.local_log.chmod(0o444)
    proc = FakeBuildProc(out=build_out(), block=True)
    try:
        resp = _launch(harness, lambda: proc)
        st = resp["result"]["spec_status"]
        assert st["ok"] is True and st["status"] == "building"
        assert harness.spec_status_on_main(SPEC) == "building"
        assert "coverage record NOT written" in st["why"]
        assert "unexplained" in st["why"]
        assert harness.local_log_lines() == []
    finally:
        proc.release.set()
        harness.broker.join_builds()
        harness.local_log.chmod(0o644)


def test_an_unconfigured_gate_says_so_rather_than_going_quiet(harness):
    """A deployment with no [gate] block writes no records — and says which
    commit it did not name, so the silence is never mistaken for a record."""
    harness.broker.config.pop("gate")
    proc = FakeBuildProc(out=build_out(), block=True)
    try:
        resp = _launch(harness, lambda: proc)
        st = resp["result"]["spec_status"]
        assert st["ok"] is True
        assert "no [gate].local_log" in st["why"]
        assert harness.local_log_lines() == []
    finally:
        proc.release.set()
        harness.broker.join_builds()
