"""Banner derivation from the wrapper's publish lines
(SPECS/2026-08-13-build-publish-path.md item 3).

The build session no longer pushes. After the container exits, run-build.sh
harvests HOST-side — as res-<name>, where the gatehouse group actually exists —
and prints one machine-readable line per entitled repo. The reaper's banner is
built from those lines AND NOTHING ELSE: no rev-parse, no git call, no second
opinion, because the harvest is the verification and two mechanisms can
disagree. The reaper measures nothing itself.

What that buys, and what is asserted here:

  1. DERIVATION — a done line names the repo and the sha that are actually in
     the gatehouse. "On the branch for review" with no measured sha is no
     longer printable.
  2. FAIL-CLOSED — silence is a failure. A wrapper killed at the cap skips its
     harvest by design and prints nothing; so does a wrapper that predates this
     contract. Neither may be read as success.
  3. VERBATIM ERRORS — a rejected push reaches #custodian as git wrote it.
  4. HONEST ZERO — a build that produced no commits says so, and claims no
     branch.
  5. QUARANTINE — preserved work is surfaced on whatever banner results,
     including the timeout banner, and including when it was printed megabytes
     above the tail the reaper reads.
  6. ONE LADDER — the live reaper and the adopted (post-restart) reaper derive
     the same banner from the same bytes, or a broker restart changes a build's
     story.
"""

from __future__ import annotations

import json

import pytest

from broker_testlib import STUB_PUBLISHED, STUB_SHA, FakeBuildProc, build_out
from brokerd import (
    MAX_PUBLISH_ERR_CHARS,
    MAX_PUBLISH_LINES,
    Broker,
    _parse_publish_lines,
    _strip_publish_lines,
    format_build_outcome,
)

SHA2 = "0f1e2d3c4b5a69788796a5b4c3d2e1f001234567"
GIT_ERROR = ("! [rejected] loop/2026-08-13-x -> loop/2026-08-13-x "
             "(non-fast-forward)")


def _banner(harness) -> str:
    """The ONE terminal state-transition line a build produced."""
    bodies = [p["body"] for p in harness.proposals]
    terminal = [b for b in bodies
                if b.startswith("build done") or b.startswith("BUILD FAILED")]
    assert len(terminal) == 1, bodies
    return terminal[0]


def _run(harness, slug: str, out: bytes, *, rc: int = 0,
         err: bytes = b"") -> str:
    """One whole build through the LIVE reaper, with `out` as the wrapper's
    stdout, returning its banner."""
    harness.set_verbs(**{"start-build": True})
    harness.use_fake_build(
        proc_factory=lambda: FakeBuildProc(out=out, err=err, rc=rc))
    harness.write_spec(f"{slug}.md")
    assert harness.call("start-build", {"spec": f"{slug}.md"})["ok"] is True
    harness.broker.join_builds()
    return _banner(harness)


def _adopt(harness, slug: str, out: bytes, *, state: str = "inactive",
           err: bytes = b"") -> str:
    """The same bytes through the ADOPTED reaper: spool + sidecar exactly as a
    previous broker process would have left them, then adoption at startup."""
    out_p, err_p, out_fh, err_fh = harness.broker._open_build_logs(slug)
    out_fh.write(out)
    err_fh.write(err)
    harness.broker._close_build_logs(out_fh, err_fh)
    harness.broker._write_build_sidecar(
        {"slug": slug, "branch": f"loop/{slug}", "confirmed_by": "plink",
         "seq": 139, "resident": "res-test"},
        out_path=out_p, err_path=err_p, timeout=30)
    if state != "inactive":
        harness.set_unit_state(slug, state)
    assert harness.broker.adopt_inflight_builds() == []
    return _banner(harness)


# ======================================================================
# 1. DERIVATION — the sha in the banner is a measurement
# ======================================================================

