"""wake.py — the keyboard's wake command (SPECS/2026-08-25-agentic-residents.md).

What this file can and cannot cover. It CANNOT cover the thing that actually
authorizes a wake: that the connecting uid is plink's, asserted by the kernel
at the broker's socket. That wall is tested where it lives
(harness/broker/tests/test_broker_wake.py) and cannot be tested from here at
all, because an unprivileged test cannot become another uid.

So what is asserted here is the other half — that this command is a plain
protocol client and nothing more: it sends the verb and the two args, it
carries no credential (there is none to carry), and it reports a refusal as a
refusal rather than swallowing it. A wake command that quietly exited 0 on a
denial would be the silence the whole lane exists to kill.
"""

from __future__ import annotations

import importlib.util
import json
import socket
import threading
from pathlib import Path

import pytest

KEYBOARD = Path(__file__).resolve().parent.parent

# Loaded BY PATH, under a name of its own. harness/residency/ ships a `wake`
# module too, and a plain `import wake` resolves to whichever sys.path entry a
# multi-suite pytest run happened to add first — which is how this file passed
# alone and failed in the full run.
_spec = importlib.util.spec_from_file_location(
    "keyboard_wake_cli", KEYBOARD / "wake.py")
wake_cli = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(wake_cli)


class FakeBroker:
    """A unix socket that speaks PROTOCOL.md's one-line request/response and
    records what it was asked."""

    def __init__(self, path: Path, response: dict) -> None:
        self.path = str(path)
        self.response = response
        self.requests: list[dict] = []
        self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._sock.bind(self.path)
        self._sock.listen(4)
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        while True:
            try:
                conn, _ = self._sock.accept()
            except OSError:
                return
            with conn:
                buf = b""
                while b"\n" not in buf:
                    chunk = conn.recv(4096)
                    if not chunk:
                        break
                    buf += chunk
                if buf:
                    self.requests.append(json.loads(buf.split(b"\n", 1)[0]))
                conn.sendall(json.dumps(self.response).encode() + b"\n")

    def close(self) -> None:
        self._sock.close()


@pytest.fixture()
def broker(tmp_path):
    ok = {"ok": True, "verb": "wake",
          "result": {"wake_id": "wake-20260825T120000Z-abc123",
                     "resident": "res-gable", "session_cap_sec": 5400,
                     "grace_sec": 600,
                     "requested_at": "2026-08-25T12:00:00+00:00"}}
    b = FakeBroker(tmp_path / "b.sock", ok)
    yield b
    b.close()


def test_a_wake_sends_the_verb_and_the_task(broker, capsys):
    rc = wake_cli.main(["res-gable", "read the digest", "--socket", broker.path])
    assert rc == 0
    assert broker.requests == [{"verb": "wake",
                                "args": {"resident": "res-gable",
                                         "task": "read the digest"}}]
    out = capsys.readouterr().out
    assert "wake-20260825T120000Z-abc123" in out
    assert "res-gable" in out


def test_the_request_carries_no_credential(broker):
    """There is nothing to carry: the caller's identity is the uid the kernel
    reports, so the body is the task and the seat and nothing else."""
    wake_cli.main(["res-gable", "x", "--socket", broker.path])
    (req,) = broker.requests
    assert set(req) == {"verb", "args"}
    assert set(req["args"]) == {"resident", "task"}


def test_a_long_task_can_come_from_a_file(broker, tmp_path):
    task = tmp_path / "task.md"
    task.write_text("review the branch and say what is wrong with it\n")
    wake_cli.main(["res-gable", "--task-file", str(task),
                   "--socket", broker.path])
    assert "review the branch" in broker.requests[0]["args"]["task"]


def test_a_wake_with_no_task_never_reaches_the_socket(broker, capsys):
    assert wake_cli.main(["res-gable", "--socket", broker.path]) == 2
    assert broker.requests == []
    assert "names the work" in capsys.readouterr().err


def test_a_refusal_is_reported_as_one(tmp_path, capsys):
    refused = {"ok": False, "error": {
        "code": "verb-disabled",
        "message": "res-gable may not wake anyone — the wake caller is "
                   "authenticated by uid at the socket, and no seat is one"}}
    b = FakeBroker(tmp_path / "refuse.sock", refused)
    try:
        rc = wake_cli.main(["res-gable", "x", "--socket", b.path])
    finally:
        b.close()
    assert rc == 4
    assert "may not wake anyone" in capsys.readouterr().err


def test_an_absent_broker_is_a_transport_failure_not_a_wake(tmp_path, capsys):
    rc = wake_cli.main(["res-gable", "x",
                        "--socket", str(tmp_path / "nothing.sock")])
    assert rc == 3
    assert "nothing.sock" in capsys.readouterr().err
