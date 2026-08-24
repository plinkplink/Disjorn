"""The hop-arbiter client (spec 2026-08-24): the wire, and the fail-closed.

The arbiter can only ever WIDEN depth-1 into a work loop, so every way of
losing it — no socket configured, no daemon listening, a disabled verb, a
malformed answer — has to land on the same answer: serve the summon, do not let
the reply re-trigger anyone. These tests speak the real PROTOCOL.md wire to a
socket in tmp_path; no broker, no /run.
"""

import json
import socket
import threading

from config import HopConfig
from hops import HopArbiter


class FakeBrokerSocket:
    """One unix socket, one canned response per connection, requests recorded."""

    def __init__(self, path, response):
        self.path = str(path)
        self.response = response
        self.requests = []
        self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._sock.bind(self.path)
        self._sock.listen(4)
        self._sock.settimeout(5)
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self):
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
                self.requests.append(json.loads(buf.split(b"\n", 1)[0]))
                if self.response is not None:
                    conn.sendall(json.dumps(self.response).encode() + b"\n")

    def close(self):
        self._sock.close()


def _arbiter(path):
    return HopArbiter(HopConfig(socket_path=str(path), timeout_sec=2.0))


def test_spend_speaks_the_protocol_and_reads_the_ruling(tmp_path):
    sock = tmp_path / "b.sock"
    broker = FakeBrokerSocket(sock, {"ok": True, "verb": "summon-hop", "result": {
        "allowed": True, "chain": True, "reason": "hop",
        "work_item": "2026-08-24-x", "count": 2, "cap": 8}})
    try:
        decision = _arbiter(sock).spend(work_item="2026-08-24-x",
                                        summoner="claudette")
    finally:
        broker.close()
    assert broker.requests[0] == {
        "verb": "summon-hop",
        "args": {"action": "spend", "summoner": "claudette",
                 "work_item": "2026-08-24-x"}}
    assert decision.chain is True and decision.count == 2 and decision.cap == 8


def test_a_refusal_carries_the_brokers_own_words(tmp_path):
    sock = tmp_path / "b.sock"
    line = "summon refused: 2026-08-24-x at 8/8 bot hops — parked until a human posts on it"
    broker = FakeBrokerSocket(sock, {"ok": True, "result": {
        "allowed": False, "chain": False, "reason": "parked",
        "refusal": line, "count": 8, "cap": 8}})
    try:
        decision = _arbiter(sock).spend(work_item="2026-08-24-x", summoner="c")
    finally:
        broker.close()
    assert decision.allowed is False and decision.refusal == line


def test_no_socket_configured_is_depth_1(tmp_path):
    arbiter = HopArbiter(HopConfig(socket_path=None))
    assert arbiter.configured is False
    assert arbiter.spend(work_item="2026-08-24-x", summoner="c").chain is False


def test_an_absent_daemon_is_depth_1_not_a_crash(tmp_path):
    arbiter = _arbiter(tmp_path / "nothing-here.sock")
    decision = arbiter.spend(work_item="2026-08-24-x", summoner="c")
    assert decision.allowed is True and decision.chain is False
    assert decision.reason == "arbiter-unreachable"


def test_a_disabled_verb_is_depth_1(tmp_path):
    """verbs.toml is plink's kill switch; flipping summon-hop off must cost the
    work loop and nothing else."""
    sock = tmp_path / "b.sock"
    broker = FakeBrokerSocket(sock, {"ok": False, "error": {
        "code": "verb-disabled", "message": "summon-hop is not enabled"}})
    try:
        decision = _arbiter(sock).spend(work_item="2026-08-24-x", summoner="c")
    finally:
        broker.close()
    assert decision.allowed is True and decision.chain is False


def test_unpark_reports_the_seq_that_makes_it_idempotent(tmp_path):
    sock = tmp_path / "b.sock"
    broker = FakeBrokerSocket(sock, {"ok": True, "result": {"reset": True}})
    try:
        assert _arbiter(sock).unpark(work_item="2026-08-24-x", by="plink",
                                     seq=1811) is True
    finally:
        broker.close()
    assert broker.requests[0]["args"] == {
        "action": "unpark", "work_item": "2026-08-24-x", "summoner": "plink",
        "seq": 1811}