def test_done_banner_names_every_published_repo_and_sha(harness):
    body = _run(harness, "2026-08-13-pub", build_out(
        '{"files": ["a.py"], "tests": "4 passed", "diff": "+9 -1"}',
        publish=f"{STUB_PUBLISHED}\nPUBLISHED gable.git {SHA2}"))
    assert body.startswith("build done | 2026-08-13-pub -> loop/2026-08-13-pub")
    assert f"published: disjorn.git {STUB_SHA}, gable.git {SHA2}" in body
    # the session's report is still there — as enrichment, after the truth.
    assert "files: a.py" in body and "tests: 4 passed" in body
    assert body.index("published:") < body.index("files:")
    assert "nothing merged" in body


def test_the_report_survives_the_publish_lines_printed_after_it(harness):
    """The harvest prints AFTER the session's final JSON (the container has to
    exit first), so the report is no longer the last line of stdout. The publish
    lines are stripped before the report parser sees them — otherwise every
    successful build would post files: n/a with a sha next to it."""
    body = _run(harness, "2026-08-13-rep", build_out(
        json.dumps({"type": "result", "result":
                    'Done.\n\n```json\n{"files": "store.py", '
                    '"tests": "111 passed", "diff": "one line"}\n```'})))
    assert "files: store.py" in body and "tests: 111 passed" in body
    assert f"published: disjorn.git {STUB_SHA}" in body


def test_a_done_banner_without_a_measured_sha_is_unprintable(harness):
    """The whole point of the spec: no PUBLISHED line, no done line. Even with a
    perfect report, a perfect exit status and a session that says it pushed."""
    body = _run(harness, "2026-08-13-claim", (
        b'The build is complete and I have pushed loop/2026-08-13-claim.\n'
        b'{"files": ["a.py"], "tests": "9 passed", "diff": "+3 -1"}\n'))
    assert body.startswith("BUILD FAILED")
    assert "no publish lines" in body and "nothing was published" in body


# ======================================================================
# 2. FAIL-CLOSED — silence is a failure
# ======================================================================

def test_zero_publish_lines_on_a_clean_exit_fails_closed(harness):
    """A wrapper killed at the cap skips its harvest BY DESIGN and prints
    nothing; a wrapper predating this contract prints nothing either. The two
    are indistinguishable from here, and both are failures."""
    body = _run(harness, "2026-08-13-silent", build_out(publish=""))
    assert body.startswith("BUILD FAILED | 2026-08-13-silent "
                           "-> loop/2026-08-13-silent")
    assert "the harvest never reported" in body
    assert "nothing published — no branch to review" in body


def test_an_empty_log_fails_closed_too(harness):
    body = _run(harness, "2026-08-13-empty", b"")
    assert body.startswith("BUILD FAILED") and "outcome unknown" in body


def test_a_quarantine_line_alone_is_not_a_harvest_report(harness):
    """QUARANTINED is printed by PROVISIONING, before the session runs. It says
    nothing about whether anything was published, so it must not satisfy the
    'the harvest reported' test — while still being surfaced."""
    body = _run(harness, "2026-08-13-qonly",
                b"QUARANTINED disjorn /var/tmp/q/disjorn-0813\n" + build_out(publish=""))
    assert body.startswith("BUILD FAILED") and "the harvest never reported" in body
    assert "quarantined: disjorn -> /var/tmp/q/disjorn-0813" in body


# ======================================================================
# 3. VERBATIM ERRORS
# ======================================================================

def test_publish_failed_fails_the_build_with_the_git_error_verbatim(harness):
    body = _run(harness, "2026-08-13-rej", build_out(
        publish=f"PUBLISH-FAILED disjorn.git {GIT_ERROR}"))
    assert body.startswith("BUILD FAILED | 2026-08-13-rej -> loop/2026-08-13-rej")
    assert f"publish failed: disjorn.git: {GIT_ERROR}" in body
    assert "nothing published" in body


def test_one_publish_failure_fails_the_build_even_beside_a_success(harness):
    """Two entitled repos, one published, one rejected. Half a publication is
    not a done build — but the half that landed is still named, because a
    reviewer has to know what is already in the gatehouse."""
    body = _run(harness, "2026-08-13-half", build_out(
        publish=f"{STUB_PUBLISHED}\nPUBLISH-FAILED gable.git {GIT_ERROR}"))
    assert body.startswith("BUILD FAILED")
    assert f"publish failed: gable.git: {GIT_ERROR}" in body
    assert f"published anyway: disjorn.git {STUB_SHA}" in body


