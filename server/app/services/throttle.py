"""Sliding-window counters for routes a stranger can call.

Counts live in this process only: a restart forgets them and a second worker
would keep its own. The server runs as a single uvicorn process.
"""

import ipaddress
import math
import time
from collections import deque

# Past this many tracked keys a hit sweeps out the expired ones first.
_SWEEP_AT = 10_000

_limiters: list["Limiter"] = []

_clock = time.monotonic


class Limiter:
    def __init__(self, limit: int, window_seconds: float) -> None:
        self.limit = limit
        self.window = window_seconds
        self._hits: dict[str, deque[float]] = {}
        _limiters.append(self)

    def _expire(self, key: str, now: float) -> deque[float]:
        hits = self._hits.get(key, deque())
        while hits and now - hits[0] >= self.window:
            hits.popleft()
        if not hits:
            self._hits.pop(key, None)
        return hits

    def retry_after(self, key: str) -> int:
        """Whole seconds until `key` may act again; 0 while it is under the limit."""
        now = _clock()
        hits = self._expire(key, now)
        if len(hits) < self.limit:
            return 0
        return max(1, math.ceil(hits[0] + self.window - now))

    def hit(self, key: str) -> None:
        now = _clock()
        if len(self._hits) >= _SWEEP_AT:
            for stale in list(self._hits):
                self._expire(stale, now)
        self._expire(key, now)
        self._hits.setdefault(key, deque()).append(now)

    def clear(self, key: str) -> None:
        self._hits.pop(key, None)

    def forget_one(self, key: str) -> None:
        hits = self._hits.get(key)
        if hits:
            hits.pop()
            if not hits:
                del self._hits[key]


def attempt(checks: list[tuple[Limiter, str]]) -> int:
    """Count one attempt against every (limiter, key), or none if any is full.

    Synchronous on purpose: check and count happen with no await between, so
    a burst of concurrent requests cannot all pass before the first counts.
    Returns the seconds to wait, 0 when the attempt was counted.
    """
    wait = max(limiter.retry_after(key) for limiter, key in checks)
    if not wait:
        for limiter, key in checks:
            limiter.hit(key)
    return wait


def reset_all() -> None:
    for limiter in _limiters:
        limiter._hits.clear()


def client_ip(request) -> str:
    """An IPv6 client is keyed by its /64: one allocation, one address for limits."""
    host = request.client.host if request.client is not None else "unknown"
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return host
    if ip.version == 4:
        return str(ip)
    if ip.ipv4_mapped is not None:
        return str(ip.ipv4_mapped)
    return str(ipaddress.ip_network(f"{ip}/64", strict=False))
