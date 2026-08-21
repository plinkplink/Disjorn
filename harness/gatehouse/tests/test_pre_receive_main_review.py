"""Tests for the keyboard-lane pre-receive gate (Plan Room Phase 0).

WHAT IS UNDER TEST AND HOW. These drive a REAL bare repo through REAL
`git push`es, with the hook installed exactly the way the README says to
install it — a symlink in `hooks/` pointing at a deployed copy outside any
working clone. The assertions then read the refs and the push log directly,
never the hook's own report, which is the thing under test.

The three behaviours that matter most, in the order the spec argues for them:

  * the gate REFUSES only when it positively knows both facts (gated lane
    touched, no trailer) — every other outcome is a pass;
  * the gate FAILS OPEN on anything it cannot establish, including a log it
    cannot write, because a gate that wedges the keyboard is worse than the
    disease;
  * the LOG is written before anything else can be believed — a floor, then one
    line per main-push decision, because the digest's uncovered flag has no
    other source.
"""

from __future__ import annotations

import io
import os
import subprocess
from pathlib import Path

import pytest


# --------------------------------------------------------------------------
# A real canonical repo + a real working clone.
# --------------------------------------------------------------------------

ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "keyboard", "GIT_AUTHOR_EMAIL": "plink@example.invalid",
    "GIT_COMMITTER_NAME": "keyboard", "GIT_COMMITTER_EMAIL": "plink@example.invalid",
    "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null",
}


def git(cwd, *args, check=True):
    proc = subprocess.run(["git", *args], cwd=str(cwd), env=ENV,
                          capture_output=True, text=True)
    if check and proc.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed:\n{proc.stderr}")
    return proc


class Lane:
    """canonical bare repo + clone + the installed hook, all in tmp_path."""

    def __init__(self, root: Path, hook_src: Path):
        self.root = root
        self.canonical = root / "disjorn.git"
        self.clone = root / "clone"
        # The DEPLOYED copy (G4): outside every working clone, so a checkout
        # cannot disarm the gate.
        self.deployed_dir = root / "hooks-deployed"
        self.deployed = self.deployed_dir / "pre-receive-main-review"

        git(root, "init", "--bare", "-b", "main", str(self.canonical))
        self.deployed_dir.mkdir()
        self.deployed.write_bytes(hook_src.read_bytes())
        os.chmod(self.deployed, 0o755)
        (self.canonical / "hooks" / "pre-receive").symlink_to(self.deployed)

        git(root, "clone", str(self.canonical), str(self.clone))
        self.write("README.md", "start\n")
        self.commit("initial commit")
        self.push()  # seeds main; see the fixture for the floor

    # -- clone-side helpers -------------------------------------------------

    def write(self, rel: str, text: str) -> None:
        p = self.clone / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
        git(self.clone, "add", rel)

    def commit(self, message: str) -> str:
        git(self.clone, "commit", "-m", message)
        return git(self.clone, "rev-parse", "HEAD").stdout.strip()

    def push(self, refspec: str = "main", check: bool = True):
        return git(self.clone, "push", "origin", refspec, check=check)

    def deliver(self) -> str:
        """Put the clone's HEAD objects in the canonical repo WITHOUT moving
        `main` — a side ref the gate passes untouched. The tests that call
        `decide()` / `run_hook()` directly need the objects to be there, the
        same way they are by the time git runs a real pre-receive hook."""
        git(self.clone, "push", "-f", "origin", "HEAD:refs/heads/staging")
        return git(self.clone, "rev-parse", "HEAD").stdout.strip()

    # -- canonical-side observations ---------------------------------------

    def main_sha(self) -> str:
        p = git(self.root, "--git-dir", str(self.canonical), "rev-parse",
                "refs/heads/main", check=False)
        return p.stdout.strip()

    @property
    def log_path(self) -> Path:
        return self.canonical / "hooks" / "disjorn-push-log"

    def log_lines(self) -> list:
        if not self.log_path.exists():
            return []
        return [ln for ln in self.log_path.read_text().splitlines() if ln.strip()]

    def push_lines(self) -> list:
        return [ln for ln in self.log_lines() if ln.startswith("PUSH ")]

    def genesis_lines(self) -> list:
        return [ln for ln in self.log_lines() if ln.startswith("GENESIS ")]

    def seed(self):
        """The README's install step 2, run as a subprocess — the real CLI."""
        return subprocess.run(
            [str(self.deployed), "--seed-genesis",
             "--git-dir", str(self.canonical)],
            env=ENV, capture_output=True, text=True)


