"""The bot-to-bot hop arbiter, adapter side (spec 2026-08-24).

One shared arbiter — plink's #1625 third-party option — keeps the hop counter,
so both residents' adapters spend against ONE wall rather than one each. This
module is the client: harness/broker/PROTOCOL.md over the broker's unix socket,
one JSON request line, one JSON response line, exactly as the resident CLI
speaks it. Authorization is SO_PEERCRED: the daemon runs host-side as its own
res-* uid, which is the uid the broker maps and verbs.toml keys on.

FAIL-CLOSED, ALWAYS TOWARDS THE NARROW WALL. An unreachable broker, a disabled
verb, a malformed answer — every one of them yields ``chain=False``, which is
rule 1: the summon may still be served, but its reply cannot re-trigger anyone.
The arbiter can only ever WIDEN depth-1 into a work-loop; losing it loses the
work loop and nothing else.
"""

from __future__ import annotations

import json
import logging
import socket
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger("disjorn.residency.hops")

__all__ = ["HopDecision", "HopArbiter"]

VERB = "summon-hop"
MAX_RESPONSE_BYTES = 64 * 1024


@dataclass
class HopDecision:
    """The broker's ruling on one bot-to-bot summon."""

    allowed: bool = True
    chain: bool = False
    reason: str = "no-arbiter"
    refusal: str = ""
    work_item: Optional[str] = None
    count: int = 0
    cap: Optional[int] = None


class HopArbiter:
    def __init__(self, config, *, timeout: Optional[float] = None) -> None:
        self.socket_path = config.socket_path
        self.timeout = timeout if timeout is not None else config.timeout_sec

    @property
    def configured(self) -> bool:
        return bool(self.socket_path)

    def spend(self, *, work_item: Optional[str], summoner: str) -> HopDecision:
        """Ask for one hop on ``work_item``. Never raises."""
        args = {"action": "spend", "summoner": summoner}
        if work_item:
            args["work_item"] = work_item
        result = self._call(args)
        if result is None:
            return HopDecision(reason="arbiter-unreachable", work_item=work_item)
        return HopDecision(
            allowed=bool(result.get("allowed", True)),
            chain=bool(result.get("chain", False)),
            reason=str(result.get("reason") or ""),
            refusal=str(result.get("refusal") or ""),
            work_item=result.get("work_item") or work_item,
            count=int(result.get("count") or 0),
            cap=result.get("cap"),
        )

    def unpark(self, *, work_item: str, by: str, seq: Optional[int]) -> bool:
        """Report the human post that unparks ``work_item``. Never raises.

        The broker takes the report on trust — an adapter is the only thing
        watching the channel — and the 24-hops-per-item-per-UTC-day ceiling is
        what bounds that trust: repeated nudges, real or invented, cannot
        compound into an all-day burn.
        """
        args = {"action": "unpark", "work_item": work_item, "summoner": by}
        if seq is not None:
            args["seq"] = int(seq)
        return self._call(args) is not None

    # ------------------------------------------------------------- socket

    def _call(self, args: dict) -> Optional[dict]:
        if not self.socket_path:
            return None
        req = json.dumps({"verb": VERB, "args": args}, ensure_ascii=False)
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
                s.settimeout(self.timeout)
                s.connect(self.socket_path)
                s.sendall(req.encode() + b"\n")
                buf = b""
                while b"\n" not in buf and len(buf) < MAX_RESPONSE_BYTES:
                    chunk = s.recv(4096)
                    if not chunk:
                        break
                    buf += chunk
            resp = json.loads(buf.split(b"\n", 1)[0] or b"{}")
        except (OSError, ValueError):
            logger.warning("hop arbiter call failed (%s)", args.get("action"),
                           exc_info=True)
            return None
        if not resp.get("ok"):
            logger.warning("hop arbiter refused the call: %s",
                           (resp.get("error") or {}).get("message", "no detail"))
            return None
        result = resp.get("result")
        return result if isinstance(result, dict) else None
