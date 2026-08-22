"""Make the hook importable in this suite.

The hook is `pre-receive-main-review` — no extension, because git names hooks
by their filename and the deployed copy keeps the name. So it cannot be a plain
`import`; it is loaded from its path, once, and shared.
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
from pathlib import Path

import pytest

HOOK = Path(__file__).resolve().parent.parent / "hooks" / "pre-receive-main-review"


def _load():
    loader = importlib.machinery.SourceFileLoader(
        "pre_receive_main_review", str(HOOK))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


hook_module = _load()


@pytest.fixture()
def hook():
    return hook_module


@pytest.fixture()
def hook_path() -> Path:
    return HOOK
