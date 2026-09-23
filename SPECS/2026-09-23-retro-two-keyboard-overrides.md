# Spec: retro review of two keyboard overrides (8fff470 CSS padding, 9ed0e51 CC pin)

> **RETROACTIVE (keyboard-built).** Two small changes merged on override lines
> whose reviews were never recorded, so their Plan Room cards sat in Review:
> `af17330` carried override-seq 2303 for `8fff470`, and `60948805a830` carried
> override-seq 2513 for `9ed0e51`. Both merged. Reviews are recorded below.

## Request
- **Verbatim**: "can we clean up the board? What loose ends are left?"
- **Requester**: plink
- **Origin**: keyboard session 2026-09-23 (board cleanup before the footer build)

## Agreed UX
No change. Both commits have been live since 2026-09-06 and 2026-09-10.

## Architecture notes
- `8fff470` (client/src/app.css): `.add-channel-btn` becomes
  `.icon-btn.add-channel-btn`, so the later `.icon-btn` rule no longer wins
  its padding at equal specificity. Both uses (AppShell.tsx:826, :845)
  carry both classes, so the compound selector matches every button it
  styled before.
- `9ed0e51` (harness/cc/Containerfile): `CLAUDE_CODE_VERSION` 2.1.259 →
  2.1.267, plink's own edit after the daily lag-check. Superseded by
  `4c51e0a` (2.1.280, both images).

## Lane → Review owner (DETERMINISTIC — filled from the lane, never preference)
- **Lane**: client (`8fff470`) and harness/cc (`9ed0e51`).
- **Review owner**: Claudette for client, Gable for harness/cc. Neither read
  was taken at the time. The override cards named plink (seq 2303) and
  BuildGable (seq 2513).

## Builder (USER PREFERENCE — who orchestrates; never touches Review owner)
- **Builder**: keyboard (`8fff470`); plink (`9ed0e51`).

## Expected diff tier
Tier 1 (`8fff470`, client CSS). Tier 2 (`9ed0e51`, resident image pin).

## Review record
- Keyboard (BuildGable seat), 2026-09-23, on `af17330` / `8fff470`: PASS.
  Specificity fix only, no `!important`, every use carries both classes.
- Keyboard (BuildGable seat), 2026-09-23, on `60948805a830` / `9ed0e51`: PASS.
  One ARG line, matched the image rebuilt that day, since superseded by
  `4c51e0a`.
- These are keyboard reads paid at plink's cleanup ask, not the lane
  owners' reads. plink can strike either and re-open its card by deleting
  its line here.

## Confirm record
- **Confirmed by**: plink
- **#custodian seq**: 2303 (`8fff470`), 2513 (`9ed0e51`) — the override lines
- **Confirmed at**: 2026-09-06, 2026-09-10

## Status
`merged`
