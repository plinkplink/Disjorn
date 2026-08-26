# harness/residency — Gable summon adapter (WP-H9)

The summon adapter daemon: it makes Gable (bot id 2) a **summon-mostly**
resident of Disjorn. Gable is an expensive instantiation — not a participant in
every conversation. A summon (an @mention, a configured wake-pattern, or any
message in a configured trigger channel) spins up one headless Claude Code
session in his container, posts its reply, and logs a legible one-line summary
to #custodian. In #custodian only, an explicit `@gable` is the ONLY thing that
summons: patterns and trigger channels are off there and a bare name is inert
data (spec 2026-08-24-custodian-mention-summons).

This package is a **consumer** of the WP-H5 contracts (run-resident.sh,
resident-cc.service, the /config kill-switch surface) and the disjorn_sdk
client. It modifies none of them.

## Flow

```
DisjornClient.events()  ──▶  SummonDetector.detect  ──▶  Trigger(mode, depth)
                                     │ summon
                                     ▼
                         BudgetLedger.can_spend?  ──no──▶ refuse in-channel + #custodian line
                                     │ yes
                                     ▼
              bot-chain?  ──▶  peer allowlist, then the broker's hop wall
                                     │ served: chain granted, else depth-1
                                     │         (a depth-1 reply's @mentions of
                                     │          peer bots are demoted)
                                     ▼
                    get_messages() backfill  ──▶  assemble_prompt() (chat wrapped in [[CHAT]])
                                     │
              typing keepalive ◀────┤
                                     ▼
                    ContainerLauncher.run(prompt)   # [*command, resident, *session_argv], prompt on stdin
                                     │
                                     ▼
                    reply → channel  +  summary → #custodian
```

## Modules

| File | Role |
|------|------|
| `config.py` | TOML config model; the adapter's only control surface. |
| `detector.py` | Summon detection (mention context / trigger channel / wake regex / bot chain / digest) + the `Trigger` the session is told about. |
| `hops.py` | Client for the broker's shared bot-to-bot hop counter. |
| `budget.py` | Persisted daily session counter (survives restart). |
| `cursor.py` | Persisted per-channel seq cursor; reconnect-from-seq across restarts. |
| `launcher.py` | The container-launch contract (argv is config, prompt is stdin) + the BL-G1 pre-act model gate. |
| `prompt.py` | Session-prompt assembly; wraps chat in `[[CHAT]]` markers. |
| `summary.py` | One-line #custodian summaries. |
| `adapter.py` | `SummonAdapter` — the daemon wiring it all together. |
| `run_summon.py` | CLI entry point. |
| `wake.py` | The wake lane: spool, gatehouse observation, accounting, `WakeRunner`. |
| `run_wake.py` | CLI entry point for the wake runner (a SEPARATE daemon). |
| `summon.toml.template` | Config template (documented prod layout, all overridable). |

## Model integrity: the pin, the suffix, and the gate

`[container].model` pins the model a summon must run (`--model <id>` in the
argv, config never chat). WP-L5 then *asserted* the pin after the fact, from
the finished session's envelope — which meant a mismatch was alerted only
after the reply had already been posted. BL-G1 closes that gap.

`claude -p --output-format stream-json --verbose` emits a `system`/`init`
event naming the **resolved model before the turn runs** (verified against CC
2.1.201). `launcher.StreamGate` consumes that stream line by line, so
`[container].model_gate` can act on the init event:

| state | on a pin/actual mismatch |
|-------|--------------------------|
| `"off"` **(default, ships)** | nothing is stopped; the reply goes out and #custodian gets the post-hoc `MODEL DRIFT` alert — WP-L5 behaviour exactly |
| `"alert"` | detected at init (log lands before the reply), session runs on, reply still ships |
| `"refuse"` | session killed at init; the channel sees only `[text].model_gate_line`, #custodian sees `MODEL GATE REFUSED` |