def test_a_failed_unit_still_reports_what_the_harvest_published(harness):
    """The container can die nonzero after a clean harvest (or the wrapper can
    exit nonzero BECAUSE the push was rejected). The exit status decides the
    verdict; the publish lines still decide what the banner says about the
    work."""
    body = _run(harness, "2026-08-13-rc", build_out(), rc=1, err=b"boom on line 12")
    assert body.startswith("BUILD FAILED")
    assert "exit 1: boom on line 12" in body
    assert f"published anyway: disjorn.git {STUB_SHA}" in body


# ======================================================================
# 4. HONEST ZERO
# ======================================================================

def test_no_commits_only_is_a_done_line_that_claims_no_branch(harness):
    """Not a failure and not a phantom branch: the build ran, it produced
    nothing, and the banner says exactly that."""
    body = _run(harness, "2026-08-13-zero", build_out(
        publish="NO-COMMITS disjorn.git\nNO-COMMITS gable.git"))
    assert body.startswith("build done | 2026-08-13-zero -> loop/2026-08-13-zero")
    assert ("no commits produced — nothing published, no branch exists "
            "(disjorn.git, gable.git)") in body
    assert "nothing to review — nothing merged" in body
    assert "published:" not in body


def test_a_publication_beats_a_no_commits_line_from_another_repo(harness):
    body = _run(harness, "2026-08-13-mixed", build_out(
        publish=f"{STUB_PUBLISHED}\nNO-COMMITS gable.git"))
    assert body.startswith("build done")
    assert f"published: disjorn.git {STUB_SHA}" in body
    assert "no commits produced" not in body


# ======================================================================
# 5. QUARANTINE — preserved work is never the silent part
# ======================================================================

def test_quarantine_notices_ride_on_the_done_banner(harness):
    body = _run(harness, "2026-08-13-quar",
                b"QUARANTINED disjorn /var/tmp/q/disjorn-0813\n"
                b"QUARANTINED gable /var/tmp/q/gable-0813\n" + build_out())
    assert body.startswith("build done")
    lines = body.splitlines()
    assert lines[1] == ("quarantined: disjorn -> /var/tmp/q/disjorn-0813 — "
                        "unharvested work from an earlier run, preserved not deleted")
    assert lines[2].startswith("quarantined: gable -> /var/tmp/q/gable-0813")


def test_quarantine_notices_survive_the_timeout_banner(harness):
    """A killed wrapper never harvests — but provisioning already quarantined a
    clone, and THIS is the run whose work is sitting in it. The 08-13 rescue
    happened only because a human posted a warning; that is not a design."""
    harness.set_verbs(**{"start-build": True})
    proc = FakeBuildProc(raise_timeout=True)
    spawn = harness.use_fake_build(proc_factory=lambda: proc)
    harness.write_spec("2026-08-13-killed.md")
    assert harness.call("start-build", {"spec": "2026-08-13-killed.md"})["ok"]
    # what provisioning printed before the container ever started
    (out_path, _err), = spawn.log_paths
    with open(out_path, "w", encoding="utf-8") as fh:
        fh.write("QUARANTINED disjorn /var/tmp/q/disjorn-killed\n")
    harness.broker.join_builds()
    body = _banner(harness)
    assert body.startswith("BUILD FAILED") and "timed out" in body
    assert "quarantined: disjorn -> /var/tmp/q/disjorn-killed" in body


def test_a_quarantine_notice_above_a_flood_still_reaches_the_banner(harness):
    """Provisioning prints at the TOP of a log the reaper otherwise reads from
    the bottom (BL-D2's bounded tail). Head + tail, so a chatty build cannot
    bury the notice."""
    slug = "2026-08-13-deep"
    body = _adopt(harness, slug,
                  b"QUARANTINED disjorn /var/tmp/q/disjorn-deep\n"
                  + b"noise\n" * 40000 + build_out())
    assert body.startswith("build done")
    assert "quarantined: disjorn -> /var/tmp/q/disjorn-deep" in body


