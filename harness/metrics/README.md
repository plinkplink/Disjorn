# harness/metrics — resident dashboard producer + budgets (WP-H12)

The action/audit half of the resident metrics surface. Produces the JSON file
the broker's `read-metrics` verb serves, posts the end-of-day #custodian
action-count line, and defines the broker-side daily action budget.

Design law (AGENTHOOD): **instrument first, tune from observed data, never from
imagined abuse.** Every knob here ships OFF/permissive.

## What it produces

`metrics.py build` writes `[paths].metrics_json` (from broker.toml). Shape:

```
{
  "generated_at": ISO8601, "window_days": 7,
  "broker_actions": { "by_resident": { "<r>": {
      total, allowed, denied, by_date{}, by_verb{}, today{},
      budget: { daily_action_cap, used_today, remaining } } } },
  "tool_actions":   { "by_resident": { "<r>": {          # optional (WP-H5 log)
      total, ok, failed, by_date{}, today, distinct_sessions,
      wp5_budget{daily_action_cap, wall_clock_cap_min} } } },
  "retrieval":      { "by_resident": { "<r>": {          # optional (house_memory)
      total_recalls, recalls_in_window, by_date{}, unique_queries,
      distinct_returned_ids, top_referenced[[id,n]] } } },
  "spine":          { "by_resident": { "<r>": { entries, kernel_entries } } }  # optional
}
```

Every input is read-only and config-driven from `broker.toml`. Unconfigured or
unreadable inputs are skipped, never fatal. No chromadb import, no network —
retrieval logs are parsed as plain JSON-lines (house_memory's unified schema).

## CLI

```
metrics.py --config broker.toml build                 # aggregate -> metrics_json
metrics.py --config broker.toml post-daily [--date D] [--no-rebuild]
```

`post-daily` composes one line per resident and posts it to #custodian via the
broker's OWN identity — the exact `_sdk_transport` `file-proposal` uses. It is a
scheduled CLI, never a verb: no resident can trigger it. Timers:
`disjorn-metrics-build.timer` (every 10 min), `disjorn-metrics-daily.timer`
(23:55 UTC). See INTEGRATION-NEEDS.md.

## Broker-side daily action budget

Lives in `broker.toml [budgets]`, enforced in `brokerd.dispatch()`:

```toml
[budgets]
# default_daily_action_cap = 2000
[budgets.res-claudette]
# daily_action_cap = 2000
```

Over the cap → `over-budget` denial (audited like any denial). Counts today's
**allowed** actions from the audit log (restart-proof; denials never count).
Default OFF. Full contract: `harness/broker/PROTOCOL.md` (Daily action budget).

## Wall-clock caps — already WP-H5's

WP-H5's `pre-tool-use.py` enforces the per-session wall-clock cap
(`wall_clock_cap_min` in each container's `/config/budget.json`, against the
session-start file `session-start.py` records) and the per-container daily
tool-action cap. WP-H12 adds **no parallel wall-clock mechanism** — it only
*surfaces* those H5 caps on the dashboard (`tool_actions.wp5_budget`) so they
are legible next to the broker budget. The broker is connectionless and has no
session concept; session wall-clock is correctly H5's job.

## Tests

```
server/.venv/bin/python -m pytest harness/metrics/tests -q      # 14, no network
server/.venv/bin/python -m pytest harness/broker/tests  -q      # 33 incl. budget
```
