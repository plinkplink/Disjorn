"""Live-server fixtures for the SDK integration tests.

Spins up a real Disjorn server (uvicorn, under the interpreter running the
tests) as a subprocess against a scratch SQLite DB + data dir, creates two users
and one bot via server/cli.py, and tears everything down (process + scratch dir)
at session end.

Run with the server venv, this checkout's SDK first on the path:

    cd sdk && PYTHONPATH=. ../server/.venv/bin/python -m pytest tests -v
"""

from __future__ import annotations

import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

import httpx
import pytest

SERVER_DIR = Path(__file__).resolve().parents[2] / "server"
PY = Path(sys.executable)

USERS = {"alice": "pw-alice-rotated", "bob": "pw-bob-rotated"}
SESSION_COOKIE = "__Host-disjorn_session"
BOT_NAME = "echobot"


@dataclass
class LiveServer:
    base_url: str
    # The one origin in the scratch server's HOUSE_ORIGINS: cookie-bearing
    # writes and the user /ws handshake must carry it.
    origin: str
    api_key: str
    users: dict[str, str]  # username -> password
    bot_name: str
    session_cookie: str = SESSION_COOKIE


def _free_port() -> int:
    # Scratch server must not collide with the long-lived dev server on 8399.
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _run_cli(args: list[str], env: dict[str, str], input_text: str | None = None) -> str:
    proc = subprocess.run(
        [str(PY), str(SERVER_DIR / "cli.py"), *args],
        cwd=env["DATA_DIR"],
        env=env,
        input=input_text,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, f"cli.py {args} failed:\n{proc.stdout}\n{proc.stderr}"
    return proc.stdout


def _rotate(base_url: str, username: str, handover: str, password: str) -> None:
    """Log in on the CLI handover password and change it, as a new user must
    before any other route will answer."""
    with httpx.Client(base_url=base_url, headers={"Origin": base_url}, timeout=10) as c:
        r = c.post("/auth/login", json={"username": username, "password": handover})
        assert r.status_code == 200, r.text
        c.headers["Cookie"] = f"{SESSION_COOKIE}={c.cookies[SESSION_COOKIE]}"
        r = c.post("/auth/password",
                   json={"current_password": handover, "new_password": password})
        assert r.status_code == 200, r.text


@pytest.fixture(scope="session")
def server() -> LiveServer:
    scratch = Path(tempfile.mkdtemp(prefix="disjorn-sdk-test-"))
    (scratch / "data").mkdir()
    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"
    # Every process runs from the scratch dir, so a deploy's server/.env is
    # never in the working directory pydantic-settings reads it from.
    env = {
        **os.environ,
        "DB_PATH": str(scratch / "disjorn.db"),
        "DATA_DIR": str(scratch / "data"),
        "SECRET_KEY": "sdk-test-secret",
        "COOKIE_SECURE": "true",
        "HOUSE_ORIGINS": json.dumps([base_url]),
    }

    for name, password in USERS.items():
        _run_cli(["create-user", name, "--password-stdin"], env,
                 input_text=f"handover-{password}\n")
    out = _run_cli(["create-bot", BOT_NAME], env)
    match = re.search(r"^\s+(\S{20,})\s*$", out, re.MULTILINE)
    assert match, f"could not parse API key from create-bot output:\n{out}"
    api_key = match.group(1)

    log_path = scratch / "server.log"
    log_file = log_path.open("w")
    proc = subprocess.Popen(
        [str(PY), "-m", "uvicorn", "app.main:app", "--app-dir", str(SERVER_DIR),
         "--host", "127.0.0.1", "--port", str(port)],
        cwd=scratch,
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

        for name, password in USERS.items():
            _rotate(base_url, name, f"handover-{password}", password)
        yield LiveServer(base_url=base_url, origin=base_url, api_key=api_key,
                         users=USERS, bot_name=BOT_NAME)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        log_file.close()
        shutil.rmtree(scratch, ignore_errors=True)
