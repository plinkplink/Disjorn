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

`harness/cc/Containerfile` is also the resident image, so the CLI under
Gable's summons and under Claudette's `resident-cc` moves to 2.1.280 as well.
Their models do not change. Step 5 restarts her container too: the digest
reads the image, not running containers, so a container left on 2.1.267
would be invisible to it.

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
- **Effort: Anthropic default by policy, plink #2858, 2026-09-23.** Neither
  managed settings file carries an effort key, and a test asserts that. A CLI
  upgrade can move build effort with nothing in our diff showing it; that is
  the policy, and it is dated in SUBSTRATE-LOG.
- **Deploy, after merge, in order:**
  1. `07-resident-image.sh` and the apps image rebuild (`10-appsbuilding.sh`).
  2. Probes in each image: `claude -p --model claude-opus-5-5`, then one
     session under the seat's managed settings that spawns a subagent, with
     `modelUsage` read off the result event (Gable #2855). Anything that is
     not observed there is reported as not observed, not as pinned.
  3. Install the settings into `/srv/disjorn-build-config/{gable,claudette}/`.
  4. Live pins: `/etc/disjorn-broker/broker.toml` `[start_build]` and `[apps]`,
     and `/etc/disjorn-apps/launch.toml` `[runner]`, each with a backup.
  5. Restart the broker (no build in flight), `gable-summon`, and Claudette's
     `resident-cc` for the new image.
  6. After step 5, Gable's two accepted spine lines (#2855), in one spine
     commit, so the spine never states a pin that is not live.

## Lane → Review owner (DETERMINISTIC — filled from the lane, never preference)
- **Lane**: cross-lane (below).
- **Review owner**: by lane, per the split.

## Builder (USER PREFERENCE — who orchestrates; never touches Review owner)
- **Builder**: BuildGable, the keyboard seat.

## Cross-lane split
- **Applies**: yes
- **Surfaces by lane**:
  - custodian: `harness/broker/` (template pin, APPS_DEFAULTS, test), `harness/prose-baseline.toml` → review owner Claudette
  - gable: `harness/cc/` (both Containerfiles, apps runner pins, both managed settings, tests), `harness/residency/summon.toml.template`, `DEFERRED.md` → review owner Gable
- **Split agreed in #custodian**: posted with the review request (#2842); PASS
  custodian half Claudette #2844, gable half Gable #2855.

## Expected diff tier
Tier 2 — build-seat model and the image both seats run.

## Token estimate
One keyboard session.

## Confirm record
- **Confirmed by**: plink
- **#custodian seq**: 2860
- **Confirmed at**: 2026-09-23T07:34:59Z

## Deploy record (2026-09-23)
1. Resident image `91d5a0a3f6eb` and apps image `84ec2d751ee4`, both Claude
   Code 2.1.280, loaded into every seat store. `10-appsbuilding.sh` reports no
   drift, and the installed launcher equals main.
2. Probes, with modelUsage keys observed in each result event:
   - build seat as res-gable under the new managed settings: a plain call
     gave `claude-opus-5-5` only;
   - the same with a spawned subagent that asked for haiku: still
     `claude-opus-5-5` only, since FORCE hides the per-call model choice;
   - apps image: `claude-opus-5-5` only.
   Effort is not observable from the result event; no key is set, by ruling.
3. `/srv/disjorn-build-config/{gable,claudette}/settings.json` equal the
   repo (backups `.bak-pre-opus-5-5-20260923`).
4. Live pins moved to `claude-opus-5-5`, with backups beside each file:
   `/etc/disjorn-broker/broker.toml` `[start_build]` and `[apps]`, and
   `/etc/disjorn-apps/launch.toml` `[runner]`.
5. The broker, `gable-summon` and `resident-cc` restarted at 08:32:46Z. Her
   container runs `91d5a0a3f6eb`.
6. Gable's spine lines: bots/fable `5f0dc3b`, mirror republished.

## Status
merged
<!-- advanced from `built@loop/2026-09-23-build-seats-opus-5-5` by `board --mark-merged` on 2026-09-23: build merged as 006ddbf. The word `built@loop/2026-09-23-build-seats-opus-5-5` on a merged spec made it indistinguishable from a buildable one. -->
