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
(00:05 UTC, reporting the PREVIOUS COMPLETE day). See INTEGRATION-NEEDS.md.

**Why 00:05-for-yesterday and not 23:55-for-today.** The old schedule left the
last five minutes of every day in no digest: those events carry today's date,
but today's digest has already posted and tomorrow's reports tomorrow. That
window held 12 of 103 audit events — 34x over-represented — because it was not
random traffic. It was Claudette pulling her own audit seconds after the digest
posted, checking the number. The blind spot sat exactly over the resident
auditing the ledger. Ruled 2026-08-04.

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

## The GATE DRIFT block — keyboard-lane detector of record (Plan Room Phase 0)

Every daily digest carries one `GATE DRIFT` block. It is the validating half of
the pre-receive gate in `harness/gatehouse/` — that hook is deliberately dumb
(paths plus a trailer, a presence check on text), so everything it delegates
lands here. Without this, `review-seq: 1` passes forever and the gate is a
spelling test.

The block opens with the detector's **own liveness**, three lines, in this
order and for this reason:

1. **the hook** — installed path, the sha of the file the symlink actually
   resolves to, and the mirror's sha for
   `harness/gatehouse/hooks/pre-receive-main-review`, or `ABSENT`. Committed is
   not installed.
2. **the push log's genesis** — `seeded` / `lazy` (a warning, never a plain
   state) / `TRUNCATED` / `REPLACED` / `NO LOG`.
3. **the floor**, against the floor *this digest's own previous post* reported.
   Any motion is `FLOOR MOVED`, the loudest line in the block. Floors don't
   move. That baseline lives in the message store, outside the git-dir, so it
   survives the log being deleted and lazily re-born — the one tamper case both
   in-log tells miss.

Then: mirror head, commits since the last digest and how many are uncited,
`classify_diff` on every uncited commit with an uncited Tier 2 named as a
**LANE VIOLATION**, the fail-open count, uncovered commits, overrides to date,
and a deploy-drift line.

**Citation is defined once, from push truth.** A commit is cited iff a logged
push covers it and that push's trailer resolves — the seq exists and lives in
#custodian. Push boundaries come from the hook's log and are never
reconstructed from reachability, so a five-commit push with one trailer on the
tip is one cited range rather than one pass and four false violations. A
`review-seq` whose author is the person who pushed is flagged **self-cited**.

**Nothing here is derived-but-stored.** The override count is recomputed from
`main`'s trailers every time, so "counted forever" survives a database rebuild.
The floor baseline is read back out of a post that already exists. The push log
is the one primary record — push boundaries and fail-open firings exist nowhere
in git — which puts it in the broker audit log's class, not a cache's.

Config lives in `broker.toml [gate]`. **With that block absent the digest still
posts a drift block, and it says `DETECTOR NOT CONFIGURED`** — an empty drift
block and a disarmed detector must never read alike.

`deploy_state()` is exported as a named function on purpose: the Plan Room's
tri-state badge is the same computation and calls it rather than
re-implementing it.

## Tests

```
server/.venv/bin/python -m pytest harness/metrics/tests   -q    # no network
server/.venv/bin/python -m pytest harness/gatehouse/tests -q    # the hook itself
server/.venv/bin/python -m pytest harness/broker/tests    -q    # 33 incl. budget
```
