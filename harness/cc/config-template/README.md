# config-template — per-resident /config content (WP-H5)

This directory is the TEMPLATE for `/home/plink/resident-config/<name>/`,
which plink creates at the keyboard, owns, and which run-resident.sh mounts
read-only at `/config` inside the resident's container. It is the
outside-the-container lever: every file here changes resident behavior and
none of them can be edited from inside.

Install per resident (keyboard, plink):

    install -d -m 0755 /home/plink/resident-config/claudette
    cp -r config-template/. /home/plink/resident-config/claudette/
    chmod 0755 /home/plink/resident-config/claudette/hooks/*.py

The config dir (and everything in it except `env`) must be world-readable:
the res-* uid reads it through the ro mount. `env` holds the API key —
make it readable by that resident's uid only (e.g. `chown
plink:res-claudette env; chmod 0640 env`).

## Contents

- `settings.json` — Claude Code settings. Symlinked inside the image as
  `/etc/claude-code/managed-settings.json`, i.e. managed policy the
  resident's own `~/.claude` settings cannot override. Permissions: file
  tools allowed in `/home/resident`, `broker` + dev tooling allowed in
  Bash; deny sudo/su, network clients (curl/wget/nc/socat/ssh/scp/rsync),
  podman/systemctl, WebFetch/WebSearch, and any write into `/config`.
  Honest note: the Bash deny list is hygiene — the real egress wall is host
  nftables keyed on the res-* uid (WP-H2), and the real privilege wall is
  the broker (WP-H3). Hooks are wired here and live in `hooks/`.
- `hooks/pre-tool-use.py` — deterministic PreToolUse gate: blocks raw
  broker-socket access, blocks `broker` invocations carrying
  `[[CHAT]]...[[/CHAT]]` channel-text markers (adapter contract, WP-H9/H11),
  enforces wall-clock cap + daily action budget from `budget.json`.
- `hooks/action-counter.py` — PostToolUse: appends one JSON line per tool
  call to `/home/resident/.action-log` (WP-H12 counting; never blocks).
- `hooks/session-start.py` — SessionStart: records session start time,
  prints kernel hash + today's budget status into context.
- `budget.json` — `daily_action_cap`, `wall_clock_cap_min`. plink tunes.
- `CLAUDE.md` — placeholder + kernel assembly contract (WP-H7 writes the
  real kernel to `~/.claude/CLAUDE.md` in the home volume).
- `env` — NOT in the template, created by plink per resident:

      ANTHROPIC_API_KEY=sk-ant-...
      # kill switch: uncomment to make the broker CLI refuse everything
      # BROKER_DISABLE=1

## Kill switches, ranked

1. `systemctl --user -M res-<x>@ stop resident-cc` — the whole residence.
2. `verbs.toml` (broker side, /etc) — per-verb, per-resident. Default OFF.
3. `env`: set `BROKER_DISABLE=1` — all broker calls refuse client-side.
4. `settings.json` deny rules / hook edits — tool surface. Container
   restart required for env changes; settings/hooks are re-read per session.
