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


SERVER_REQS = CC_DIR.parent.parent / "server" / "requirements.txt"
IMAGE_SCRIPT = CC_DIR.parent / "keyboard" / "07-resident-image.sh"


def test_image_installs_the_server_stack_from_the_real_requirements_file():
    """A build session that touches server/ must be able to RUN its tests.

    Three builds hit this hole before it was closed: 08-06 (no pytest at all),
    08-12 (zero-diff build), 08-14 (the password build wrote 22 tests and ran
    none — conftest imports aiosqlite). Each time the builder correctly refused
    to pip-install its way to a green result, which is the rule working against
    ground nobody had laid.
    """
    text = CONTAINERFILE.read_text(encoding="utf-8")
    assert "server-requirements.txt" in text, (
        "the image does not install server/requirements.txt, so no build "
        "session can run any test under server/tests/"
    )
    assert re.search(r"pip install[^\n]*(\\\n[^\n]*)*-r\s+\S*server-requirements\.txt",
                     text), "server-requirements.txt is present but never installed"


def test_the_image_script_stages_the_requirements_file_into_the_context():
    """The Containerfile COPYs a file that lives outside its build context, so
    the staging step is load-bearing: without it the build fails outright (good)
    — but this test says WHY, so the next person does not 'fix' it by deleting
    the COPY."""
    script = IMAGE_SCRIPT.read_text(encoding="utf-8")
    assert "server-requirements.txt" in script, (
        f"{IMAGE_SCRIPT.name} no longer stages server/requirements.txt into the "
        f"build context; the Containerfile's COPY will fail"
    )


def test_the_build_context_is_never_the_repo_root():
    """871MB, and it contains server/data — the production database. An image
    must not be able to carry that, and a .containerignore is one forgotten
    line away from letting it."""
    script = IMAGE_SCRIPT.read_text(encoding="utf-8")
    assert not re.search(r'podman build[^\n]*"\$REPO"\s*$', script, re.M), (
        "the resident image is being built with the repo root as its context"
    )


def test_server_requirements_still_names_the_dependency_that_broke_the_builds():
    """Not a pin check — a canary. If aiosqlite ever leaves this file while
    server/tests/conftest.py still imports it, builds go back to 'tests: NOT
    RUN' and the reason will not be obvious."""
    assert SERVER_REQS.exists()
    assert "aiosqlite" in SERVER_REQS.read_text(encoding="utf-8")


def test_pytest_is_installed_in_the_image():
    """Rule 4 of the build kernel ('run the tests the spec asks for') is
    unsatisfiable without a test runner. This was true of every spec in the
    repo until 2026-08-06."""
    text = CONTAINERFILE.read_text(encoding="utf-8")
    assert re.search(r"pip install[^\n]*(\\\n[^\n]*)*\bpytest\b", text), (
        "the resident image installs no pytest; a build session cannot run "
        "the tests its spec asks for"
    )
