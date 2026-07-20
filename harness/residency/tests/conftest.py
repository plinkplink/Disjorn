"""Pytest setup for the residency (WP-H9) suite.

Puts the worktree's own disjorn_sdk, the flat residency modules, and the test
lib on sys.path (uniquely-named testlib so multi-rootdir collection with the
other harness suites doesn't collide on `conftest`)."""

import sys
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
RESIDENCY_DIR = TESTS_DIR.parent
REPO = RESIDENCY_DIR.parents[1]

# Prefer the worktree's SDK over any site-packages editable install.
sys.path.insert(0, str(REPO / "sdk"))
sys.path.insert(0, str(RESIDENCY_DIR))
sys.path.insert(0, str(TESTS_DIR))

from residency_testlib import *  # noqa: E402,F401,F403