@pytest.fixture()
def lane(tmp_path, hook_path) -> Lane:
    """A lane where nobody ran the install's seed step — so the floor is
    minted LAZILY at the first push. The unhealthy-but-survivable shape, and
    the one this week's install record says to expect."""
    root = tmp_path / "lane"
    root.mkdir()
    ln = Lane(root, hook_path)
    return ln


@pytest.fixture()
def seeded(tmp_path, hook_path) -> Lane:
    """A lane seeded the way the README says, BEFORE any push happens."""
    root = tmp_path / "seeded"
    root.mkdir()
    canonical = root / "disjorn.git"
    git(root, "init", "--bare", "-b", "main", str(canonical))
    # Build the repo first, THEN install the hook, THEN seed — the real order.
    clone = root / "clone"
    git(root, "clone", str(canonical), str(clone))
    (clone / "README.md").write_text("start\n")
    git(clone, "add", "README.md")
    git(clone, "commit", "-m", "initial commit")
    git(clone, "push", "origin", "main")

    deployed_dir = root / "hooks-deployed"
    deployed_dir.mkdir()
    deployed = deployed_dir / "pre-receive-main-review"
    deployed.write_bytes(hook_path.read_bytes())
    os.chmod(deployed, 0o755)
    (canonical / "hooks" / "pre-receive").symlink_to(deployed)

    ln = Lane.__new__(Lane)
    ln.root, ln.canonical, ln.clone = root, canonical, clone
    ln.deployed_dir, ln.deployed = deployed_dir, deployed
    proc = ln.seed()
    assert proc.returncode == 0, proc.stderr
    return ln


# --------------------------------------------------------------------------
# Pure helpers.
# --------------------------------------------------------------------------

def test_find_trailer_reads_both_kinds(hook):
    assert hook.find_trailer("subject\n\nreview-seq: 1428\n") == "review-seq:1428"
    assert hook.find_trailer("subject\n\noverride-seq: 7\n") == "override-seq:7"
    # Case and inner spacing are tolerated; the stored form is normalised.
    assert hook.find_trailer("s\n\nReview-Seq:  1428  ") == "review-seq:1428"


def test_find_trailer_is_a_presence_check_not_a_validator(hook):
    # The hook does NOT know whether 1 is a real seq. That is the digest's job,
    # and the whole reason the digest exists — see G2.
    assert hook.find_trailer("s\n\nreview-seq: 1") == "review-seq:1"


def test_find_trailer_rejects_near_misses(hook):
    assert hook.find_trailer("just a subject") is None
    assert hook.find_trailer("review-seq 1428") is None       # no colon
    assert hook.find_trailer("review-seq: abc") is None       # not a number
    assert hook.find_trailer("see review-seq: 1428 inline") is None
    # Last trailer wins — trailers sit at the bottom of a message.
    assert hook.find_trailer("review-seq: 1\n\noverride-seq: 2") == "override-seq:2"


def test_guarded_hits_covers_the_four_lanes_and_nothing_else(hook):
    hits = hook.guarded_hits([
        "server/app/ws.py", "client/src/a.ts", "sdk/x.py", "harness/metrics/m.py",
        "SPECS/2026-08-20-x.md", "README.md", "notes.txt", "serverless/x.py",
    ])
    assert hits == ["client/src/a.ts", "harness/metrics/m.py",
                    "sdk/x.py", "server/app/ws.py"]


def test_is_zero(hook):
    assert hook.is_zero("0" * 40)
    assert hook.is_zero("0" * 64)
    assert not hook.is_zero("0" * 39 + "1")
    assert not hook.is_zero("")


# --------------------------------------------------------------------------
# The gate itself, through real pushes.
# --------------------------------------------------------------------------

def test_doc_only_push_passes_without_a_trailer(lane):
    lane.write("SPECS/2026-08-20-x.md", "a spec\n")
    sha = lane.commit("spec: a spec")
    assert lane.push().returncode == 0
    assert lane.main_sha() == sha
    assert lane.push_lines()[-1].endswith("NONE passed")


