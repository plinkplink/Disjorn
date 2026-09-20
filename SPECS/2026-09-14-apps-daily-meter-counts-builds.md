# Spec: the daily build meter counts builds, not dialogs (#26)

> **RETROACTIVE (keyboard-built).** Fixed at the keyboard 2026-09-14 on plink's
> word ("It's too expensive and time consuming to run three frontier models
> for a bugfix. Just fix #26."), merged as `79fae4f` under override-seq 2625.
> Review paid post-merge, below.

## Request
- **Verbatim**: "Just fix #26."
- **Requester**: plink
- **Origin**: keyboard session 2026-09-14; backlog #26 (#custodian seq 2616)

## Agreed UX
A build session counts against the daily cap once it has spent a turn, or
while it is still open with a live lock. A dialog opened and closed with no
turn is refunded. A lock that lapsed with no turn reads as ended.

## Architecture notes
- `_quota_for` in `server/app/routers/apps.py`: one query,
  `turns > 0 OR (ended_at IS NULL AND locked_until > now)`, the same liveness
  `_open_sessions_by_app` uses.
- Red-first test pins every branch. Parent spec 2026-08-30-apps-tab-v1 D5
  amended in place.
- Open seam, named in the commit: the broker's `apps-build` checks `open`,
  not `lock_lapsed`, so a turn can still land on a lapsed session and push a
  user over cap. Keyboard item, not yet built.

## Lane → Review owner (DETERMINISTIC)
- **Lane**: server. Review owner: **Claudette** by the broker's lane map; read
  by Gable at plink's summons (#2645), which the house accepted as the paid
  review.

## Builder
- **Builder**: keyboard (BuildGable).

## Expected diff tier
Tier 1 (one query and its test; no protected path).

## Review record
- Gable, #custodian seq 2647: PASS, no BLOCK. Query matches the liveness
  rule; refund on ceiling refusal confirmed; the `lock_lapsed` seam noted as
  a keyboard item; a turn halted at `scoped` after spawning counts, which is
  correct because tokens were spent.

## Confirm record
- **Confirmed by**: plink
- **#custodian seq**: 2625 (override-merge line)
- **Confirmed at**: 2026-09-14

## Status
`merged`
