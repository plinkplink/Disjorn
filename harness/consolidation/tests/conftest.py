"""Pytest fixtures for the consolidation suite.

Shared helpers live in `consolidation_testlib` (uniquely named so multi-rootdir
collection alongside the broker/house_memory/classifier suites can't collide on
module names).

Everything is synthetic: StubEmbedder, tmp dirs, hand-written retrieval logs
and spine files. NO network, NO Voyage, NO real stores, NO broker socket.
"""

import sys
from pathlib import Path

import pytest

# Make the consolidation package importable without installing into the venv.
_PKG_ROOT = Path(__file__).resolve().parent.parent  # harness/consolidation
if str(_PKG_ROOT) not in sys.path:
    sys.path.insert(0, str(_PKG_ROOT))

from house_memory import MemoryStore, RetrievalLog, Spine, StubEmbedder  # noqa: E402


@pytest.fixture
def embedder():
    return StubEmbedder(dim=64)


@pytest.fixture
def store(tmp_path, embedder):
    return MemoryStore(
        data_dir=tmp_path / "chroma",
        collection_name="test_memory",
        embedder=embedder,
    )


@pytest.fixture
def spine_dir(tmp_path):
    d = tmp_path / "spine"
    d.mkdir()
    return d


@pytest.fixture
def spine(spine_dir):
    return Spine(spine_dir)


@pytest.fixture
def log_path(tmp_path):
    return tmp_path / "memory_retrieval.jsonl"


@pytest.fixture
def log(log_path):
    return RetrievalLog(log_path, resident="claudette")
