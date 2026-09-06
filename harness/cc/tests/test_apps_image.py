"""The seat image's inputs are what the spec says they are.

Two claims: the shelf on disk matches MANIFEST.toml byte for byte (spec §F —
the digests in the spec are the witness, the manifest is the record), and the
house store's ES-module build is current with its classic-script source.
Both run offline; either failing means the image would bake something the
spec did not pin. The Claude Code version pin is asserted in
test_apps_launch.py next to the launcher's other image facts.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
SHELF = REPO / "harness" / "cc" / "shelf"


def test_the_shelf_matches_its_manifest():
    r = subprocess.run(["bash", str(SHELF / "fetch.sh"), "--verify"],
                       capture_output=True, text=True, cwd=REPO)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "shelf verify OK" in r.stdout


@pytest.mark.skipif(shutil.which("node") is None, reason="node not on this host")
def test_the_store_esm_build_is_current():
    r = subprocess.run(["node", str(SHELF / "store" / "build.mjs"), "--check"],
                       capture_output=True, text=True, cwd=REPO)
    assert r.returncode == 0, r.stdout + r.stderr


@pytest.mark.skipif(shutil.which("node") is None, reason="node not on this host")
def test_the_store_tests_pass():
    r = subprocess.run(["node", "--test", str(SHELF / "store" / "test_store.mjs")],
                       capture_output=True, text=True, cwd=REPO)
    assert r.returncode == 0, r.stdout + r.stderr
