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


# ───────────────────────────────────────────── the ponytail pin is a wall ──

def test_the_vendored_ponytail_matches_its_pin_file():
    """Gable #2339, Claudette #2341: a pin verified by hand is decoration. The
    pin file lists a sha256 per vendored file; every listed file that is not
    marked DROPPED must exist with that hash, every DROPPED file must be
    gone, and nothing unlisted may sit in the vendored tree."""
    import hashlib
    import re
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent / "shelf" / "ponytail"
    pin = (root / "PONYTAIL-PIN").read_text(encoding="utf-8")
    listed = {}
    for m in re.finditer(r"^\s+([0-9a-f]{64})\s+(\S+)(.*)$", pin, re.M):
        listed[m.group(2)] = (m.group(1), "DROPPED" in m.group(3))
    assert listed, "no pinned files parsed from PONYTAIL-PIN"
    present = sorted(str(p.relative_to(root)) for p in root.rglob("*")
                     if p.is_file() and p.name != "PONYTAIL-PIN")
    for rel, (digest, dropped) in listed.items():
        path = root / rel
        if dropped:
            assert not path.exists(), f"{rel} is marked DROPPED but is present"
            continue
        assert path.is_file(), f"{rel} is pinned but missing"
        assert hashlib.sha256(path.read_bytes()).hexdigest() == digest, \
            f"{rel} does not match its pinned sha256"
    unlisted = [p for p in present if p not in listed]
    assert unlisted == [], f"unpinned files in the vendored tree: {unlisted}"
