"""Runs client/tests/approvals.test.mjs, so a client regression fails the gate."""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

CLIENT = Path(__file__).resolve().parents[2] / "client"
NODE_TEST = CLIENT / "tests" / "approvals.test.mjs"


def _toolchain() -> str | None:
    for d in (os.environ.get("DISJORN_CLIENT_NODE_MODULES"),
              str(CLIENT / "node_modules"), "/opt/node_modules"):
        if d and (Path(d) / "esbuild").is_dir():
            return d
    return None


@pytest.mark.skipif(shutil.which("node") is None or _toolchain() is None,
                    reason="no node, or no client node_modules, on this host")
def test_the_approvals_client_answers_and_badges_as_the_spec_says(tmp_path):
    env = {**os.environ, "DISJORN_CLIENT_NODE_MODULES": _toolchain(),
           "TMPDIR": str(tmp_path)}
    r = subprocess.run(["node", "--test", str(NODE_TEST)], capture_output=True,
                       text=True, timeout=180, env=env)
    assert r.returncode == 0, r.stdout[-4000:] + r.stderr[-2000:]
    assert "# pass 10" in r.stdout
