"""Daily summon budget with a persisted counter (WP-H9 / WP-H12).

A summon is an expensive act; the house caps how many happen per day. The
counter is persisted to a JSON file so it survives daemon restarts — you
cannot reset the budget by bouncing the process. It rolls over automatically
when the date changes.

The cap is config (plink-owned, outside the container). Nothing a chat message
says can raise the cap or reset the count: the ledger only ever reads its own
state file and the config-supplied cap.

State file shape::

    {"date": "2026-07-20", "count": 3}
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import tempfile
from pathlib import Path
from typing import Callable

__all__ = ["BudgetLedger"]


def _today() -> str:
    return _dt.date.today().isoformat()


class BudgetLedger:
    def __init__(
        self,
        path: str,
        daily_cap: int,
        *,
        today_fn: Callable[[], str] = _today,
    ) -> None:
        self.path = Path(path)
        self.daily_cap = daily_cap
        self._today_fn = today_fn

    # --------------------------------------------------------------- state io

    def _load(self) -> dict:
        try:
            data = json.loads(self.path.read_text())
        except (OSError, json.JSONDecodeError):
            return {"date": self._today_fn(), "count": 0}
        if not isinstance(data, dict):
            return {"date": self._today_fn(), "count": 0}
        return data

    def _count_today(self, state: dict) -> int:
        """Count for *today*, treating a stale-date file as zero (rollover)."""
        if state.get("date") != self._today_fn():
            return 0
        count = state.get("count", 0)
        return count if isinstance(count, int) and count >= 0 else 0

    def _atomic_write(self, state: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(self.path.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(state, fh)
            os.replace(tmp, self.path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)

    # ----------------------------------------------------------------- public

    def used(self) -> int:
        return self._count_today(self._load())

    def remaining(self) -> int:
        return max(0, self.daily_cap - self.used())

    def can_spend(self) -> bool:
        return self.used() < self.daily_cap

    def spend(self) -> int:
        """Record one summon; returns the new today-count. Persisted atomically.

        Rolls the counter over if the stored date is not today.
        """
        state = self._load()
        count = self._count_today(state) + 1
        new_state = {"date": self._today_fn(), "count": count}
        self._atomic_write(new_state)
        return count
