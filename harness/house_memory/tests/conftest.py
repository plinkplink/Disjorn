"""Pytest fixtures for house_memory tests — shared helpers live in
house_memory_testlib (uniquely named so multi-rootdir collection alongside
the broker/classifier suites can't collide on module names).

StubEmbedder only — NO network, NO voyage calls, NO chroma against real
stores (tmp dirs everywhere)."""

import pytest

from house_memory import MemoryStore, StubEmbedder


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
