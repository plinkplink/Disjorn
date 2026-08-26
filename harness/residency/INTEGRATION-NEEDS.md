# INTEGRATION-NEEDS — WP-H9 residency adapter

Changes this package needs OUTSIDE harness/residency/. Written here (not made)
because harness/residency/ is this WP's exclusive file territory. Each item is
a keyboard/config or a sibling-WP task, not adapter code.

**Status 2026-07-22 (Gable went live), recorded here 2026-07-26:** §1–§5 are
**CLOSED**. This file carried no markers until now, so §1 in particular has been
reading as a live activation blocker for four days after it was cleared —
Gable has been a running resident since 2026-07-22 (`harness/KEYBOARD-NEXT.md`
§5, "DONE; verified live 2026-07-22"). §6 is a flagged limitation, not a task.
Verified from the keyboard 2026-07-26:

- **§1 CLOSED — no longer a blocker.** Bot id 2 is named **`Gable`** in the live
  roster, and his key file is in place plink-owned `0640 plink:res-gable` at
  `/srv/disjorn-resident-config/res-gable/gable-key`.
- **§2 CLOSED — `podman run --rm` per summon is the settled shape** (confirmed
  as the intended one, not `podman exec`), and the in-container command reads
  the prompt on stdin: live `session_argv` is
  `bash -lc "…bootstrap.py >&2 && exec claude -p --output-format stream-json
  --verbose \"$@\"" cc-session`. No in-image wrapper was needed.
- **§3 CLOSED.** `gable-summon.service` — the suggested name was taken — runs
  under res-gable's own user manager and is `active`.
- **§4 CLOSED.** Both state paths point into the res-gable-writable volume:
  `/home/res-gable/resident-home/.summon-budget.json` and `.summon-cursor.json`.
- **§5 CLOSED — the two configs agree.** `summon.custodian_channel_id = 4` and
  `/etc/disjorn-broker/broker.toml` `custodian_channel_id = 4`.
- **§6** is unchanged as a statement of what this package cannot reach, but the
  "keeps running forever" case it worries about is now covered by the container
  reaper in `harness/cc/` (see KB-D11/KB-D12 in RED-TEAM-BACKLOG.md); the
  refusal-latency sliver remains open there, not here.

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

## 7. The wake lane (2026-08-25 agentic residents) — OPEN, keyboard work

`run_wake.py` and `gable-wake.service` ship here; nothing in this package can
install or arm either. Five keyboard acts, in this order, none of which a
resident can perform or reach:

1. **broker.toml**: add plink's own uid to `[uids]` (`"1000" = "plink"`) and
   fill in `[wake]` — `callers`, `residents`, `spool_dir`, `session_cap_sec`,
   `grace_sec`. The broker REFUSES TO START if the spool is resident-writable,
   if a listed caller has no uid, or if a listed caller is a `res-*` seat.
2. **Create the spool**: `/var/lib/disjorn-broker/wake-spool`, plink-owned,
   0755 (the broker writes it; res-gable must be able to READ it and must not
   be able to write it). Records land 0644.

   **PRE-ARM CHECK (2026-08-25 wake-pre-arm-riders, items 1 and 2), both
   verified before `[wake]` is uncommented**: the day's wake cap is in force
   (`[wake].daily_wake_cap` — omitting it accepts the default of 3, it does not
   remove the cap), and `[wake].state_path` in this seat's `summon.toml` is
   OUTSIDE the home volume the container mounts (`~/resident-home` on the host,
   `/home/resident` inside). Both are startup refusals — the broker will not
   come up on a resident-writable spool, the wake runner will not come up on a
   state_path in the volume — so the check here is that neither refusal is what
   tells you on the day you arm it.
3. **verbs.toml**: `[plink] "wake" = true`. It ships `false`.
4. **This seat's config** (`summon.toml`): the `[wake]` block — `spool_dir`
   pointing at the same directory, `state_path` in res-gable's home but NOT in
   `resident-home/` (e.g. `/home/res-gable/.wake-served.json`; a path inside the
   volume refuses to start), `gatehouse_dir` (the bare repos the seat pushes
   loop branches into),
   and `action_log` (the HOST path of this seat's `~/.action-log`, i.e.
   `/home/res-gable/resident-home/.action-log`).
5. **Install `gable-wake.service`** under res-gable's user manager, sibling to
   `gable-summon.service`. Separate unit on purpose: a wake holds a session for
   up to its cap, and sharing the summon adapter's loop would mean either a
   summon queued behind an hour-long wake or two sessions racing one container
   name.

What is NOT wanted and must not be added to make this easier: a cron entry, a
chat trigger, or a verb that lets a resident enqueue a wake. Human-initiated
only is the whole of v1 (spec, decision 2).

## Deferred (not needed for WP-H9)

- Concurrent summons: the daemon serves one summon at a time (expensive, and it
  keeps the typing keepalive + subprocess from racing other summons). A queue
  or per-channel concurrency is a later tuning item if summon volume warrants.
- Action-count fidelity: the summary's action count is whatever the launched CC
  session reports (`num_turns`/`action_count`); if a richer per-tool count is
  wanted it should come from the WP-H12 action-log, joined by session id — out
  of scope here.
