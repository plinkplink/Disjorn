"""Sliding-window counters for routes a stranger can call.

Counts live in this process only: a restart forgets them and a second worker
would keep its own. The server runs as a single uvicorn process.
"""

import math
import time
from collections import deque

# Past this many tracked keys a hit sweeps out the expired ones first.
_SWEEP_AT = 10_000

_limiters: list["Limiter"] = []


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
        now = time.monotonic()
        hits = self._expire(key, now)
        if len(hits) < self.limit:
            return 0
        return max(1, math.ceil(hits[0] + self.window - now))

    def hit(self, key: str) -> None:
        now = time.monotonic()
        if len(self._hits) >= _SWEEP_AT:
            for stale in list(self._hits):
                self._expire(stale, now)
        self._expire(key, now)
        self._hits.setdefault(key, deque()).append(now)

    def clear(self, key: str) -> None:
        self._hits.pop(key, None)


def reset_all() -> None:
    for limiter in _limiters:
        limiter._hits.clear()


def client_ip(request) -> str:
    return request.client.host if request.client is not None else "unknown"