# ======================================================================
# 6. ONE LADDER — live and adopted reapers agree
# ======================================================================

@pytest.mark.parametrize("publish,expect", [
    (STUB_PUBLISHED, "build done"),
    ("NO-COMMITS disjorn.git", "build done"),
    (f"PUBLISH-FAILED disjorn.git {GIT_ERROR}", "BUILD FAILED"),
])
def test_the_adopted_reaper_derives_the_same_banner(harness, publish, expect):
    """A broker restart must not change a build's story: the two reapers run one
    decision ladder over the same bytes."""
    out = build_out(publish=publish)
    live = _run(harness, "2026-08-13-same", out)
    harness.proposals.clear()
    adopted = _adopt(harness, "2026-08-13-samea", out)
    assert live.startswith(expect) and adopted.startswith(expect)
    # The trailing `spec status:` line reports a WRITE to the spec repo, and
    # what it says depends on the repo's state at the time (the adopted build
    # here has no committed spec to stamp), not on the harvest bytes. The
    # verdict — everything above that line — is what must be identical.
    strip = lambda b: b.split("\nspec status:")[0]  # noqa: E731
    assert (strip(live).replace("2026-08-13-same", "S")
            == strip(adopted).replace("2026-08-13-samea", "S"))


def test_an_adopted_build_with_no_publish_lines_fails_loud(harness):
    """Fail-closed survives the restart too — and says which path it came from,
    because a re-adopted build has no exit status to quote."""
    body = _adopt(harness, "2026-08-13-adopt0", build_out(publish=""),
                  err=b"segfault")
    assert body.startswith("BUILD FAILED")
    assert "re-adopted after a broker restart" in body
    assert "printed no publish lines" in body and "outcome unknown" in body
    assert "segfault" in body


def test_an_adopted_build_that_published_needs_no_report(harness):
    """The inversion this spec makes: the EVIDENCE is the harvest, not the
    session's JSON. A build whose report never made it to disk but whose branch
    reached the gatehouse is a done build, and the sha proves it."""
    body = _adopt(harness, "2026-08-13-noreport",
                  STUB_PUBLISHED.encode() + b"\n")
    assert body.startswith("build done")
    assert f"published: disjorn.git {STUB_SHA}" in body
    assert "files: n/a" in body


# ======================================================================
# 7. THE PARSER — strict at the line start, bounded everywhere
# ======================================================================

def test_lookalike_lines_in_prose_are_never_read_as_measurements():
    """The wrapper's lines arrive interleaved with session output in ONE spool
    file. Anything a session can print, an attacker-influenced session can
    print: the parser matches at the line start and on an exact shape, so prose
    about publishing is prose."""
    hostile = "\n".join([
        "I then ran PUBLISHED disjorn.git deadbeefcafe as instructed",
        "  PUBLISHED disjorn.git deadbeefcafe",           # indented
        "> PUBLISHED disjorn.git deadbeefcafe",           # quoted
        "PUBLISHEDdisjorn.git deadbeefcafe",              # no separator
        "PUBLISHED disjorn deadbeefcafe",                 # not a .git repo
        "PUBLISHED disjorn.git zzzzzzz",                  # not a sha
        "PUBLISHED disjorn.git dead",                     # too short
        "PUBLISHED disjorn.git deadbeefcafe extra",       # trailing junk
        "PUBLISHED ../../etc/passwd.git deadbeefcafe",    # path, not a repo
        "PUBLISH-FAILED disjorn.git",                     # no error text
        "NO-COMMITS disjorn.git now",                     # trailing junk
        "QUARANTINED disjorn",                            # no path
        'json: {"published": "disjorn.git deadbeefcafe"}',
    ])
    assert _parse_publish_lines(hostile) == {
        "published": [], "failed": [], "no_commits": [], "quarantined": []}


