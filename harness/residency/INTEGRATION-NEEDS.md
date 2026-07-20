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
  prompt from **stdin**, run non-interactively, and print the reply as JSON on
  **stdout** (e.g. `claude -p --output-format json`, whose `result` /
  `num_turns` keys the launcher already parses). If the chosen CC invocation
  can't take stdin, a tiny in-image wrapper script is the integration point —
  it belongs in the resident image (WP-H5), not here.
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

## Deferred (not needed for WP-H9)

- Concurrent summons: the daemon serves one summon at a time (expensive, and it
  keeps the typing keepalive + subprocess from racing other summons). A queue
  or per-channel concurrency is a later tuning item if summon volume warrants.
- Action-count fidelity: the summary's action count is whatever the launched CC
  session reports (`num_turns`/`action_count`); if a richer per-tool count is
  wanted it should come from the WP-H12 action-log, joined by session id — out
  of scope here.
