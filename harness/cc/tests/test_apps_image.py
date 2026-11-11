"""The seat image's inputs are what the spec says they are.

Two claims: the shelf on disk matches MANIFEST.toml byte for byte (spec §F —
the digests in the spec are the witness, the manifest is the record), and the
house store's ES-module build is current with its classic-script source.
Both run offline; either failing means the image would bake something the
spec did not pin. The Claude Code version pin is asserted in
test_apps_launch.py next to the launcher's other image facts. The Phaser slot
and the TypeScript toolchain have their own section at the end.
"""
from __future__ import annotations

import base64
import hashlib
import json
import re
import shutil
import subprocess
import tomllib
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
SHELF = REPO / "harness" / "cc" / "shelf"
CONTAINERFILE = REPO / "harness" / "cc" / "Containerfile.apps"
CONFIG = REPO / "harness" / "cc" / "apps" / "config"


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


# ─────────────────────────────────────────── phaser and the typescript tools ──

BOOT_TEXTURES = ("default", "missing", "white")
PHASER_TYPES = ("phaser/types/phaser.d.ts", "phaser/types/matter.d.ts")


def _phaser_entries() -> dict:
    manifest = tomllib.loads((SHELF / "MANIFEST.toml").read_text(encoding="utf-8"))
    return {e["shelf_path"]: e for e in manifest["file"] if e["package"] == "phaser"}


def _containerfile_args() -> dict:
    return dict(re.findall(r"^ARG (\w+)=(\S+)$",
                           CONTAINERFILE.read_text(encoding="utf-8"), re.M))


def test_phaser_is_pinned_and_nothing_unlisted_sits_in_its_slot():
    entries = _phaser_entries()
    assert set(entries) == {"phaser/phaser.min.js", *PHASER_TYPES}
    for rel, entry in entries.items():
        assert re.fullmatch(r"\d+\.\d+\.\d+", entry["version"]), entry["version"]
        assert entry["tarball_url"].endswith(f"/phaser-{entry['version']}.tgz")
        assert hashlib.sha256((SHELF / rel).read_bytes()).hexdigest() == entry["sha256"], rel
        assert entry["license_shelf_path"] == "phaser/LICENSE-phaser"
    assert "MIT License" in (SHELF / "phaser" / "LICENSE-phaser").read_text(encoding="utf-8")
    on_disk = {p.relative_to(SHELF).as_posix()
               for p in (SHELF / "phaser").rglob("*") if p.is_file()}
    boot = {f"phaser/phaser-boot/{name}.png" for name in BOOT_TEXTURES}
    assert on_disk == set(entries) | boot | {"phaser/LICENSE-phaser"}


def test_the_phaser_boot_textures_are_the_literals_inside_phaser_min_js():
    """Phaser writes two of the three as a shared 32-character PNG header plus
    a tail, so each file is matched as those two halves."""
    js = (SHELF / "phaser" / "phaser.min.js").read_text(encoding="utf-8")
    for name in BOOT_TEXTURES:
        png = (SHELF / "phaser" / "phaser-boot" / f"{name}.png").read_bytes()
        b64 = base64.b64encode(png).decode()
        assert b64[:32] in js and b64[32:] in js, name


def test_the_toolchain_is_pinned_by_version_and_every_download_is_checksummed():
    text = CONTAINERFILE.read_text(encoding="utf-8")
    args = _containerfile_args()
    for name in ("ESBUILD_VERSION", "TYPESCRIPT_VERSION"):
        assert re.fullmatch(r"\d+\.\d+\.\d+", args[name]), name
    adds = re.findall(r"^ADD (\S+) (\S+)$", text, re.M)
    assert len(adds) == 3, adds
    check = re.search(r"^RUN printf .*?\| sha256sum -c -", text, re.M | re.S)
    assert check, "no sha256sum -c over the downloads"
    digest_of = {dest: var for var, dest in re.findall(r'"\$\{(\w+)\}" (\S+)', check.group(0))}
    for url, dest in adds:
        assert url.startswith("https://registry.npmjs.org/"), url
        assert re.search(r"-\$\{(ESBUILD|TYPESCRIPT)_VERSION\}\.tgz$", url), url
        assert re.fullmatch(r"[0-9a-f]{64}", args[digest_of[dest]]), dest
    assert not re.search(r"npm install[^\n]*(esbuild|typescript)", text)


def test_the_builder_may_run_the_two_typescript_tools_and_nothing_wider():
    perms = json.loads((CONFIG / "settings.json").read_text(encoding="utf-8"))["permissions"]
    commands = ("ls", "cat", "head", "tail", "mkdir", "cp", "mv", "rm", "node", "esbuild", "tsc")
    assert {p for p in perms["allow"] if p.startswith("Bash(")} == \
        {f"Bash({c}:*)" for c in commands}
    assert {"Bash(npm:*)", "Bash(npx:*)"} <= set(perms["deny"])


def test_the_brief_and_the_index_describe_what_is_pinned():
    args = _containerfile_args()
    (phaser,) = {e["version"] for e in _phaser_entries().values()}
    brief = (CONFIG / "BUILDER-BRIEF.md").read_text(encoding="utf-8")
    index = (SHELF / "INDEX.md").read_text(encoding="utf-8")
    for doc in (brief, index):
        assert f"Phaser {phaser}" in doc
        assert f"esbuild {args['ESBUILD_VERSION']}" in doc.replace("`", "")
        assert f"TypeScript {args['TYPESCRIPT_VERSION']}" in doc or \
            f"tsc {args['TYPESCRIPT_VERSION']}" in doc
        assert "esbuild src/main.ts --bundle --outfile=app.js" in doc
        assert "tsc --noEmit" in doc
    assert "/shelf/phaser/" in brief
    assert "`npm`" in brief and "`npx`" in brief
    assert "/shelf/phaser/types/phaser.d.ts" in index
    assert "imageLoadType: 'HTMLImageElement'" in index
    for name in BOOT_TEXTURES:
        assert f"vendor/phaser-boot/{name}.png" in index
