"""The `changed-files` verb, against a real git repo through the real socket.

A reviewing resident reads one file per call and needs a list of what a branch
touched; these tests are about that list being the branch's own work and
nothing else.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import brokerd  # noqa: E402
from broker_testlib import harness  # noqa: E402,F401

VERB = "changed-files"
# The container path res-test's path_map points at tmp_path/"mirror".
REPO = "/opt/disjorn"
GIT_ENV = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}


def git(repo: Path, *args: str) -> str:
    cp = subprocess.run(["git", "-C", str(repo), *args], check=True,
                        capture_output=True, text=True,
                        env={**os.environ, **GIT_ENV})
    return cp.stdout


def commit(repo: Path, message: str) -> str:
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", message)
    return git(repo, "rev-parse", "HEAD").strip()


def write(repo: Path, rel: str, text: str) -> None:
    target = repo / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text)


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    """A real repo at the host path /opt/disjorn maps to, with one commit on
    main."""
    path = tmp_path / "mirror"
    path.mkdir()
    git(path, "init", "-q", "-b", "main")
    write(path, "a.md", "one\n")
    write(path, "gone.txt", "gone\n")
    write(path, "doc/long.txt", "".join(f"line {i}\n" for i in range(40)))
    (path / "bin.dat").write_bytes(b"\x00\x01\x02binary\n")
    commit(path, "init")
    return path


def call(harness, rng: str, repo_arg: str = REPO) -> dict:  # noqa: F811
    harness.set_verbs(**{VERB: True})
    return harness.call(VERB, {"repo": repo_arg, "range": rng})


def by_path(result: dict) -> dict:
    return {entry["path"]: entry for entry in result["files"]}


# ── what a branch did ────────────────────────────────────────────────────

def test_every_status_reports_with_its_counts(harness, repo):  # noqa: F811
    git(repo, "checkout", "-q", "-b", "feature")
    write(repo, "new.txt", "fresh\n")
    write(repo, "a.md", "two\n")
    (repo / "gone.txt").unlink()
    git(repo, "mv", "doc/long.txt", "doc/moved.txt")
    (repo / "bin.dat").write_bytes(b"\x00\x01\x02different\n")
    commit(repo, "feature")

    resp = call(harness, "main..feature")
    assert resp["ok"] is True, resp
    files = by_path(resp["result"])
    assert files["new.txt"] == {"path": "new.txt", "status": "A", "added": 1,
                                "removed": 0, "binary": False}
    assert files["a.md"]["status"] == "M"
    assert (files["a.md"]["added"], files["a.md"]["removed"]) == (1, 1)
    assert files["gone.txt"]["status"] == "D"
    assert files["gone.txt"]["removed"] == 1
    moved = files["doc/moved.txt"]
    assert moved["status"] == "R" and moved["old_path"] == "doc/long.txt"
    assert files["bin.dat"] == {"path": "bin.dat", "status": "M", "added": None,
                                "removed": None, "binary": True}
    assert "doc/long.txt" not in files


def test_totals_count_every_file_and_the_shas_are_resolved(harness, repo):  # noqa: F811
    git(repo, "checkout", "-q", "-b", "feature")
    write(repo, "new.txt", "fresh\n")
    write(repo, "a.md", "two\n")
    commit(repo, "feature")

    result = call(harness, "main..feature")["result"]
    assert result["totals"] == {"files": 2, "added": 2, "removed": 1}
    assert result["truncated"] is False
    assert result["from"] == git(repo, "rev-parse", "main").strip()
    assert result["to"] == git(repo, "rev-parse", "feature").strip()
    assert result["base"] == git(repo, "merge-base", "main", "feature").strip()
    assert harness.audit_lines()[-1]["result_summary"] == (
        "changed-files: 2 files, +2 -1")


def test_two_dot_and_three_dot_both_answer_for_the_branch(harness, repo):  # noqa: F811
    """Main moving under a branch must not put main's own files in a
    reviewer's list."""
    git(repo, "checkout", "-q", "-b", "feature")
    write(repo, "feature.txt", "mine\n")
    commit(repo, "feature")
    git(repo, "checkout", "-q", "main")
    write(repo, "main-only.txt", "theirs\n")
    commit(repo, "main moves")

    two = call(harness, "main..feature")["result"]
    three = call(harness, "main...feature")["result"]
    assert [f["path"] for f in two["files"]] == ["feature.txt"]
    assert two == three
    assert two["base"] != two["from"]