def test_guarded_push_without_a_trailer_is_refused(lane):
    before = lane.main_sha()
    lane.write("harness/metrics/thing.py", "x = 1\n")
    lane.commit("metrics: a thing")
    proc = lane.push(check=False)
    assert proc.returncode != 0
    assert lane.main_sha() == before, "the ref must not have moved"
    assert "REFUSED" in proc.stderr
    assert "review-seq" in proc.stderr and "override-merge" in proc.stderr
    assert lane.push_lines()[-1].endswith("NONE refused")


def test_review_seq_trailer_lets_it_through(lane):
    lane.write("server/app/thing.py", "x = 1\n")
    sha = lane.commit("server: a thing\n\nreview-seq: 1428")
    assert lane.push().returncode == 0
    assert lane.main_sha() == sha
    assert lane.push_lines()[-1].endswith("review-seq:1428 passed")


def test_override_seq_trailer_lets_it_through(lane):
    lane.write("client/src/thing.ts", "export const x = 1;\n")
    sha = lane.commit("client: a thing\n\noverride-seq: 1440")
    assert lane.push().returncode == 0
    assert lane.main_sha() == sha
    assert lane.push_lines()[-1].endswith("override-seq:1440 passed")


def test_non_main_branch_passes_untouched_and_is_not_logged(lane):
    before = len(lane.push_lines())
    git(lane.clone, "checkout", "-b", "loop/2026-08-20-thing")
    lane.write("harness/metrics/other.py", "y = 2\n")
    lane.commit("metrics: on a branch, no trailer")
    assert lane.push("loop/2026-08-20-thing").returncode == 0
    assert len(lane.push_lines()) == before, "only main decisions are logged"


def test_a_five_commit_push_is_one_range_with_the_trailer_on_the_tip(lane):
    """G1b: the digest reads push boundaries from this log, never from
    reachability. One trailer on the tip must produce ONE cited range, not one
    pass and four false violations."""
    old = lane.main_sha()
    for i in range(4):
        lane.write(f"harness/metrics/f{i}.py", f"x = {i}\n")
        lane.commit(f"metrics: step {i}")
    lane.write("harness/metrics/f4.py", "x = 4\n")
    new = lane.commit("metrics: step 4\n\nreview-seq: 1428")
    assert lane.push().returncode == 0
    line = lane.push_lines()[-1]
    assert f"{old}..{new}" in line
    assert line.endswith("review-seq:1428 passed")
    assert len([l for l in lane.push_lines() if f"{old}.." in l]) == 1


def test_mixed_range_with_one_guarded_file_still_needs_a_trailer(lane):
    lane.write("README.md", "docs\n")
    lane.commit("docs: a line")
    lane.write("sdk/thing.py", "x = 1\n")
    lane.commit("sdk: a thing")
    assert lane.push(check=False).returncode != 0
    assert lane.push_lines()[-1].endswith("NONE refused")


def test_refusal_message_names_the_paths(lane):
    lane.write("harness/gatehouse/hooks/x", "x\n")
    lane.commit("gatehouse: x")
    proc = lane.push(check=False)
    assert "harness/gatehouse/hooks/x" in proc.stderr


# --------------------------------------------------------------------------
# Fail-open — the design intent, not an operational note (seq 1380).
# --------------------------------------------------------------------------

def test_an_unreadable_range_fails_open(hook, lane):
    d = hook.decide(str(lane.canonical), "dead" * 10, lane.main_sha())
    assert d["outcome"] == hook.FAILED_OPEN
    assert "cannot read the commit range" in d["why"]


def test_run_hook_allows_the_push_when_it_fails_open(hook, lane, tmp_path):
    out = io.StringIO()
    log = tmp_path / "failopen-log"
    rc = hook.run_hook(f"{'dead' * 10} {lane.main_sha()} refs/heads/main\n",
                       str(lane.canonical), str(log), out=out)
    assert rc == 0, "a gate that cannot tell must never refuse"
    assert "FAILED OPEN" in out.getvalue()
    assert log.read_text().splitlines()[-1].endswith("NONE failed-open")


