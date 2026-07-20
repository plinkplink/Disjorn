"""Persisted per-channel seq cursor (WP-H9).

The SDK tracks ``last_seen_seq`` per channel in memory and, on *reconnect*,
REST-backfills every known channel from that mark. To make that handoff
survive a daemon *restart* (not just a WS reconnect), the adapter mirrors the
cursor to disk after each handled message and re-seeds the client from disk at
boot via :meth:`DisjornClient.seed_seq`. First reconnect after boot then
backfills exactly the gap the daemon was down for.

State file shape (JSON): ``{"<channel_id>": <seq>, ...}``.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

__all__ = ["CursorStore"]


class CursorStore:
    def __init__(self, path: str) -> None:
        self.path = Path(path)

    def load(self) -> dict[int, int]:
        try:
            data = json.loads(self.path.read_text())
        except (OSError, json.JSONDecodeError):
            return {}
        if not isinstance(data, dict):
            return {}
        out: dict[int, int] = {}
        for k, v in data.items():
            try:
                out[int(k)] = int(v)
            except (TypeError, ValueError):
                continue
        return out

    def save(self, cursor: dict[int, int]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(self.path.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump({str(k): int(v) for k, v in cursor.items()}, fh)
            os.replace(tmp, self.path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
