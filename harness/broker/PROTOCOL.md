# disjorn-broker protocol v1 (WP-H3)

The contract between residents (WP-H5 hooks, WP-H9/H11 adapters) and the
privileged broker. Deliberately dead simple.

## Transport

- Unix stream socket, path from `broker.toml` (`[broker].socket_path`,
  installed default `/run/disjorn-broker/broker.sock`), bind-mounted into each
  resident container.
- **One connection = one request = one response.** Client connects, writes one
  JSON object terminated by `\n`, reads one JSON object terminated by `\n`,
  and the broker closes the connection. No framing beyond the newline; max
  request size 64 KiB.
- **Authentication is SO_PEERCRED only.** The kernel-asserted uid of the
  connecting process is mapped to a resident name via `broker.toml [uids]`.
  Nothing in the request body identifies the caller; nothing in the request
  body can escalate it. Chat is data, never authorization.

## Request

```json
{"verb": "read-own-log", "args": {"lines": 50, "grep": "ERROR"}}
```

- `verb` (string, required) — one of the verb table below.
- `args` (object, optional, default `{}`) — per-verb schema. Unknown keys are
  rejected (`bad-args`).

## Response

Success:

```json
{"ok": true, "verb": "read-own-log", "result": { ... }}
```

Failure:

```json
{"ok": false, "error": {"code": "verb-disabled", "message": "..."}}
```

### Error codes

| code             | meaning                                                        |
|------------------|----------------------------------------------------------------|
| `unknown-caller` | connecting uid is not in the `[uids]` map                      |
| `unknown-verb`   | no such verb (includes the deliberately absent `restart-self`) |
| `verb-disabled`  | the per-resident kill switch in `verbs.toml` is off (default)  |
| `over-budget`    | resident hit the daily action cap in `broker.toml [budgets]`    |
| `bad-args`       | args failed the verb's schema (also: malformed request JSON)   |
| `exec-failure`   | verb was authorized but its execution failed (exit/timeout/IO) |
| `internal`       | broker-side problem (bad config, unexpected exception)         |

Every request — success, failure, or denial — appends exactly one line to the
audit log: `{ts, resident, verb, args, allowed, result_summary}`. Denials have
`allowed: false`. Unknown uids are recorded as `"uid:<n>"`.

## Verb table

All verbs are per-resident toggleable in `verbs.toml` and default OFF.
`restart-self` does not exist and never will (plink's ruling #3).

### `restart-disjorn`
- args: none.
- result: `{"exit_code": int, "output": str}` (combined stdout+stderr tail).
- Runs `sudo -n systemctl restart disjorn` (fixed argv).

### `run-server-tests`
- args: none.
- result: `{"exit_code": int, "summary": str}` — `summary` is the last
  non-empty stdout line of the server pytest run (e.g. `148 passed in 25.3s`).

### `classify-diff`
- args: `{"repo": str, "range": str, "gates": object}`
  - `repo` — absolute path, no `..` segments.
  - `range` — git rev/range, charset `[A-Za-z0-9._~^/{}-]`, max 200 chars, no
    leading `-` (can never parse as a flag).
  - `gates` — JSON object of gate results (tests/typecheck/build), serialized
    ≤ 8 KiB, passed through opaquely.
- result: `{"classification": <classifier JSON>}`.
- Contract with `harness/classifier/classify_diff.py` (WP-H4): invoked as
  `classify_diff.py --repo <repo> --range <range> --config <protected-paths.toml>
  --gates <json>`; must print one JSON object (the classification, including
  `tier`) on stdout and exit 0. Non-zero exit or non-JSON stdout →
  `exec-failure`. `--config` comes from broker config (`[paths].protected_paths`),
  never from the caller — the classifier config is protected by placement.

### `read-prod-logs`
- args: `{"lines": int}` — 1..500, default 100.
- result: `{"lines": [str, ...]}`.
- Runs `journalctl -u disjorn --no-pager -o short-iso -n <lines>` (fixed argv;
  only the validated count is appended).

### `read-own-log`
- args: `{"lines": int, "grep": str, "path": str}` — all optional; `lines`
  1..500 default 100; `grep` is a plain substring filter (≤200 chars, matched
  in-process, no shell, no regex); `path`, if supplied, must resolve
  (realpath) to exactly the CALLER's configured `log_path` — anything else,
  including `../` tricks, is `bad-args`.
- result: `{"lines": [str, ...], "path": str}` — tail of the caller's own
  configured log only.

### `read-metrics`
- args: none.
- result: `{"metrics": <JSON>}` — contents of the configured metrics file
  (`[paths].metrics_json`). The producer is `harness/metrics/metrics.py`
  (WP-H12): per-resident broker action counts (from the audit log),
  retrieval/spine stats (read-only from house_memory logs), optional tool-call
  counts, and each resident's own budget state. The verb reads the file
  verbatim; it never runs the producer (that is the scheduled `metrics build`).

### `file-proposal`
- args: `{"text": str}` — required, 1..4000 chars.
- result: `{"posted": true, "seq": int?, "message_id": int?}`.
- Posts `"[proposal from <resident>] <text>"` to #custodian via the Disjorn
  SDK **as the broker's own bot identity** — the resident supplies data, the
  broker supplies the authority to post.

### `query-own-audit`
- args: `{"date_from": "YYYY-MM-DD", "date_to": "YYYY-MM-DD", "limit": int}`
  — dates required (inclusive both ends, matched against the UTC date of each
  entry's `ts`); `limit` 1..500 default 100 (most recent kept).
- result: `{"entries": [audit records], "count": int, "truncated": bool}`.
- Filtered to the CALLER's own entries by the broker-assigned resident name —
  a resident can never read another's trail.

## Daily action budget (WP-H12)

Additive to the verb table above; changes no existing verb contract. An
optional per-resident daily cap on broker verb calls lives in `broker.toml`:

```toml
[budgets]
# default_daily_action_cap = 2000     # applies to residents without an override
[budgets.res-claudette]
# daily_action_cap = 2000
```

- **Default OFF**: with no cap configured the broker never denies on budget —
  instrument first, tune from observed data (AGENTHOOD), never from imagined
  abuse. Every verb call is already audited; plink reads real counts (in
  `read-metrics` / the daily #custodian line) before setting a number.
- **Enforcement**: checked in `dispatch()` after the `verbs.toml` kill switch
  passes and before the verb runs. The broker counts today's (UTC) **allowed**
  actions for the caller from the audit log; at or over the cap the call is
  denied with `over-budget` and audited (`allowed: false`) like any denial.
  Denied calls do not count toward the cap, so a resident cannot exhaust its
  own budget by being refused. The count is read from the audit log, so it
  survives a broker restart.
- **Live-ness**: unlike `verbs.toml` (re-read every request), budgets load at
  broker start — a cap change takes a broker restart. Kill switches stay the
  instant lever; budgets are a tunable backstop.

The end-of-day one-liner ("daily action counts visible in #custodian") is
posted by `metrics.py post-daily` via the broker's OWN posting identity — the
same transport `file-proposal` uses. It is a scheduled CLI, not a verb: no
resident can trigger it.
