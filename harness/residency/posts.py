"""This adapter's own-sends ledger (SPECS/2026-09-08-gable-cross-channel-context.md
slice A, item 3).

A seat that posted in one room and was next summoned in another had no way to
know its post had landed: the audit line said nothing about it, the ledger did
not exist, and the seat's memory of posting is not evidence. This file holds
what THIS ADAPTER sent — channel, room name, seq, size, time — last 20 kept,
newest last, and the prompt header lists the last five.

It is a convenience, not the wall. Sends from any other path under the same
key never reach it, so it is labelled partial wherever it renders; the audit
line in #custodian (summary.format_summary, `posted #N`) is the record.

Same atomic-write shape as cursor.CursorStore. State file shape (JSON): a list
of ``{"channel_id", "name", "seq", "chars", "utc"}``.
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

__all__ = ["PostLedger", "KEEP"]

KEEP = 20


class PostLedger:
    def __init__(self, path: str, *, keep: int = KEEP) -> None:
        self.path = Path(path)
        self.keep = keep

    def load(self) -> list[dict[str, Any]]:
        try:
            data = json.loads(self.path.read_text())
        except (OSError, json.JSONDecodeError):
            return []
        if not isinstance(data, list):
            return []
        return [d for d in data if isinstance(d, dict)]

    def recent(self, n: int = 5) -> list[dict[str, Any]]:
        return self.load()[-n:]

    def record(self, *, channel_id: int, name: str, seq: Optional[int],
               chars: int, utc: Optional[str] = None) -> None:
        """Append one send and trim to `keep`. A send with no seq (the server
        answered without one) is still recorded — it happened — with the seq
        left null rather than invented."""
        entry = {
            "channel_id": int(channel_id),
            "name": str(name or ""),
            "seq": int(seq) if seq is not None else None,
            "chars": int(chars),
            "utc": utc or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
        posts = self.load()
        posts.append(entry)
        posts = posts[-self.keep:]
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(self.path.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(posts, fh)
            os.replace(tmp, self.path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
