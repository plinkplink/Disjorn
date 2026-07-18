"""Live-server fixtures for the SDK integration tests.

Spins up a real Disjorn server (server/.venv uvicorn) as a subprocess against
a scratch SQLite DB + data dir, creates two users and one bot via server/cli.py,
and tears everything down (process + scratch dir) at session end.

Run with the server venv (the SDK must be installed into it first):

    server/.venv/bin/pip install -e sdk
    cd sdk && ../server/.venv/bin/python -m pytest tests -v
"""

from __future__ import annotations

import os
import re
import shutil
import socket
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

import httpx
import pytest

SERVER_DIR = Path(__file__).resolve().parents[2] / "server"
PY = SERVER_DIR / ".venv" / "bin" / "python"

USERS = {"alice": "pw-alice-1", "bob": "pw-bob-1"}
BOT_NAME = "echobot"


@dataclass
class LiveServer:
    base_url: str
    api_key: str
    users: dict[str, str]  # username -> password
    bot_name: str


def _free_port() -> int:
    # Scratch server must not collide with the long-lived dev server on 8399.
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _run_cli(args: list[str], env: dict[str, str], input_text: str | None = None) -> str:
    proc = subprocess.run(
        [str(PY), "cli.py", *args],
        cwd=SERVER_DIR,
        env=env,
        input=input_text,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, f"cli.py {args} failed:\n{proc.stdout}\n{proc.stderr}"
    return proc.stdout


@pytest.fixture(scope="session")
def server() -> LiveServer:
    assert PY.exists(), f"server venv python not found at {PY}"
    scratch = Path(tempfile.mkdtemp(prefix="disjorn-sdk-test-"))
    env = {
        **os.environ,
        "DB_PATH": str(scratch / "disjorn.db"),
        "DATA_DIR": str(scratch / "data"),
        "SECRET_KEY": "sdk-test-secret",
        # server/.env carries production values (COOKIE_SECURE=true breaks the
        # plain-http scratch server); env vars override the .env file.
        "COOKIE_SECURE": "false",
    }

    for name, password in USERS.items():
        _run_cli(["create-user", name, "--password-stdin"], env, input_text=password + "\n")
    out = _run_cli(["create-bot", BOT_NAME], env)
    match = re.search(r"^\s+(\S{20,})\s*$", out, re.MULTILINE)
    assert match, f"could not parse API key from create-bot output:\n{out}"
    api_key = match.group(1)

    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"
    log_path = scratch / "server.log"
    log_file = log_path.open("w")
    proc = subprocess.Popen(
        [str(PY), "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(port)],
        cwd=SERVER_DIR,
        env=env,
        stdout=log_file,
        stderr=subprocess.STDOUT,
    )
    try:
        deadline = time.monotonic() + 30
        while True:
            if proc.poll() is not None:
                raise RuntimeError(
                    f"scratch server died on startup:\n{log_path.read_text()}"
                )
            try:
                if httpx.get(base_url + "/healthz", timeout=1).status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            if time.monotonic() > deadline:
                raise RuntimeError(
                    f"scratch server never became healthy:\n{log_path.read_text()}"
                )
            time.sleep(0.2)

        yield LiveServer(base_url=base_url, api_key=api_key, users=USERS, bot_name=BOT_NAME)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        log_file.close()
        shutil.rmtree(scratch, ignore_errors=True)
