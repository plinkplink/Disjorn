"""The Containerfile's dependency pins must match house_memory/pyproject.toml.

WHY THIS TEST EXISTS. The image installs chromadb and voyageai so that build
sessions can actually run the house_memory suite (found the hard way on
2026-08-06: two consecutive build sessions wrote correct code and correctly
refused to report a pass, because every test in that package dies during
collection without the chroma stack).

Pinning them in the Containerfile makes a SECOND copy of a version number whose
source of truth is pyproject.toml. This project's most expensive recurring
defect is exactly that shape — a value copied into a second file, where it
quietly stops matching the first and nothing notices. So the copy is allowed,
and this test is the thing that notices.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

CC_DIR = Path(__file__).resolve().parent.parent
CONTAINERFILE = CC_DIR / "Containerfile"
PYPROJECT = CC_DIR.parent / "house_memory" / "pyproject.toml"


def _pyproject_pins() -> dict[str, str]:
    data = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    pins = {}
    for dep in data["project"]["dependencies"]:
        name, _, version = dep.partition("==")
        pins[name.strip()] = version.strip()
    return pins


def _containerfile_pins() -> dict[str, str]:
    """Every `name==version` token in the image's pip install lines."""
    text = CONTAINERFILE.read_text(encoding="utf-8")
    # Only look at real instructions, never the comment block above them —
    # otherwise the prose explaining the pins would satisfy the test.
    installs = [
        ln for ln in text.splitlines()
        if "pip install" in ln or (ln.startswith(" ") and "==" in ln
                                   and not ln.lstrip().startswith("#"))
    ]
    pins = {}
    for line in installs:
        for name, version in re.findall(r"([A-Za-z0-9_.\-]+)==([0-9][\w.\-]*)", line):
            pins[name] = version
    return pins


def test_house_memory_runtime_deps_are_pinned_identically():
    expected = _pyproject_pins()
    actual = _containerfile_pins()
    missing = {k: v for k, v in expected.items() if k not in actual}
    assert not missing, (
        f"Containerfile does not install {sorted(missing)} — build sessions "
        f"cannot import house_memory, so no test in that package can run. "
        f"Add the pin to the pip install line in {CONTAINERFILE}."
    )
    mismatched = {
        k: (expected[k], actual[k]) for k in expected
        if k in actual and expected[k] != actual[k]
    }
    assert not mismatched, (
        f"version drift between pyproject.toml and the Containerfile: "
        f"{mismatched} (pyproject, Containerfile). pyproject.toml is the "
        f"source of truth; update the Containerfile to match and rebuild the "
        f"image with harness/keyboard/07-resident-image.sh."
    )


def test_pytest_is_installed_in_the_image():
    """Rule 4 of the build kernel ('run the tests the spec asks for') is
    unsatisfiable without a test runner. This was true of every spec in the
    repo until 2026-08-06."""
    text = CONTAINERFILE.read_text(encoding="utf-8")
    assert re.search(r"pip install[^\n]*(\\\n[^\n]*)*\bpytest\b", text), (
        "the resident image installs no pytest; a build session cannot run "
        "the tests its spec asks for"
    )
