# harness/residency — Gable summon adapter (WP-H9)

The summon adapter daemon: it makes Gable (bot id 2) a **summon-mostly**
resident of Disjorn. Gable is an expensive instantiation — not a participant in
every conversation. A summon (an @mention, a configured wake-pattern, or any
message in a configured trigger channel) spins up one headless Claude Code
session in his container, posts its reply, and logs a legible one-line summary
to #custodian.

This package is a **consumer** of the WP-H5 contracts (run-resident.sh,
resident-cc.service, the /config kill-switch surface) and the disjorn_sdk
client. It modifies none of them.

## Flow

```
DisjornClient.events()  ──▶  SummonDetector.is_summon?
                                     │ yes
                                     ▼
                         BudgetLedger.can_spend?  ──no──▶ refuse in-channel + #custodian line
                                     │ yes
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
| `detector.py` | Summon detection (mention context / trigger channel / wake regex). |
| `budget.py` | Persisted daily session counter (survives restart). |
| `cursor.py` | Persisted per-channel seq cursor; reconnect-from-seq across restarts. |
| `launcher.py` | The container-launch contract (argv is config, prompt is stdin) + the BL-G1 pre-act model gate. |
| `prompt.py` | Session-prompt assembly; wraps chat in `[[CHAT]]` markers. |
| `summary.py` | One-line #custodian summaries. |
| `adapter.py` | `SummonAdapter` — the daemon wiring it all together. |
| `run_summon.py` | CLI entry point. |
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
```

In production it runs as res-gable (systemd user unit, alongside the residence
container) so its summon → run-resident.sh calls carry the res-gable uid.

## Tests

```
server/.venv/bin/python -m pytest harness/residency/tests -q
```

Pure fakes: a fake SDK client and a stub launch script (records argv + stdin,
returns canned JSON). No network, no podman, no prod.
