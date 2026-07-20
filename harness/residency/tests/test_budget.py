"""Daily budget: enforcement, persistence, rollover (WP-H9 / WP-H12)."""

import json

from budget import BudgetLedger


def _ledger(tmp_path, cap=3, today="2026-07-20"):
    return BudgetLedger(str(tmp_path / "b.json"), cap, today_fn=lambda: today)


def test_starts_empty(tmp_path):
    led = _ledger(tmp_path)
    assert led.used() == 0
    assert led.remaining() == 3
    assert led.can_spend() is True


def test_spend_increments_and_persists(tmp_path):
    path = tmp_path / "b.json"
    led = BudgetLedger(str(path), 3, today_fn=lambda: "2026-07-20")
    assert led.spend() == 1
    assert led.spend() == 2
    # A fresh ledger over the SAME file sees the persisted count — surviving a
    # daemon restart is the whole point.
    reborn = BudgetLedger(str(path), 3, today_fn=lambda: "2026-07-20")
    assert reborn.used() == 2
    assert reborn.remaining() == 1


def test_enforcement_at_cap(tmp_path):
    led = _ledger(tmp_path, cap=2)
    led.spend()
    led.spend()
    assert led.can_spend() is False
    assert led.remaining() == 0


def test_rollover_on_new_day(tmp_path):
    path = tmp_path / "b.json"
    day = {"v": "2026-07-20"}
    led = BudgetLedger(str(path), 3, today_fn=lambda: day["v"])
    led.spend()
    led.spend()
    assert led.used() == 2
    day["v"] = "2026-07-21"  # next day
    assert led.used() == 0
    assert led.can_spend() is True
    assert led.spend() == 1
    assert json.loads(path.read_text())["date"] == "2026-07-21"


def test_corrupt_state_treated_as_empty(tmp_path):
    path = tmp_path / "b.json"
    path.write_text("not json {{{")
    led = BudgetLedger(str(path), 3, today_fn=lambda: "2026-07-20")
    assert led.used() == 0
    assert led.spend() == 1
