# Spec: a bot seat's tool schemas list only the verbs its section lists

## Request
- **Verbatim**: "I agree with Claudette's Option 2 ruling on `broker_tools.py`. We'll close that out before updating her model."
- **Requester**: plink
- **Origin**: #custodian seq 2808 (BuildGable's DECIDE, option 2), 2811 (Claudette), 2826 (plink)

## Agreed UX
Claudette's `broker_tools.py` is regenerated from the live `verbs.toml` and
carries exactly the verbs her `[res-claudette]` section lists. She gains
`changed_files` and the current `refresh_mirror` description. She does not
gain `summon_hop` or `apps_build`, which her section does not list.

A key set to `false` still yields a tool that answers `verb-disabled`,
because the generator never reads the booleans. Today that is
`restart_disjorn`, which is already in her list. Deleting the key is how a
tool leaves a seat.

## Architecture notes
- `harness/broker/gen_verb_surface.py`: `seat_surface(verbs_path, seat)` and
  `emit-tools --seat res-<name>`. It refuses a section that is not a
  `res-*` seat or is absent. It refuses a listed verb that
  `verb_surface.toml` does not describe. `--seat` is refused in every other
  mode. Without `--seat`, output is byte-identical to today's.
- In seat mode the union drift check is skipped, because the live file's union
  legitimately omits verbs no seat holds yet (`summon-hop`). `check` and the
  test suite still run the union check against the repo template.
- `verbs.toml` template header: the regeneration recipe names `--verbs` and
  `--seat`.
- Deploy: regenerate `/home/plink/bots/claudette/broker_tools.py` with
  `--verbs /etc/disjorn-broker/verbs.toml --seat res-claudette`, commit on
  `disjorn-port`, then run `claudette-update.sh`, which restarts `resident-cc`
  once.

## Lane → Review owner (DETERMINISTIC — filled from the lane, never preference)
- **Lane**: custodian — the generator that decides Claudette's tool list, and her `broker_tools.py`.
- **Review owner**: Claudette.

## Builder (USER PREFERENCE — who orchestrates; never touches Review owner)
- **Builder**: the keyboard (Claude Opus 5.5, posting as BuildGable).

## Expected diff tier
Tier 2 — `harness/broker/`, and it decides a resident's tool surface.

## Token estimate
One keyboard session.

## Confirm record
- **Confirmed by**: plink
- **#custodian seq**: 2826
- **Confirmed at**: 2026-09-23T06:35:58Z

## Status
`built@loop/2026-09-23-per-seat-tool-surface`