def test_a_log_that_cannot_be_written_never_blocks_the_push(hook, lane, tmp_path):
    """Writing the line is inside the fail-open envelope."""
    unwritable = tmp_path / "not-a-file"
    unwritable.mkdir()  # a directory where the log should be: every write errors
    out = io.StringIO()
    old = lane.main_sha()
    lane.write("harness/metrics/t.py", "x = 1\n")
    new = lane.commit("metrics: t\n\nreview-seq: 1428")
    lane.deliver()
    rc = hook.run_hook(f"{old} {new} refs/heads/main\n",
                       str(lane.canonical), str(unwritable), out=out)
    assert rc == 0
    assert "PUSH LOG WRITE FAILED" in out.getvalue()
    assert "UNCOVERED" in out.getvalue(), "say what the digest will now report"


def test_a_log_that_cannot_be_written_still_refuses_a_real_violation(hook, lane, tmp_path):
    """Fail-open covers what the hook CANNOT ESTABLISH. It does not convert a
    fact the hook does know — no trailer on a gated range — into a pass."""
    unwritable = tmp_path / "nope"
    unwritable.mkdir()
    old = lane.main_sha()
    lane.write("harness/metrics/t.py", "x = 1\n")
    new = lane.commit("metrics: t")
    lane.deliver()
    rc = hook.run_hook(f"{old} {new} refs/heads/main\n",
                       str(lane.canonical), str(unwritable), out=io.StringIO())
    assert rc == 1


def test_an_unparseable_stdin_line_is_allowed_and_warned(hook, lane, tmp_path):
    out = io.StringIO()
    log = tmp_path / "log"
    rc = hook.run_hook("garbage\n", str(lane.canonical), str(log), out=out)
    assert rc == 0
    assert "unparseable" in out.getvalue()
    assert not log.exists(), "a line that names no ref is not a main decision"


def test_deleting_main_passes_and_is_logged(hook, lane, tmp_path):
    """No range and no head commit means nothing this hook can check — and
    nothing a trailer would make safer. Pass it, but leave the event in the log
    rather than a hole."""
    out = io.StringIO()
    log = tmp_path / "log"
    rc = hook.run_hook(f"{lane.main_sha()} {'0' * 40} refs/heads/main\n",
                       str(lane.canonical), str(log), out=out)
    assert rc == 0
    assert log.read_text().splitlines()[-1].endswith("NONE passed")


def test_creating_main_is_judged_on_the_whole_tree(hook, lane, tmp_path):
    """`old` all-zeros is a ref creation: the range is the entire tree, so a
    tree containing gated lanes needs a trailer like any other push."""
    lane.write("harness/metrics/t.py", "x = 1\n")
    new = lane.commit("metrics: t")
    lane.deliver()
    d = hook.decide(str(lane.canonical), "0" * 40, new)
    assert d["outcome"] == hook.REFUSED
    assert "harness/metrics/t.py" in d["hits"]


# --------------------------------------------------------------------------
# The genesis floor (G1c) and its provenance (G1d).
# --------------------------------------------------------------------------

def test_an_unseeded_log_is_born_lazy_at_the_first_push(lane):
    """The floor's EXISTENCE never depends on a hand step being remembered."""
    gen = lane.genesis_lines()
    assert len(gen) == 1
    kind, ts, sha = gen[0].split()[1], gen[0].split()[2], gen[0].split()[3]
    assert kind == "lazy"
    assert ts.endswith("Z")
    # Minted from the `old` sha of the triggering push — here, main's creation.
    assert set(sha) == {"0"}


def test_the_lazy_floor_is_written_before_the_first_decision_line(lane):
    assert lane.log_lines()[0].startswith("GENESIS lazy ")
    assert lane.log_lines()[1].startswith("PUSH ")


def test_lazy_seeding_warns_loudly(hook, tmp_path, lane):
    out = io.StringIO()
    log = tmp_path / "fresh-log"
    lane.write("SPECS/x.md", "x\n")
    new = lane.commit("spec: x")
    lane.deliver()
    hook.run_hook(f"{lane.main_sha()} {new} refs/heads/main\n",
                  str(lane.canonical), str(log), out=out)
    assert "seeded lazily" in out.getvalue()
    assert "UNVERIFIABLE" in out.getvalue()


def test_seed_genesis_records_mains_head_and_says_seeded(seeded):
    gen = seeded.genesis_lines()
    assert len(gen) == 1
    _, kind, _ts, sha = gen[0].split()
    assert kind == "seeded"
    assert sha == seeded.main_sha()
    assert seeded.push_lines() == [], "the floor predates the first push"


