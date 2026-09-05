"""Make harness/metrics/metrics.py importable in the metrics test suite,
independently of any install. No network, no chromadb — the producer parses
JSON-lines directly."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402

import metrics as _M  # noqa: E402

# The suite never runs podman: the image probe is a fixed answer unless a test
# overrides it. Tests that want a different deployed version monkeypatch
# metrics._image_cc_version themselves.
FAKE_CC_VERSION = "2.1.259"


@pytest.fixture(autouse=True)
def _no_podman(monkeypatch):
    monkeypatch.setattr(_M, "_image_cc_version", lambda image: (FAKE_CC_VERSION, ""))
