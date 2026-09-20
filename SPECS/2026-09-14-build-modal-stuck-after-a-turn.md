# Spec: a finished turn is done, and the confirm dialog is on top

> **RETROACTIVE (keyboard-built).** Fixed at the keyboard 2026-09-14 on the
> same standing as #26 (a bugfix is not a three-model round), merged as
> `1b5e68b` under override-seq 2630, client rebuilt. Review paid post-merge,
> below.

## Request
- **Verbatim**: keyboard finding on session 13, 2026-09-14: after a one-turn
  build was published the modal kept Stop on screen, the clock kept counting,
  and Stop / End session looked dead.
- **Requester**: plink
- **Origin**: keyboard session 2026-09-14 (#custodian seq 2630)

## Agreed UX
After a turn that wrote files, the build modal shows the turn as done: Stop
goes away, the clock freezes, End session ends without a confirm dialog. When
a confirm dialog does open, it is on top of the modal and its buttons work.

## Architecture notes
- `client/src/stores/apps.ts`: the turn fold marks a turn `done` on the
  `deployed` event, not on `files_written`, so the frame stays frozen until
  the copy has landed. Halts and `no_changes` already set `done`.
- `client/src/components/AppBuildModal.tsx`: the confirm dialog's backdrop
  sits at z-index 41, between the modal's 40 and the corner notice's 50.
- No client test runner exists; both fixes are unpinned.

## Lane → Review owner (DETERMINISTIC)
- **Lane**: client. Review owner: **Claudette** by the broker's lane map; read
  by Gable at plink's summons (#2645), accepted as the paid review.

## Builder
- **Builder**: keyboard (BuildGable).

## Expected diff tier
Tier 1 (client only, no protected path).

## Review record
- Gable, #custodian seq 2647: PASS, no BLOCK. `deployed` is always reached
  for a committed turn; z-index band is free. NOTE: after Live the frozen
  clock shows the publish time, cosmetic and pre-existing.

## Confirm record
- **Confirmed by**: plink
- **#custodian seq**: 2630 (override-merge line)
- **Confirmed at**: 2026-09-14

## Status
`merged`