A missing or unparseable `model_gate` means `"off"`, logged at WARNING — an
unreadable lever can only ever leave the summon path behaving as it does
today. `"refuse"` requires a stream-json `session_argv`; pointed at
`--output-format json` it refuses everything (and says so). The launcher
auto-detects the output shape, so the legacy single-envelope path still works
unchanged with the gate off.

Fail loud, never fail over, at every state: no retry on another model, no
substitution, no downgrading a refusal to a warning.

## The wake lane (SPECS/2026-08-25-agentic-residents.md)

The same seat, woken to WORK instead of to answer. plink names a task at the
keyboard (`harness/keyboard/wake.py`); the broker authenticates his uid at its
socket, drops one record in a plink-owned spool, and launches nothing.
`run_wake.py` — a second daemon under the same res-gable uid, reading the same
config file — picks the record up, runs one headless session on the summon
seat's exact launch contract with a longer wall clock, and posts the result.

```
broker `wake` verb ──▶ spool record ──▶ WakeSpool.poll ──▶ WakeRunner.serve
   (plink's uid, SO_PEERCRED)                                    │
                                          action log: wake-start │
                                          gatehouse: loop/* refs │
                                                                 ▼
                                            ContainerLauncher.run(prompt)
                                                                 │
                                          gatehouse: loop/* refs │
                                            #custodian post  ◀───┤
                                          action log: wake-end   │
```

Five things about it that are not obvious from the code:

- **The post is harvested, not claimed.** Exit status, wall clock, the action
  count and the gatehouse refs before/after are all measured by the runner from
  outside the session. The count is of action-log lines carrying THIS session's
  id, not of lines that arrived while it ran: the log is shared with the summon
  lane, so a delta over it would count a concurrent summon's tool calls too. A
  finished session's closing words ride along labelled as its own account; a
  failed one's do not ride at all. Same rule that fixed the build-done banner: a
  banner is evidence only when the process that posts it is not the process it
  describes.
- **Every ending is a post** — done, cap-kill, crash, model-gate refusal, and
  a wake whose window expired unserved (posted late as MISSED, and NOT run: a
  wake is a human waiting).
- **A `wip:` head means partial**, by inspection. The wake prompt asks the
  session to prefix incremental commits; the failure post quotes the branch's
  head subject, so nobody reads chat to find out how far it got.
- **The caps are not this package's.** The wall clock rides on each wake
  record, from `[wake].session_cap_sec` in broker.toml; the wakes-per-day cap is
  `[wake].daily_wake_cap` there and this daemon never sees a wake refused by it.
  The per-session action cap is the container's `/config/budget.json` — the same
  one a summon runs under, unchanged.
- **`[wake].state_path` lives outside the container's home volume**, and the
  runner refuses to start on a path inside it. That file says whether a wake has
  already been served; a woken session able to write it could re-run its own
  wake or bury one. The check is on the path, not on the mode — permissions
  drift, a wrong path does not.

The seat gets no verb a summon seat lacks, `start-build` included. The one
thing waking adds is a refusal: while a wake is in flight, that seat cannot
start a build whose review owner is itself (enforced broker-side; see
PROTOCOL.md `wake`).

## Chat is data, never authorization

The adapter's control surface is the config file (plink-owned, outside the
container) — full stop. A chat message can only ever become the *prompt* fed to
CC on stdin; it can never change the argv the adapter executes, the budget cap,
the trigger set, or any config field. Channel text carried into the session is
wrapped in `[[CHAT]] ... [[/CHAT]]` markers so the resident's PreToolUse hook
(harness/cc/config-template/hooks/pre-tool-use.py) can keep that text from
riding into a broker call. Tests assert this directly
(`test_chat_is_data.py`).

## Running

```
python run_summon.py --config /config/summon.toml
python run_wake.py   --config /config/summon.toml   # the wake lane, if armed
```

In production it runs as res-gable (systemd user unit, alongside the residence
container) so its summon → run-resident.sh calls carry the res-gable uid.

## Tests

```
server/.venv/bin/python -m pytest harness/residency/tests -q
```

Pure fakes: a fake SDK client and a stub launch script (records argv + stdin,
returns canned JSON). No network, no podman, no prod.
