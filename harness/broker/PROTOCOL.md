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
  connecting process is mapped to a caller identity via `broker.toml [uids]`.
  Nothing in the request body identifies the caller; nothing in the request
  body can escalate it. Chat is data, never authorization.
- Every identity but one is a resident seat. The exception is the **wake
  caller** (`wake`, 2026-08-25): a human at the keyboard, from their own uid,
  who may call that verb and nothing else — and whom no seat may impersonate,
  because the uid is the kernel's word, not the caller's.

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

A verb may add extra FACT fields to its own line; they can never overwrite the
six core keys. Today there is exactly one: `start-build` success lines carry
`"build_started": true`, which is how the build budget distinguishes "a build
ran" from "the call was authorized but nothing ever launched" (BL-D3).

## Verb table

All verbs are per-caller toggleable in `verbs.toml` and default OFF.
`restart-self` does not exist and never will (plink's ruling #3). `wake` is in
the table but in no seat's section — see its entry below.

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
- result: `{"head": str, "before": str, "updated": bool, "gatehouse": [...]}` —
  short HEAD of the mirror after (and before) the refresh, plus one record per
  gatehouse repo: `{"repo": str, "arrived": [ref], "vanished": [ref]}`.
- Fast-forwards the shared read-only repo mirror (`/srv/disjorn-ro`, the
  residents' `/opt/disjorn`) to the canonical repo's `origin/main`. The mirror
  is the only view of the repo residents have, and nothing else fetches into
  it — host commits don't cross the wall until this runs. All three git argvs
  (`rev-parse`, `fetch`, `merge --ff-only`) are fixed broker config; the
  caller supplies nothing, so the verb can refresh the mirror but never aim
  git anywhere else. A non-fast-forward mirror is `exec-failure` — a diverged
  mirror is plink's to resolve, never a resident's.
- **Since 2026-08-14 (file vision) it also fetches every ENTITLED gatehouse
  repo's branches** into `refs/gatehouse/<repo>/*`, with `--prune`, one fixed
  argv per repo. Still zero caller args. So `loop/<slug>` branches — the work
  residents are asked to review — are readable in the mirror instead of only
  in the gatehouse, which no resident can reach. `--prune` means a ref that
  vanished from the gatehouse vanishes here; the summary names it and says
  **harvested or deleted**, because from the mirror those two are
  indistinguishable and a guess would be worse than a fact.
- **TWO SOURCES, deliberately.** `main` comes from `origin` (plink's working
  clone = what production actually runs); branches come from the gatehouse.
  Single-sourcing on the gatehouse would let mirror-`main` LEAD production —
  the merged-is-not-deployed gap inverted, in the direction nobody watches.
- **The motion ping** — did their side change since main? No file reads, no
  broker call, and now available from either seat.

  From a seat with a shell, as **two separate `rev-parse` calls**:

      git -C /opt/disjorn rev-parse gatehouse/<repo>/loop/<slug>:<path>
      git -C /opt/disjorn rev-parse main:<path>

  Two calls, not the two-argument form: under `podman exec ... bash -lc` the
  quoting layers mangle a two-arg command line and it comes back with an
  answer about revs you did not ask for — which looks exactly like a real
  result (BuildGable's field note, #custodian seq 1322).

  From a **bot seat**, the same ping is two `read_repo_file` calls with
  `sha_only` (2026-08-19 read-repo-file-rev):

      read_repo_file {path: <path>, rev: "gatehouse/<repo>/loop/<slug>", sha_only: true}
      read_repo_file {path: <path>, rev: "main",                        sha_only: true}

  Two identical shas means that path is untouched on the branch; two different
  ones means read it. A directory answers with its **tree** sha, so a path
  that moved is motion too. Zero content either way — that is the point of the
  mode.

  **Pass `main` EXPLICITLY; do not lean on the no-rev default.**
  `read_repo_file` with no `rev` reads the mounted WORKING TREE, and `rev:
  "main"` reads the object store. During a `refresh-mirror` those two can
  momentarily disagree, so a ping that omitted the rev would be comparing a
  working tree against an object store and could report motion that was only
  a refresh in flight. Object store to object store, both sides.

### `start-build`
- args: `{"spec": str}` — a spec filename (or path) resolving DIRECTLY inside
  the configured `SPECS/` dir. Absolute paths, `..` traversal, and symlink
  escape are all rejected (`bad-args`); a leading `-` or NUL is rejected.
- result: `{"started": true, "branch": str, "slug": str, "pid": int?,
  "unit": str, "confirmed_by": str, "seq": int, "spec_status": {...}}` — the
  build was accepted and launched DETACHED; the branch is `loop/<slug>` and the
  build runs in the transient systemd unit `disjorn-build-<slug>.service` under
  the resident's own uid (see **Identity** below). `pid` is the broker's LOCAL
  launch process, not the build. `spec_status` is `{"ok": bool, "status":
  "building", "commit": str?, "why": str}` — what happened to the spec's
  `## Status` line (see **The Status line moves with the build** below);
  `ok: false` is NOT a refusal, the build runs regardless, but say so where a
  human will read it.
- **The Status line moves with the build (2026-08-17).** SPECS/README.md rules
  that state lives in the file — `draft → confirmed → building → built@<branch>
  → merged` (or `failed`) — and the broker is the process that knows each
  transition the instant it happens, so it writes the middle words:
  - on accept, BEFORE the started line: `confirmed → building`;
  - with the terminal banner, from the same ladder the banner is derived from:
    published → `built@<branch>`; failed (unit failed, PUBLISH-FAILED, timed
    out, no harvest) → `failed`; only NO-COMMITS → back to `confirmed`;
  - a launch that never ran (spawn error, preflight refusal) → back to
    `confirmed`.
  Each stamp is a **plumbing commit on the canonical repo's `main`**
  (`[start_build].spec_repo`, branch `spec_repo_branch` default `main`,
  subdir `spec_repo_subdir` default `SPECS`), authored `disjorn-broker`,
  followed by the SAME fetch + `--ff-only` refresh `refresh-mirror` runs, so
  the mirror the gate and the residents read carries the word at once. The
  keyboard's index and working tree are never read or written — except a
  courtesy `checkout HEAD -- <spec>` when HEAD is that branch and the file is
  provably clean, so `git status` stays quiet. A dirty file is left alone and
  named in `why`. The stamp only ever moves FROM the word it expects
  (`confirmed` at start, `building` at the end): a spec the keyboard already
  advanced (merged, superseded) is never overwritten. The merge is the one
  transition the broker never sees; `board --mark-merged` writes that word.
  Every banner ends with a `spec status:` line saying what moved, or that
  nothing did and why — a stamp that fails must be heard, or the next resident
  reads a stale `confirmed` and rebuilds. Nothing a resident controls reaches
  the file: the slug is the gate-validated filename, the words are the
  broker's own, and the one resident-influenced string (a failure reason) is
  flattened to one line with every `--` broken before it enters the comment.
  Consequence for callers: a spec that reads `building` / `built@…` / `failed`
  is refused by the gate exactly like `draft` (only `confirmed` builds); to
  allow another attempt after a failure, a human sets the line back to
  `confirmed` — the confirm record is never touched by any of this.
- Launches a headless Claude Code **build session** that builds the confirmed
  spec to a NEW branch `loop/<slug>`, where **slug = the spec filename minus
  `.md`, date prefix INCLUDED** (`2026-07-21-gif-picker.md` →
  `loop/2026-07-21-gif-picker`, container `disjorn-build-2026-07-21-gif-picker`).
  The date prefix is required and is the collision disambiguator (BL-D4): two
  specs with the same name on different dates used to derive the same branch
  and the same podman `--name`. Branch name is now 1:1 with the spec file.
  A second build of the SAME slug while one is in flight is refused
  `bad-args` ("a build for … is already running"), which burns no budget.
  The session runs in the resident's
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

  **THE LITERAL TEMPLATE.** The gate is a regex over markdown, so the shape is
  the gate. This is the exact text — two `##` headings, the `- **Field**:`
  bullet form, the bold markers included:

  ```markdown
  ## Confirm record
  - **Confirmed by**: plink
  - **#custodian seq**: 1008
  - **Confirmed at**: 2026-08-12

  ## Status
  confirmed
  ```

  What the parser actually does with it, so a near-miss is recognisable as one:
  - It scans for a line that is exactly `## Confirm record` (case-insensitive)
    and reads until the next `## ` heading. A record under a `###` subheading,
    or trailing after a horizontal rule inside another section, is not found.
  - `Confirmed by` and `#custodian seq` must be `- **Name**: value` bullets. The
    bold markers are part of the pattern; `- Confirmed by: plink` does not match
    and reads as an EMPTY record.
  - A value that is blank, `-`, `_`, or still angle-bracketed (`<username>`) is
    **None** — mechanically identical to having no record at all. A spec that
    looks confirmed at a glance because the template's placeholder is still in
    the box is refused, which is the point.
  - `seq` takes the first run of digits in the value, so `seq 1008` and `#1008`
    both work; a seq with no digits at all (`n/a`, `at the keyboard`) is None
    and the build is refused. A build that was confirmed off-channel needs the
    witness recorded some other way before it can run through this verb.
  - `## Status` takes the first non-blank, non-HTML-comment line after the
    heading, strips backticks, lowercases it. `` `confirmed` `` with a trailing
    `<!-- … -->` comment is fine; `confirmed for phase 1` is not the token
    `confirmed` and is refused.
  - `Confirmed at` is **not read by the gate**. It is for the humans; the seq is
    the witness.

  Both headings are required and both are checked — a `confirmed` status with an
  empty record is refused, and a filled record under a `draft` status is refused.
- **What makes the confirm gate real: `specs_dir` must be resident-unwritable**
  (BL-D1). The record is a presence check on *text*; it is only trustworthy
  because the text lives in the plink-gated read-only mirror
  (`/srv/disjorn-ro/SPECS`). The broker now **asserts that at startup** and
  **refuses to start** (exit 2, loud on stderr) if `realpath(specs_dir)`:
  - sits inside any resident volume — `/home/<resident>` for any resident in
    `[uids]`/`[residents]`, any `[residents.<r>].writable_roots` entry, or any
    `[residents.<r>.path_map]` host target that resolves inside one of those; or
  - does not exist / is not a directory; or
  - has any path component (the directory itself or ANY parent up to `/`)
    writable by a resident uid, by a group a resident belongs to, or by
    "other". Sticky directories (e.g. `/tmp`) are exempt as *parents* only —
    the kernel forbids replacing entries you do not own there — never as the
    SPECS directory itself.

  There is no degraded mode and no opt-out: a broker that cannot prove SPECS/
  is resident-unwritable does not come up. One deliberate carve-out: a resident
  uid equal to the broker's own euid is not counted, because such a caller
  already *is* the broker.
- **Budget**: a per-day build cap (`[start_build].daily_build_cap`, ratified
  default **2**; CAPPED by default, unlike the action budget). Enforced
  race-safely — count-with-reservation under a lock (H13-D4) — so concurrent
  calls can never both pass a cap of N. At/over the cap the call is denied
  `over-budget` and audited like any denial. The day's count is seeded from the
  audit log at first use, counting only lines that carry the `build_started`
  marker: a call that was authorized but whose spawn failed never started a
  build and must not consume a slot after a broker restart (BL-D3).
- **Identity — the build runs AS the resident, not as the broker.** The broker
  runs as plink, so a build launched straight from it ran podman as plink:
  `--userns keep-id` mapped the container to uid 1000 (a callback to this socket
  presented **plink** in SO_PEERCRED), `$HOME/resident-home` resolved to
  plink's home, and a resident-authored spec drove a session with plink's
  filesystem access — both 0700 resident homes and the live production tree.
  The launch therefore goes through **one validating root helper**
  (`harness/broker/disjorn-build-launch`, installed
  `/usr/local/lib/disjorn/disjorn-build-launch`, root:root 0755), reached by
  `sudo -n` and named exactly by `/etc/sudoers.d/91-disjorn-build`. It execs

      systemd-run --unit=disjorn-build-<slug> --uid=res-<resident> --gid=…
                  --collect --pipe <fixed limits> -- <run-build.sh> <resident>
                  <slug> <session argv…>

  so the build is a **transient system service under the resident's own uid**:
  keep-id, `$HOME` and SO_PEERCRED are all correct *by construction*. The
  helper takes NO path, unit name, uid or limit from its caller — it derives
  every one of them from `(mode, resident, slug)` and re-validates both, because
  a sudoers rule is a privilege boundary and a boundary that trusts its caller
  is decoration. There is deliberately **no sudoers rule for `systemd-run`**:
  sudoers matches arguments as one concatenated string, so any wildcard
  permissive enough to carry a real launch also permits appending `--uid=0`,
  which makes such a rule equivalent to a grant of full root.
  `result.unit` names the unit; `systemctl status disjorn-build-<slug>` is the
  way to look at a running build.
- **Detachment**: the broker execs the launch via `subprocess.Popen` with
  `start_new_session=True` and does NOT wait, so the build outlives this
  request. Because `systemd-run --pipe` passes the broker's own descriptors
  through, that local process still carries the build's stdin, stdout, stderr
  and exit status — but the BUILD itself is a unit **outside the broker's
  cgroup**, so `systemctl restart disjorn-broker` no longer kills it (and
  killing the local process no longer kills the build: the reaper's timeout
  path stops the *unit*, via the helper's `stop` shape).
- **Reattachment across a broker restart**: at spawn the broker writes a 0600
  JSON sidecar `<slug>.build.json` next to the spool files (unit, branch, both
  spool paths, pid, the ORIGINAL deadline). At startup `adopt_inflight_builds()`
  reads them: a unit still running is **re-adopted** — its slug is re-claimed
  (so a duplicate `start-build` is still refused) and a polling reaper narrates
  the terminal transition when it lands, honouring the original deadline rather
  than granting a fresh one; a unit already gone is narrated from the spool tail
  (a parseable report ⇒ done, anything else ⇒ a loud "outcome unknown"). Spool
  files with no live sidecar are swept in the same pass — that is the only thing
  that bounds `build-logs` across crashes. Adoption never launches anything, and
  never blocks startup.
- **Resource ceilings are kernel-enforced, not polled.** The transient unit
  carries `RuntimeMaxSec` (an outer backstop, longer than `timeout_sec`, so a
  build orphaned by a broker crash still dies), `MemoryMax` / `MemorySwapMax=0`,
  `TasksMax`, `LimitCORE=0`, and — closing the BL-D2 follow-up — **`LimitFSIZE`,
  the disk bound**. BL-D2 bounded build output in the broker's RAM but left it
  unbounded on disk; because the build's stdout *is* a regular file (the spool),
  RLIMIT_FSIZE caps it, and a flooding build dies loudly on SIGXFSZ instead of
  filling `/`. The values are constants in the helper, not config: a limit a
  caller can widen is not a limit.
- **Build output is bounded** (BL-D2). Only **stdin** is a pipe (that is how
  the spec is delivered). The build's **stdout and stderr are redirected at
  spawn to two 0600 temp files** in `[broker].build_log_dir` (default:
  `<dir of audit_log>/build-logs`, plink-owned and disk-backed — deliberately
  not `/tmp`, which is tmpfs here and would put the flood back in RAM), and
  the reaper reads back at most a **64 KiB tail of each**
  (`MAX_BUILD_LOG_TAIL`) for the done/failed narration. Piping a
  resident-influenced session that runs up to `timeout_sec` used to buffer the
  whole stream in the privileged broker (180 MB of stdout → 540 MB broker RSS
  → OOM for every resident). Both files are deleted on every exit path — done,
  failed, timed out, crashed, and launch-failed. The final JSON report is
  looked for in the whole tail and then in its last line, so a truncated head
  never costs the report.
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

### The Plan Room verbs — `board-list`, `board-card`, `board-search`, `board-flag`, `board-comment`

SPECS/2026-08-20-plan-room.md (confirmed by plink, #custodian seq 1434). Five
verbs: three read, two write.

**The rule that shapes all five: the board owns no authoritative state.** Every
card is a rendering of an artifact that already exists — a `SPECS/` file's
Status line, a confirm seq, a gatehouse branch, a backlog row, deploy
provenance. What the board owns natively is comments, card order, the blocked
flag + its reason, and archived — that list is complete. **The two write verbs
are structurally unable to touch anything else, because derived state has no
write path anywhere in this house** (seq 1428 P1). Nothing a resident sends
through this socket can move a card between columns; changing the underlying
artifact does that. Phase II's write-through is a separate spec.

All five go through the Disjorn server's `/planroom` surface as the broker's own
bot identity. A card is derived state plus board-native state, composing them is
exactly one job, and the server already does it for the tab — a second composer
in the broker would be a second answer to "is this card blocked". The server's
refusal text is carried back verbatim, so a resident told "the Plan Room index
is unavailable" can act, where one told "HTTP 503" has to go find someone.

Every read returns a `face` string first: when the board was derived and from
which mirror head. The board cannot go stale relative to the mirror — it is not
a copy of it — but the mirror can lag, so staleness is declared, never denied.

- **`board-list`** — args `{"column": str, "lane": str, "owner": str,
  "blocked": "yes"|"no", "limit": int}`, all optional; `limit` 1..200 default
  80. Result `{"face": str, "counts": {column: n}, "cards": [str, ...],
  "count": int, "truncated": bool}` — ONE LINE PER CARD. Skim is the default
  and detail is opt-in; that is the context-budget answer to the request that
  started the feature. `counts` describes the whole board, never the filter.
- **`board-card`** — args `{"slug": str}` required. Result `{"face": str,
  "card": {...}, "comments": [...], "note": str?}` — everything, comments
  included. Comments outlive the card leaving the board, so a slug with
  comments and no card returns the comments and a note saying so.
- **`board-search`** — args `{"text": str, "limit": int}`; `text` required,
  1..200 chars, plain case-insensitive substring across card text AND comments.
  Result is `board-list`'s shape.
- **`board-flag`** — args `{"slug": str, "action": "blocked"|"unblock",
  "reason": str}`. A reason is REQUIRED to block (≤500 chars) and a card
  blocked without one is `bad-args`: a card held for no stated reason is one
  nobody can unblock. **The card does not move** — blocked is a flag, never a
  column, so a held card keeps its place and everyone can see where it
  re-enters. Attribution is the broker's, stamped from the caller's
  SO_PEERCRED-derived resident name, never from `args`.
- **`board-comment`** — args `{"slug": str, "text": str}`, both required, text
  1..4000 chars. Same attribution rule.

A `slug` is a spec slug (`YYYY-MM-DD-name` — the spec filename without `.md`,
because a card's identity IS its spec file), or one of the two forms for cards
with no spec yet: `backlog-<n>` and `keyboard-<sha>`. Anything else is
`bad-args` before it reaches the network.

The broker also WRITES the derived index these verbs read, from
`harness/planroom/planroom.py`, on three triggers (seq 1428 P4): after
`refresh-mirror` moves the mirror, after a build's terminal banner, and on its
own timer. All three are best-effort — a rebuild failure never turns a
successful verb into a failed one, and lands in that call's audit summary
instead. Each rebuild posts one #custodian line per COLUMN TRANSITION, never per
edit; a rebuild that moved nothing says nothing, and a cold start says nothing
at all. Configured under `[planroom]` in broker.toml; with that block absent
nothing rebuilds and the verbs still answer honestly.

### `summon-hop`

SPECS/2026-08-24-custodian-mention-summons.md. The bot-to-bot summon wall, kept
broker-side so both residents' summon adapters spend against ONE counter. The
adapters are the callers; a session has no reason to press it.

- args: `{"action": "spend"|"unpark", "work_item": str, "summoner": str,
  "seq": int}` — `action` required; `work_item` required for `unpark`.
- `spend` result: `{"allowed": bool, "chain": bool, "work_item": str|null,
  "reason": str, "count": int, "cap": int, "refusal": str}`.
  - `chain: false` is NOT a refusal: it means serve the summon but do not let
    the reply re-trigger anyone (depth 1, the default since WP-H9). It is the
    answer whenever no live work item is cited — an unknown slug, a card
    outside Review, or a board that cannot be reached.
  - `allowed: false` carries `refusal`, the fixed in-channel line the adapter
    posts verbatim: `summon refused: <slug> at 8/8 bot hops — parked until a
    human posts on it`.
- `unpark` result: `{"reset": bool, "count": int, "cap": int}`. Idempotent per
  `seq`, because both adapters see the same human post and both report it.
- Caps live in `broker.toml`; with `[summon_hops]` absent there is no wall and
  every `spend` answers `chain: false`.

The clock never unparks a chain: midnight rolls the 24-per-UTC-day ceiling and
nothing else. A parked work item stays parked until a human posts about it.

### `wake`

SPECS/2026-08-25-agentic-residents.md (confirmed seq 1913). Wake a seat with a
task: one headless work session, a longer wall clock than a summon, one result
post in #custodian, then exit.

**This is the one verb no resident may call, and the only verb its caller may
call.** Both halves are enforced in `dispatch()` before `verbs.toml` is read:

- a `wake` from any resident, build or adapter uid is refused (`verb-disabled`)
  and audit-logged. Origin arrives as connection data — the SO_PEERCRED uid of
  the connecting process — so no text in any channel can constitute a wake;
- a wake caller (`[wake].callers`, a human's identity, never a `res-*` seat)
  gets the same refusal on every other verb.

- args: `{"resident": str, "task": str}` — the seat to wake (checked against
  `[wake].residents`; caller input never names an unlisted seat) and the work,
  ≤4000 chars.
- result: `{"wake_id": str, "resident": str, "session_cap_sec": int,
  "grace_sec": int, "requested_at": str}`.
- The audit line carries `"wake_id"` as a FACT field, like `start-build`'s
  `build_started`. That id is the join key between this line, the seat's
  action-log start/end pair, and the #custodian post.
- **Budget**: a per-seat per-UTC-day wake cap (`[wake].daily_wake_cap`, default
  **3**; CAPPED by default, like the build cap and unlike the action budget).
  At/over the cap the call is denied `over-budget`, no record is written and
  nothing is queued for later. The refusal names the day's wall clock beside the
  count — "3/3 wakes, 4h10m of session time today" — because the minutes are the
  cost and the count is the speed bump. That clock is a CEILING: the broker sees
  when each wake started and what cap it granted, never when the session
  actually stopped. The count is read from the spool under the same lock that
  writes the new record, so concurrent presses cannot both pass a cap of N.

The broker **launches nothing**. It writes one 0644 JSON record into
`[wake].spool_dir` — plink-owned and verified resident-*unwritable* at startup,
for the reason `start_build.specs_dir` is (a resident that can write the spool
can write itself a wake) — and returns. The seat's own runner
(`harness/residency/run_wake.py`, running as `res-<seat>`) picks the record up,
runs the session in the seat's container, and is what harvests and posts. The
session's wall-clock cap rides on the record, so it is plink's value and there
is only one of it.

With `[wake]` absent there are no callers, so every `wake` is refused: the verb
is present and inert, the same fail-closed shape as an unflipped kill switch.

**What a wake does NOT get.** No verb a summon seat lacks — including
`start-build`, which a woken session inherits and which stays safe because the
confirm gate is upstream of every build. One rule is added for a seat that is
awake: while a wake is in flight for it, `start-build` also refuses a spec
whose **Review owner** resolves to that seat (or to the seat that woke it — a
forward rule, inert while only humans wake), and refuses a spec that states no
review owner at all. From the socket a woken caller and a summoned one are the
same uid, so the wake window is the only signal; the imprecision runs in the
safe direction, refusing builds a summon could have run rather than letting a
woken session past the rule.

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
  passes and before the verb runs. The day's count of **allowed** actions is
  seeded from the audit log (so it survives a broker restart) and then held as
  an in-memory **reservation taken under a lock** — counting and acting are one
  atomic step (H13-D4), so N concurrent dispatches can never all read the same
  pre-cap count and all run. At or over the cap the call is denied with
  `over-budget` and audited (`allowed: false`) like any denial. Denied calls do
  not count toward the cap — a denial refunds its reservation — so a resident
  cannot exhaust its own budget by being refused. The same discipline applies
  to every numeric budget, including the `start-build` per-day build cap.
- **Live-ness**: unlike `verbs.toml` (re-read every request), budgets load at
  broker start — a cap change takes a broker restart. Kill switches stay the
  instant lever; budgets are a tunable backstop.

The end-of-day one-liner ("daily action counts visible in #custodian") is
posted by `metrics.py post-daily` via the broker's OWN posting identity — the
same transport `file-proposal` uses. It is a scheduled CLI, not a verb: no
resident can trigger it.