def test_the_cap_cuts_the_list_and_not_the_totals(harness, repo, monkeypatch):  # noqa: F811
    monkeypatch.setattr(brokerd, "MAX_CHANGED_FILES", 2)
    git(repo, "checkout", "-q", "-b", "feature")
    for name in ("f1.txt", "f2.txt", "f3.txt"):
        write(repo, name, "x\n")
    commit(repo, "feature")

    result = call(harness, "main..feature")["result"]
    assert [f["path"] for f in result["files"]] == ["f1.txt", "f2.txt"]
    assert result["truncated"] is True
    assert result["totals"]["files"] == 3
    assert "(truncated)" in harness.audit_lines()[-1]["result_summary"]


def test_a_newline_in_a_filename_is_one_quoted_entry(harness, repo):  # noqa: F811
    """A path that could forge a line in a reviewer's context comes back as a
    repr instead."""
    git(repo, "checkout", "-q", "-b", "feature")
    write(repo, "we\nird.txt", "x\n")
    commit(repo, "feature")

    result = call(harness, "main..feature")["result"]
    assert result["totals"]["files"] == 1
    assert len(result["files"]) == 1
    path = result["files"][0]["path"]
    assert "\n" not in path
    assert path == repr("we\nird.txt")


def test_a_renamed_file_with_a_newline_has_a_quoted_old_path(harness, repo):  # noqa: F811
    write(repo, "we\nird.txt", "one\ntwo\nthree\n")
    commit(repo, "main")
    git(repo, "checkout", "-q", "-b", "feature")
    git(repo, "mv", "we\nird.txt", "ok.txt")
    commit(repo, "feature")

    entry = call(harness, "main..feature")["result"]["files"][0]
    assert entry["status"] == "R" and entry["path"] == "ok.txt"
    assert entry["old_path"] == repr("we\nird.txt")


# ── refusals ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize("rng", ["main", "HEAD", "abc1234"])
def test_a_bare_rev_is_refused(harness, repo, rng):  # noqa: F811
    resp = call(harness, rng)
    assert resp["ok"] is False
    assert resp["error"]["code"] == "bad-args"
    assert "A..B" in resp["error"]["message"]


@pytest.mark.parametrize("rng", ["-rf..main", "main..-O/etc/x", "main...-rf"])
def test_neither_side_may_start_with_a_dash(harness, repo, rng):  # noqa: F811
    resp = call(harness, rng)
    assert resp["ok"] is False
    assert resp["error"]["code"] == "bad-args"


@pytest.mark.parametrize("rng,side", [("main..nope", "right"),
                                      ("nope..main", "left")])
def test_an_unknown_rev_names_its_side(harness, repo, rng, side):  # noqa: F811
    resp = call(harness, rng)
    assert resp["ok"] is False
    assert resp["error"]["code"] == "bad-args"
    assert side in resp["error"]["message"]
    assert "nope" in resp["error"]["message"]


def test_a_repo_outside_the_mapped_roots_is_refused(harness, repo):  # noqa: F811
    resp = call(harness, "main..main", repo_arg="/etc/anything")
    assert resp["ok"] is False
    assert resp["error"]["code"] == "bad-args"
    assert "/opt/disjorn" in resp["error"]["message"]


def test_the_switch_is_the_switch(harness, repo):  # noqa: F811
    harness.set_verbs(**{VERB: False})
    resp = harness.call(VERB, {"repo": REPO, "range": "main..main"})
    assert resp["ok"] is False
    assert resp["error"]["code"] == "verb-disabled"
