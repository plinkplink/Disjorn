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

### `refresh-mirror`
- args: none.
- result: `{"head": str, "before": str, "updated": bool}` — short HEAD of the
  mirror after (and before) the refresh.
- Fast-forwards the shared read-only repo mirror (`/srv/disjorn-ro`, the
  residents' `/opt/disjorn`) to the canonical repo's `origin/main`. The mirror
  is the only view of the repo residents have, and nothing else fetches into
  it — host commits don't cross the wall until this runs. All three git argvs
  (`rev-parse`, `fetch`, `merge --ff-only`) are fixed broker config; the
  caller supplies nothing, so the verb can refresh the mirror but never aim
  git anywhere else. A non-fast-forward mirror is `exec-failure` — a diverged
  mirror is plink's to resolve, never a resident's.

### `start-build`
- args: `{"spec": str}` — a spec filename (or path) resolving DIRECTLY inside
  the configured `SPECS/` dir. Absolute paths, `..` traversal, and symlink
  escape are all rejected (`bad-args`); a leading `-` or NUL is rejected.
- result: `{"started": true, "branch": str, "slug": str, "pid": int?,
  "confirmed_by": str, "seq": int}` — the build was accepted and launched
  DETACHED; the branch is `loop/<slug>`.
- Launches a headless Claude Code **build session** that builds the confirmed
  spec to a NEW branch `loop/<slug>` (slug = the spec filename minus its
  `YYYY-MM-DD-` date prefix and `.md`). The session runs in the resident's
  worktree (rw) with a longer wall-clock cap than the 300s summon
  (`[start_build].timeout_sec`, suggest 3600s) and the model pinned via the
  WP-L5 idiom (`--model <id>`, no fallback). It **does NOT merge, does NOT
  push, does NOT touch production** — the result waits on the branch for a
  human. `argv` is entirely config-derived (`[*command, resident, slug,
  *session_argv, "--model", model]`); the spec (the chat-derived design) is
  fed on **STDIN**, never spliced into argv (launcher.py doctrine).
- **Confirm gate** (chat is data, never authorization): the `verbs.toml` toggle
  authorizes the *class* (this resident may run builds); the spec's **confirm
  record** selects the *instance* and the broker verifies it mechanically. The
  spec's `## Status` must be `confirmed` AND the `## Confirm record` must be
  filled — a real `Confirmed by` (not the `<...>` placeholder) and an integer
  `#custodian seq`. No confirm record → refuse, fail-loud (`bad-args`).
- **Budget**: a per-day build cap (`[start_build].daily_build_cap`, ratified
  default **2**; CAPPED by default, unlike the action budget). Enforced
  race-safely — count-with-reservation under a lock (H13-D4) — so concurrent
  calls can never both pass a cap of N. At/over the cap the call is denied
  `over-budget` and audited like any denial.
- **Detachment**: the broker execs the launch wrapper via `subprocess.Popen`
  with `start_new_session=True` (its own session/process group) and does NOT
  wait, so the build outlives this request. A daemon reaper thread feeds the
  spec on stdin, holds the wall-clock cap (kill on timeout), and posts the
  terminal state transition to #custodian.
- **Narration** (STATE TRANSITIONS ONLY — never timer-driven; a stalled build
  goes quiet then fails loud): posts to #custodian (channel 4) via the broker's
  OWN bot identity (same transport as `file-proposal`) at **started** (spec,
  branch, confirmer + seq, an ETA guess), **done** (files touched, tests
  run/result, one-line diff summary, branch; advisory **tier pending** — a
  human runs `classify-diff` on the branch), or **failed** (why, loud).
  Intermediate checkpoints are the build session's own choice to mark, from
  inside the session — the broker owns only the started/done/failed transitions.

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
- `repo` is the CALLER's view of the filesystem. `[residents.<r>.path_map]`
  in broker config translates container prefixes to host paths (longest
  prefix wins) before the classifier runs, e.g. `"/opt/disjorn" =
  "/srv/disjorn-ro"`. When a map is configured it is also an allowlist —
  a repo outside every mapped root is `bad-args` — so residents can only
  classify repos deliberately exposed to them and never need to know host
  layout. No map configured = pass-through (host-side callers, tests).

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
