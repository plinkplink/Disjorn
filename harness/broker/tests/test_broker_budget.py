"""Daily per-resident action budget (WP-H12).

Additive to the WP-H3 broker: a configurable per-resident cap on broker verb
calls. Default OFF; over-budget -> `over-budget` denial, audited like any
other denial. Budgets are set on the live broker instance the harness exposes
(mirrors `broker.toml [budgets]`, which the broker loads at construction).
"""

from __future__ import annotations


def test_default_off_never_denies(harness):
    """No budget configured => the cap check is a no-op, however many calls."""
    harness.set_verbs(**{"read-metrics": True})
    assert harness.broker.budgets == {}
    for _ in range(20):
        assert harness.call("read-metrics", {})["ok"] is True


def test_over_budget_denies_and_audits(harness):
    harness.set_verbs(**{"read-metrics": True})
    harness.broker.budgets = {"res-test": {"daily_action_cap": 3}}
    # Exactly `cap` allowed actions go through...
    for _ in range(3):
        assert harness.call("read-metrics", {})["ok"] is True
    # ...the next is denied with the new error code, and audited allowed=False.
    resp = harness.call("read-metrics", {})
    assert resp["ok"] is False
    assert resp["error"]["code"] == "over-budget"
    last = harness.audit_lines()[-1]
    assert last["allowed"] is False
    assert "budget" in last["result_summary"]
    # Still denied on the following call (denials don't free up room).
    assert harness.call("read-metrics", {})["error"]["code"] == "over-budget"


def test_denials_do_not_count_toward_budget(harness):
    """A resident cannot exhaust its own budget by being refused: only ALLOWED
    actions count. Disabled-verb denials are audited allowed=False and free."""
    harness.set_verbs(**{"read-metrics": True})  # restart-disjorn stays OFF
    harness.broker.budgets = {"res-test": {"daily_action_cap": 2}}
    for _ in range(5):  # five denials (verb disabled) — must not consume budget
        assert harness.call("restart-disjorn", {})["error"]["code"] == "verb-disabled"
    assert harness.call("read-metrics", {})["ok"] is True   # 1st allowed
    assert harness.call("read-metrics", {})["ok"] is True   # 2nd allowed
    assert harness.call("read-metrics", {})["error"]["code"] == "over-budget"


def test_over_budget_does_not_run_the_verb(harness):
    """The denied call must never reach the handler/subprocess."""
    harness.set_verbs(**{"restart-disjorn": True})
    harness.broker.budgets = {"res-test": {"daily_action_cap": 1}}
    assert harness.call("restart-disjorn", {})["ok"] is True
    assert harness.recorded_argv() == [[]]          # ran exactly once
    assert harness.call("restart-disjorn", {})["error"]["code"] == "over-budget"
    assert harness.recorded_argv() == [[]]          # still once — verb not run


def test_default_daily_action_cap_applies_without_override(harness):
    harness.set_verbs(**{"read-metrics": True})
    harness.broker.budgets = {"default_daily_action_cap": 1}
    assert harness.call("read-metrics", {})["ok"] is True
    assert harness.call("read-metrics", {})["error"]["code"] == "over-budget"


def test_per_resident_override_beats_default(harness):
    harness.set_verbs(**{"read-metrics": True})
    harness.broker.budgets = {
        "default_daily_action_cap": 1,
        "res-test": {"daily_action_cap": 4},
    }
    for _ in range(4):
        assert harness.call("read-metrics", {})["ok"] is True
    assert harness.call("read-metrics", {})["error"]["code"] == "over-budget"


def test_budget_template_section_parses_all_off():
    """The shipped broker.toml has a [budgets] table with nothing enabled
    (every cap commented out) — instrument-first default."""
    import tomllib
    from pathlib import Path
    tmpl_dir = Path(__file__).resolve().parent.parent
    with open(tmpl_dir / "broker.toml", "rb") as fh:
        tmpl = tomllib.load(fh)
    budgets = tmpl.get("budgets", {})
    # Present but empty of active caps: no default, no per-resident subtable.
    assert "default_daily_action_cap" not in budgets
    assert all(not isinstance(v, dict) for v in budgets.values())
