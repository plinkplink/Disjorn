"""Fixtures for WP-H5 broker-CLI tests.

Loads the extensionless `broker` script as a module, and runs the fake
broker (tests/fake_broker.py) in a background thread on a scratch socket.
"""

from __future__ import annotations

import importlib.util
import socket
import sys
import threading
import time
from importlib.machinery import SourceFileLoader
from pathlib import Path

import pytest

CC_DIR = Path(__file__).resolve().parent.parent
BROKER_CLI = CC_DIR / "broker-cli" / "broker"


@pytest.fixture(scope="session")
def broker_cli():
    """The broker CLI, imported as a module (script has no .py extension)."""
    loader = SourceFileLoader("broker_cli", str(BROKER_CLI))
    spec = importlib.util.spec_from_loader("broker_cli", loader)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["broker_cli"] = mod
    loader.exec_module(mod)
    return mod


@pytest.fixture()
def fake_broker(tmp_path):
    """Fake broker on a scratch socket; yields (socket_path, set_denials)."""
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import fake_broker as fb

    sock_path = str(tmp_path / "broker.sock")
    denials: dict = {}
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(sock_path)
    srv.listen(8)
    srv.settimeout(0.2)
    stop = threading.Event()

    def serve():
        while not stop.is_set():
            try:
                conn, _ = srv.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            try:
                fb.handle(conn, denials)
            except OSError:
                pass

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    time.sleep(0.05)
    yield sock_path, denials
    stop.set()
    thread.join(timeout=2)
    srv.close()
