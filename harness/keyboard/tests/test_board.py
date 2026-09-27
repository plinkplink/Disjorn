"""board.py against a real gatehouse shelf and a real SPECS/: only a merged
build may read as merged, whatever else sits on the shelf."""

from __future__ import annotations

import importlib.util
import os
import subprocess
from pathlib import Path

import pytest

KEYBOARD = Path(__file__).resolve().parent.parent

_spec = importlib.util.spec_from_file_location("keyboard_board_under_test",
                                               KEYBOARD / "board.py")
board = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(board)

GIT_ENV = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@t", "GIT_CONFIG_GLOBAL": "/dev/null"}
MERGED, UNMERGED = "2099-01-01-landed", "2099-01-02-on-the-shelf"

SPEC = """\
# Spec: {slug}

## Confirm record
- **Confirmed by**: plink
- **#custodian seq**: 1434

## Status
{status}
"""


@pytest.fixture
def specs(tmp_path, monkeypatch):
    bin_ = tmp_path / "bin"
    bin_.mkdir()
    (bin_ / "sudo").write_text('#!/bin/sh\nexec "$@"\n')
    (bin_ / "sudo").chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_}:{os.environ['PATH']}")
    for k, v in GIT_ENV.items():
        monkeypatch.setenv(k, v)

    work = tmp_path / "work"
    work.mkdir()
    git = lambda *a: subprocess.run(["git", "-C", str(work), *a], check=True,
                                    capture_output=True)
    git("init", "-q", "-b", "main")
    git("commit", "-q", "--allow-empty", "-m", "init")
    for slug in (MERGED, UNMERGED):
        git("checkout", "-q", "-b", f"loop/{slug}", "main")
        git("commit", "-q", "--allow-empty", "-m", slug)
    git("checkout", "-q", "main")
    git("merge", "-q", "--ff-only", f"loop/{MERGED}")
    gatehouse = tmp_path / "gatehouse"
    subprocess.run(["git", "clone", "-q", "--bare", str(work),
                    str(gatehouse / "disjorn.git")], check=True, capture_output=True)

    specs = tmp_path / "SPECS"
    specs.mkdir()
    board._broker_load()
    monkeypatch.setattr(board, "REPO", work)
    monkeypatch.setattr(board, "GATEHOUSE", gatehouse)
    monkeypatch.setattr(board, "SPECS", specs)
    for name in ("collect_running_builds", "collect_proposals", "collect_asks_to_plink"):
        monkeypatch.setattr(board, name, lambda: [])
    return specs


def write(specs: Path, slug: str, status: str) -> None:
    (specs / f"{slug}.md").write_text(SPEC.format(slug=slug, status=status))


def rows(b: dict, kind: str) -> list:
    return [r["slug"] for r in b["waiting"] + b["in_flight"] + b["tidy"]
            if r["kind"] == kind]


@pytest.mark.parametrize("status", ["failed", "built@loop/{slug}", "confirmed"])
def test_the_board_calls_merged_exactly_what_mark_merged_would_advance(specs, status):
    for slug in (MERGED, UNMERGED):
        write(specs, slug, status.format(slug=slug))
    b = board.build_board()
    advanced = [c["slug"] for c in board.mark_merged(dry_run=True)]
    assert rows(b, "stale-status") == advanced == [MERGED]


@pytest.mark.parametrize("status,kind", [("failed", "failed-build"),
                                         ("built@loop/{slug}", "built")])
def test_an_unmerged_build_keeps_its_own_row(specs, status, kind):
    write(specs, UNMERGED, status.format(slug=UNMERGED))
    b = board.build_board()
    assert rows(b, kind) == [UNMERGED]
    assert rows(b, "build") == [UNMERGED]


def test_a_confirmed_spec_with_its_build_on_the_shelf_is_not_waiting_to_be_built(specs):
    write(specs, UNMERGED, "confirmed")
    b = board.build_board()
    assert rows(b, "ready") == [] and rows(b, "stale-status") == []
    assert rows(b, "build") == [UNMERGED]
