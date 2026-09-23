"""The house's one call into the broker (harness/broker/PROTOCOL.md).

One connection, one JSON line out, one line back. The caller is authenticated
by SO_PEERCRED alone — the uid this process runs under IS the authorization,
so there is no token here to pass and none to leak.
"""

import json
import socket
from typing import Any

from starlette.concurrency import run_in_threadpool

from ..config import get_settings

DEFAULT_TIMEOUT_SECONDS = 20

# A request is a verb and a handful of ints; anything past this is not one.
MAX_REQUEST_BYTES = 64 * 1024
MAX_RESPONSE_BYTES = 1024 * 1024

RECV_CHUNK = 65536

# The code a caller sees when the broker never answered, as distinct from a
# refusal it spoke.
TRANSPORT_CODE = "broker-unreachable"


class BrokerError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _exchange(socket_path: str, payload: bytes, timeout: int) -> bytes:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(timeout)
        sock.connect(socket_path)
        sock.sendall(payload)
        chunks: list[bytes] = []
        received = 0
        while True:
            chunk = sock.recv(RECV_CHUNK)
            if not chunk:
                break
            chunks.append(chunk)
            received += len(chunk)
            if chunk.endswith(b"\n") or received > MAX_RESPONSE_BYTES:
                break
    return b"".join(chunks)


async def call_broker(
    verb: str, args: dict[str, Any], *, timeout: int = DEFAULT_TIMEOUT_SECONDS
) -> dict[str, Any]:
    """Speak one verb and return the broker's response object.

    Raises BrokerError on a spoken refusal (``ok`` is not true, and the
    broker's own code and message are carried through untouched) and on every
    transport failure. A caller that reaches the next line has an ``ok``
    response and nothing else.
    """
    payload = (json.dumps({"verb": verb, "args": args}, ensure_ascii=False)
               + "\n").encode("utf-8")
    if len(payload) > MAX_REQUEST_BYTES:
        raise BrokerError(TRANSPORT_CODE, "That request is too large to send.")
    socket_path = get_settings().BROKER_SOCKET_PATH
    try:
        raw = await run_in_threadpool(_exchange, socket_path, payload, timeout)
    except OSError as exc:
        raise BrokerError(
            TRANSPORT_CODE, f"The broker did not answer ({type(exc).__name__})."
        ) from exc
    if not raw:
        raise BrokerError(TRANSPORT_CODE, "The broker closed without answering.")
    try:
        response = json.loads(raw.decode("utf-8", errors="replace"))
    except ValueError as exc:
        raise BrokerError(TRANSPORT_CODE, "The broker's answer was unreadable.") from exc
    if not isinstance(response, dict):
        raise BrokerError(TRANSPORT_CODE, "The broker's answer was unreadable.")
    if response.get("ok") is not True:
        error = response.get("error")
        error = error if isinstance(error, dict) else {}
        raise BrokerError(
            str(error.get("code") or "broker-refused"),
            str(error.get("message") or "The broker refused."),
        )
    return response
