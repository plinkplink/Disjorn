# Spec: build seats and their subagents run Claude Opus 5.5

## Request
- **Verbatim**: "We also want to upgrade all build seats and subagents to Opus 5.5. This includes finding and replacing any docs/context that is outdated."
- **Requester**: plink
- **Origin**: keyboard session 2026-09-23 (announced in #custodian with the review request)

## Agreed UX
Every Claude Code build seat runs `claude-opus-5-5`: detached builds
(`start-build`, and `/build` from chat) and the apps builder. Every subagent
they spawn runs the same model. A subagent definition or a per-call `model`
parameter cannot choose another. Resident summons are unchanged: Gable stays
on `claude-fable-5-1`, and Claudette's adapter is its own spec.

## Architecture notes
- **Claude Code 2.1.280 in both images** (`harness/cc/Containerfile`,
  `Containerfile.apps`). It is the first release that knows the id; 2.1.278
  does not, which was checked in the binaries. npm `latest` is 2.1.280, while
  `stable` is still 2.1.267.
- **Pins.** Shipped templates, code defaults and the two tests that assert
  them: `[start_build].model` (`harness/broker/broker.toml`), `[runner].model`
  (`harness/cc/apps/launch.toml.example`), `DEFAULT_MODEL`
  (`disjorn-apps-launch`), the `run-apps.sh` default, and `APPS_DEFAULTS` in
  brokerd.py.
- **Subagents.** Both seats' managed settings (`harness/cc/build-config/`,
  `harness/cc/apps/config/`) set `CLAUDE_CODE_SUBAGENT_MODEL=claude-opus-5-5`
  and `CLAUDE_CODE_SUBAGENT_MODEL_FORCE=1`. Without these, built-in subagents
  already inherit the session model. The pin makes the house rule "Opus hands"
  mechanical instead of a default.
- **Effort: held for plink's ruling (#2844).** Claude Code starts Opus 5.5 at
  `medium`, where Opus 5 started at `high`. Deploy step 4 waits until plink
  rules `high` (hold) or `medium` (on purpose). The ruling is pinned in
  both managed settings as `modelSettings` and dated in the digest's record.
- **Deploy, after merge, in order:**
  1. `07-resident-image.sh` and the apps image rebuild (`10-appsbuilding.sh`).
  2. A probe: `claude -p --model claude-opus-5-5` in each image.
  3. Install the settings into `/srv/disjorn-build-config/{gable,claudette}/`.
  4. Live pins: `/etc/disjorn-broker/broker.toml` `[start_build]` and `[apps]`,
     and `/etc/disjorn-apps/launch.toml` `[runner]`, each with a backup.
  5. Restart the broker (no build in flight) and `gable-summon` for the new
     image.

## Lane → Review owner (DETERMINISTIC — filled from the lane, never preference)
- **Lane**: cross-lane (below).
- **Review owner**: by lane, per the split.

## Builder (USER PREFERENCE — who orchestrates; never touches Review owner)
- **Builder**: BuildGable, the keyboard seat.

## Cross-lane split
- **Applies**: yes
- **Surfaces by lane**:
  - custodian: `harness/broker/` (template pin, APPS_DEFAULTS, test), `harness/prose-baseline.toml` → review owner Claudette
  - gable: `harness/cc/` (both Containerfiles, apps runner pins, both managed settings, test) → review owner Gable
- **Split agreed in #custodian**: posted with the review request; one commit per lane.

## Expected diff tier
Tier 2 — build-seat model and the image both seats run.

## Token estimate
One keyboard session.

## Confirm record
- **Confirmed by**: plink, by instruction at the keyboard. His own one-line
  confirm in #custodian is owed, and it replaces the seq below.
- **#custodian seq**: 2842 (the keyboard line, posted by the keyboard seat)
- **Confirmed at**: 2026-09-23T07:19Z

## Status
`built@loop/2026-09-23-build-seats-opus-5-5`
