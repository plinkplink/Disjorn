# Spec: Gable and the build seats run Claude Fable 5.2

## Request
- **Verbatim**: "Time to upgrade Gable to Fable 5.2. We should also upgrade the build agent harnesses to that model pin as well."
- **Requester**: plink
- **Origin**: keyboard session 2026-11-11

## Agreed UX
Gable's summons run `claude-fable-5-2`. Every Claude Code build seat runs the
same id: detached builds (`start-build`, and `/build` from chat) and the apps
builder. Every subagent they spawn runs it too, and a subagent definition or a
per-call `model` parameter cannot choose another.

This retires the "Opus hands" rule of 2026-07-21 for the build seats. That
rule was plink's design choice, and so is this. Claudette's adapter is not
touched: she stays on `claude-opus-5-5`.

## Architecture notes
- **Claude Code 2.1.322 in both images** (`harness/cc/Containerfile`,
  `Containerfile.apps`). 2.1.321 is the first release that knows the id;
  2.1.311 (npm `stable`), 2.1.316 and 2.1.319 do not, which was checked in the
  linux-x64 binaries. 2.1.322 is npm `latest`. `harness/cc/Containerfile` is
  also the resident image, so the CLI under Claudette's `resident-cc` moves as
  well; her model does not.
- **Probe before the build, 2026-11-11.** `claude -p --model claude-fable-5-2`
  on the 2.1.322 binary with res-gable's credential: init event, assistant
  event and `modelUsage` all name `claude-fable-5-2`.
- **Pins.** Shipped templates, code defaults and the two tests that assert
  them: `[container].model` (`harness/residency/summon.toml.template`),
  `[start_build].model` (`harness/broker/broker.toml`), `[runner].model`
  (`harness/cc/apps/launch.toml.example`), `DEFAULT_MODEL`
  (`disjorn-apps-launch`), the `run-apps.sh` default, and `APPS_DEFAULTS` in
  `broker_apps.py`.
- **Subagents.** Both seats' managed settings keep
  `CLAUDE_CODE_SUBAGENT_MODEL_FORCE=1`; `CLAUDE_CODE_SUBAGENT_MODEL` moves to
  `claude-fable-5-2`.
- **Effort.** Unchanged: Anthropic default by policy (plink #2858).
- **Two things that change with the model, neither in the diff:**
  - Price. Fable 5.2 is $8 / $40 per MTok against Opus 5.5 at $4 / $20, so a
    build costs about twice as much per token.
  - The safeguard gate. Fable is declined on flagged inbound context more
    often than Opus (KB-D14/KB-D15). Gable's summons have the pre-act model
    gate; the build seats report the model in the result and do not gate on
    it. A build whose context trips the gate may run on a substitute, and the
    ledger will say so after the fact.
- **Deploy, after merge, in order:**
  1. `07-resident-image.sh` and the apps image rebuild (`10-appsbuilding.sh`).
  2. Probes in each image: `claude -p --model claude-fable-5-2`, then one
     session under the seat's managed settings that spawns a subagent, with
     `modelUsage` read off the result event. Then the credential-cache
     recheck `config-template/README.md` asks for on a CLI bump: no
     `.credentials.json` in the resident home (Gable #3425).
  3. Install the settings into `/srv/disjorn-build-config/{gable,claudette}/`.
  4. Live pins, each with a backup: `/etc/disjorn-broker/broker.toml`
     `[start_build]` and `[apps]`, `/etc/disjorn-apps/launch.toml` `[runner]`,
     and `/srv/disjorn-resident-config/res-gable/summon.toml` `[container]`.
  5. Restart the broker (no build in flight), `gable-summon`, and Claudette's
     `resident-cc` for the new image.
  6. A live hail to Gable; the attribution header must read
     `claude-fable-5-2`, verified.
  7. Gable's spine lines that name his model or the Opus hands are his to
     change, after step 6.

## Lane → Review owner (DETERMINISTIC — filled from the lane, never preference)
- **Lane**: cross-lane (below).
- **Review owner**: by lane, per the split.

## Builder (USER PREFERENCE — who orchestrates; never touches Review owner)
- **Builder**: BuildGable, the keyboard seat.

## Cross-lane split
- **Applies**: yes
- **Surfaces by lane**:
  - custodian: `harness/broker/` (template pin, APPS_DEFAULTS, test) → review owner Claudette
  - gable: `harness/cc/` (both Containerfiles, apps runner pins, both managed settings, test), `harness/residency/summon.toml.template` → review owner Gable
- **Reviews**: custodian half approved by Claudette #3424, gable half PASS by Gable #3425, both at e3b4210.

## Expected diff tier
Tier 2 — build-seat model, Gable's model, and the image both seats run.

## Token estimate
One keyboard session.

## Confirm record
- **Confirmed by**: plink
- **#custodian seq**: 3427
- **Confirmed at**: 2026-11-11T17:58:54Z

## Status
built@loop/2026-11-11-seats-move-to-fable-5-2
