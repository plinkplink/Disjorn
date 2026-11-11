# Spec: every Fable seat runs at effort high

## Request
- **Verbatim**: "what's the effort level of each Fable seat now? Anthropic suggests default to \"High\", so we should probably update those if they are still on \"Medium.\""
- **Requester**: plink
- **Origin**: keyboard session 2026-11-11, after the Fable 5.2 deploy

## Agreed UX
Gable's summons, both build seats and the apps builder send effort `high`.
Their subagents do too. Nothing else about a turn changes.

This replaces the effort half of plink's #2858 ruling (2026-09-23,
"Anthropic default by policy"). That ruling left the level to the CLI; the
CLI's choice for this model is `medium`, and Anthropic's guidance for
Fable 5.2 is to start at `high`.

## What was measured (2026-11-11, Claude Code 2.1.322)
Effort is not in the result event, so it was read off the request body, with
`ANTHROPIC_BASE_URL` pointed at a local capture endpoint.
- No seat sets effort anywhere: not in a settings file, an env file, or an
  argv. Checked in every file each seat loads.
- With nothing set, `claude -p --model claude-fable-5-2` sends
  `output_config.effort = "medium"`. Same with Gable's cached `.claude.json`,
  and in both output formats the seats use.
- `effortLevel: "high"` in a settings file moves it to `high`. So do a
  `modelSettings` entry and `CLAUDE_CODE_EFFORT_LEVEL`.
- One real call under the build seat's settings plus `effortLevel: "high"`,
  with one spawned subagent: the main requests and the subagent's request all
  carried `high`. One small request with no tools still carried `medium`; it
  is a side call of the CLI's, not the turn.

## Architecture notes
- **One key, top level**: `"effortLevel": "high"` in
  `harness/cc/build-config/settings.json`, `harness/cc/apps/config/settings.json`
  and `harness/cc/config-template/settings.json` (Gable's summon seat). Not
  `modelSettings`: that is keyed by model id and would stop applying, silently,
  at the next pin move.
- **Test**: the assertion that no effort key exists becomes one that all three
  files carry `high`, and that neither other spelling is present.
- **Cost**: `high` spends more tokens per turn than `medium`. Not measured
  here.
- **Summon wall clock**: Gable's summons have a 600 s limit. `high` turns run
  longer; a timeout after this change is this change until shown otherwise.
- **Deploy, after merge, in order:**
  1. Apps image rebuild (`10-appsbuilding.sh`); its settings are baked in.
  2. Install the build settings into
     `/srv/disjorn-build-config/{gable,claudette}/`, with backups.
  3. Add the key to `/srv/disjorn-resident-config/res-gable/settings.json`,
     in place, with a backup. That file differs from the template (an extra
     directory, formatting), so it is edited, not overwritten.
  4. Restart `gable-summon`. Build and apps turns read settings at launch.
  5. Capture the request body once per seat in its own image, where the
     network allows it; report what was not observed as not observed.
  6. A live hail to Gable.

## Lane → Review owner (DETERMINISTIC — filled from the lane, never preference)
- **Lane**: gable (`harness/cc/`).
- **Review owner**: Gable.

## Builder (USER PREFERENCE — who orchestrates; never touches Review owner)
- **Builder**: BuildGable, the keyboard seat.

## Cross-lane split
- **Applies**: no

## Expected diff tier
Tier 2 — managed settings of every seat.

## Token estimate
One keyboard session.

## Confirm record
- **Confirmed by**: pending
- **#custodian seq**: pending

## Status
built@loop/2026-11-11-fable-seats-effort-high
