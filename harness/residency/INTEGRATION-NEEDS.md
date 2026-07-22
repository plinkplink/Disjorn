# INTEGRATION-NEEDS — WP-H9 residency adapter

Changes this package needs OUTSIDE harness/residency/. Written here (not made)
because harness/residency/ is this WP's exclusive file territory. Each item is
a keyboard/config or a sibling-WP task, not adapter code.

## 1. Gable bot identity + key (blocker for live activation)

- Prod bot rename `bots.name` for id 2 to "Gable" awaits plink's blessing
  (HARNESS-PLAN / AGENTHOOD "The name"). The adapter authenticates as whatever
  key it's given; the display name is server-side.
- A Gable bot API key must exist (server/cli.py create-bot or the existing
  id-2 key) and be written to the plink-owned key file the config points at
  (default `/config/gable-key`, mounted ro). The adapter never creates it.

## 2. run-resident.sh must support a summon (per-summon ephemeral session)

The adapter's launch contract is `[*command, resident, *session_argv]` with the
prompt on stdin and the result as JSON on stdout. run-resident.sh already
accepts `run-resident.sh <name> [command...]` and does `podman run --rm`, so a
summon maps to `run-resident.sh gable <headless-cc-argv>`. Two things to
confirm at install time (owners: WP-H5 / keyboard, not this WP):

- The in-container headless CC command (the `session_argv`) must: read the
  prompt from **stdin**, run non-interactively, and print the session as JSON
  on **stdout**. The template now ships
  `claude -p --output-format stream-json --verbose` (one JSON object per line;
  `--verbose` is mandatory in `--print` mode). The launcher auto-detects the
  shape, so the older `--output-format json` single envelope still parses —
  it just cannot support the BL-G1 pre-act model gate, which reads the
  `system`/`init` event. If the chosen CC invocation can't take stdin, a tiny
  in-image wrapper script is the integration point — it belongs in the resident
  image (WP-H5), not here.
- `podman run --rm` per summon is an ephemeral container distinct from the
  long-lived residence container started by resident-cc.service. Confirm that's
  the intended shape for Gable (vs. `podman exec` into the residence
  container). If `exec` is preferred, only this package's `container.command`
  config changes — no adapter code changes. Flagging the choice for plink.

## 3. Where the adapter process itself runs

The adapter is a long-lived daemon that must run as **res-gable** (so its
run-resident.sh invocation carries the res-gable uid the broker/nftables key
on). It needs its own systemd user unit — sibling to resident-cc.service — that
plink installs at the keyboard. Not written here because it's install/keyboard
territory; suggested unit name `gable-summon.service`. It needs network access
to the Disjorn port (already in the WP-H2 allowlist: loopback→Disjorn) and read
access to the config dir + key file.

## 4. Budget / cursor state paths must be writable by the adapter

`budget.state_path` and `cursor.state_path` default under `/home/resident`
(the res-gable home volume, rw). If the adapter runs outside the container,
point these at a res-gable-writable path in the config. No code change — config
only.

## 5. #custodian channel id

Defaulted to 4 (matches the broker.toml on this deployment,
`custodian_channel_id = 4`). If that changes, update `summon.custodian_channel_id`.
Flagging only so the two configs stay in sync.

## 6. BL-G1 model gate — what it cannot reach from here

`container.model_gate = "refuse"` kills the launched process the moment the
init event names the wrong model. In prod that process is run-resident.sh,
which fronts `podman run --rm` — killing it does not necessarily kill the
container, whose stdout then goes nowhere. The channel guarantee holds either
way (nothing the session produced is ever posted), but a refused session's
*side effects* could continue running inside the container until it exits or
hits `timeout_sec`. Closing that would need run-resident.sh to trap and
`podman kill` its container, or to run it with a name the wrapper can kill —
harness/cc/ territory (WP-H5), not this package's. Flagging, not fixing.

Same note for the `--model` flag reaching claude: the gate reads what CC
*resolved*, so if run-resident.sh ever drops the appended `--model <id>` the
gate reports it as a mismatch rather than silently running the account
default. That is the intended failure.

## Deferred (not needed for WP-H9)

- Concurrent summons: the daemon serves one summon at a time (expensive, and it
  keeps the typing keepalive + subprocess from racing other summons). A queue
  or per-channel concurrency is a later tuning item if summon volume warrants.
- Action-count fidelity: the summary's action count is whatever the launched CC
  session reports (`num_turns`/`action_count`); if a richer per-tool count is
  wanted it should come from the WP-H12 action-log, joined by session id — out
  of scope here.