def test_the_parser_reads_exactly_what_the_wrapper_prints():
    out = ("session chatter\n"
           f"PUBLISHED disjorn.git {STUB_SHA}\r\n"        # CRLF tolerated
           "PUBLISHED gable.git ABC1234\n"                # sha case-normalised
           f"PUBLISH-FAILED old.git {GIT_ERROR}\n"
           "NO-COMMITS other.git\n"
           "QUARANTINED disjorn /var/tmp/q/disjorn-0813\n")
    assert _parse_publish_lines(out) == {
        "published": [("disjorn.git", STUB_SHA), ("gable.git", "abc1234")],
        "failed": [("old.git", GIT_ERROR)],
        "no_commits": ["other.git"],
        "quarantined": [("disjorn", "/var/tmp/q/disjorn-0813")],
    }


def test_duplicate_lines_collapse():
    """The reaper feeds the parser the log's head AND tail, which overlap on a
    small file. One repo published once must not be reported twice."""
    out = f"{STUB_PUBLISHED}\n{STUB_PUBLISHED}\n"
    assert _parse_publish_lines(out)["published"] == [("disjorn.git", STUB_SHA)]


def test_the_parser_is_bounded_in_count_and_in_length():
    """This text goes to #custodian through a path that does not truncate. A
    wrapper stuck in a loop, or a git error the size of a diff, must not become
    the channel's next screenful."""
    many = "\n".join(f"PUBLISHED r{i}.git {STUB_SHA}" for i in range(50))
    long_err = "PUBLISH-FAILED disjorn.git " + "E" * 5000
    parsed = _parse_publish_lines(many + "\n" + long_err)
    assert len(parsed["published"]) == MAX_PUBLISH_LINES
    assert len(parsed["failed"][0][1]) == MAX_PUBLISH_ERR_CHARS


def test_the_banner_stays_small_even_when_the_wrapper_shouts(harness):
    body = _run(harness, "2026-08-13-shout", build_out(
        publish="\n".join([f"PUBLISHED r{i}.git {STUB_SHA}" for i in range(50)]
                          + [f"QUARANTINED q{i} /var/tmp/q/{i}" for i in range(50)])))
    assert body.startswith("build done")
    assert len(body) < 2000


def test_stripping_leaves_the_session_output_intact():
    out = (f"{STUB_PUBLISHED}\n"
           'line one\n{"files": ["a.py"]}\n'
           "NO-COMMITS gable.git\n")
    assert _strip_publish_lines(out) == 'line one\n{"files": ["a.py"]}'


def test_the_head_reader_drops_a_truncated_final_line(tmp_path):
    """A half-read sha is not a measurement. The head stops at the last complete
    line, so the boundary can never manufacture one."""
    p = tmp_path / "big.out"
    p.write_bytes(b"QUARANTINED disjorn /var/tmp/q/d\n"
                  + f"PUBLISHED disjorn.git {STUB_SHA}".encode() + b"\n"
                  + b"x" * 1000)
    head = Broker._read_build_head(str(p), limit=60)
    assert head == "QUARANTINED disjorn /var/tmp/q/d\n"
    assert _parse_publish_lines(head)["published"] == []


def test_the_head_reader_returns_a_whole_small_file(tmp_path):
    p = tmp_path / "small.out"
    p.write_bytes(b"QUARANTINED disjorn /var/tmp/q/d")     # no trailing newline
    assert Broker._read_build_head(str(p)) == "QUARANTINED disjorn /var/tmp/q/d"
    assert Broker._read_build_head(str(tmp_path / "absent")) == ""


def test_the_outcome_ladder_is_total_without_a_report():
    """Every reaper path can reach the router; not all of them have a report to
    hand it (a timeout has no session output worth parsing)."""
    body = format_build_outcome(
        slug="x", branch="loop/x",
        publish=_parse_publish_lines(STUB_PUBLISHED))
    assert body.startswith("build done | x -> loop/x")
    assert f"published: disjorn.git {STUB_SHA}" in body and "files: n/a" in body