def test_a_seeded_floor_is_not_replaced_by_a_lazy_one(seeded):
    seeded.write("harness/metrics/t.py", "x = 1\n")
    seeded.commit("metrics: t\n\nreview-seq: 1428")
    assert seeded.push().returncode == 0
    assert len(seeded.genesis_lines()) == 1
    assert seeded.genesis_lines()[0].split()[1] == "seeded"


def test_seeding_twice_is_a_no_op(seeded):
    before = seeded.log_lines()
    proc = seeded.seed()
    assert proc.returncode == 0
    assert "already seeded" in proc.stdout + proc.stderr
    assert seeded.log_lines() == before, "never a second genesis line"


def test_seeding_refuses_to_paper_over_a_truncated_log(seeded):
    """A log with content but no genesis line was TRUNCATED. Seeding on top
    would launder that; the digest's TRUNCATED tell must survive."""
    seeded.log_path.write_text("PUSH 2026-08-20T11:00:00Z a..b NONE passed\n")
    proc = seeded.seed()
    assert proc.returncode == 1
    assert "REFUSING to seed" in proc.stdout + proc.stderr
    assert seeded.genesis_lines() == []


def test_the_hook_does_not_prepend_a_floor_to_a_truncated_log(hook, lane, tmp_path):
    log = tmp_path / "truncated"
    log.write_text("PUSH 2026-08-20T11:00:00Z a..b NONE passed\n")
    lane.write("SPECS/y.md", "y\n")
    new = lane.commit("spec: y")
    lane.deliver()
    hook.run_hook(f"{lane.main_sha()} {new} refs/heads/main\n",
                  str(lane.canonical), str(log), out=io.StringIO())
    lines = log.read_text().splitlines()
    assert not any(l.startswith("GENESIS") for l in lines)
    assert len(lines) == 2, "the decision line is still written"


# --------------------------------------------------------------------------
# Log shape — the digest parses these back, so the grammar is pinned here.
# --------------------------------------------------------------------------

def test_push_line_grammar(hook):
    line = hook.format_push("a" * 40, "b" * 40, "review-seq:1428", hook.PASSED,
                            now=None).rstrip("\n")
    kind, ts, rng, trailer, outcome = line.split()
    assert kind == "PUSH"
    assert ts.endswith("Z") and len(ts) == 20
    assert rng == f"{'a' * 40}..{'b' * 40}"
    assert trailer == "review-seq:1428"
    assert outcome == "passed"


def test_push_line_uses_NONE_for_a_missing_trailer(hook):
    assert hook.format_push("a", "b", None, hook.REFUSED).split()[-2] == "NONE"


def test_genesis_line_grammar(hook):
    kind, prov, ts, sha = hook.format_genesis("seeded", "c" * 40).split()
    assert (kind, prov, sha) == ("GENESIS", "seeded", "c" * 40)
    assert ts.endswith("Z")


def test_log_path_default_is_beside_the_hook_in_the_git_dir(hook, monkeypatch):
    monkeypatch.delenv("DISJORN_PUSH_LOG", raising=False)
    assert hook.log_path_for("/var/lib/x/disjorn.git") == \
        "/var/lib/x/disjorn.git/hooks/disjorn-push-log"


def test_log_path_env_override(hook, monkeypatch, tmp_path):
    monkeypatch.setenv("DISJORN_PUSH_LOG", str(tmp_path / "elsewhere"))
    assert hook.log_path_for("/var/lib/x/disjorn.git") == str(tmp_path / "elsewhere")


# --------------------------------------------------------------------------
# Installation shape (G4).
# --------------------------------------------------------------------------

def test_the_installed_hook_is_a_symlink_to_a_copy_outside_every_clone(lane):
    link = lane.canonical / "hooks" / "pre-receive"
    assert link.is_symlink()
    target = Path(os.readlink(link))
    assert target == lane.deployed
    assert lane.clone not in target.parents, \
        "a checkout in a clone would silently disarm the gate (G4)"


def test_the_in_tree_hook_is_executable(hook_path):
    assert os.access(hook_path, os.X_OK), \
        "git will not run a hook it cannot execute, and will not say so"
