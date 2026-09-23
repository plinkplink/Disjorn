# Spec: a reset-password path for locked-out accounts

> **RETROACTIVE (keyboard-built).** Built at the keyboard 2026-09-15 as
> `868bde6`, merged `0696898`, override trailer `0b5bf3b` citing seq 2636.
> Review paid post-merge, below.

## Request
- **Verbatim**: "A user has forgotten their password. Can you reset 'Albert'
  password?" / "Can you add a Reset Password function to the login system?"
- **Requester**: plink
- **Origin**: keyboard session 2026-09-15 (#custodian seq 2636)

## Agreed UX
- Login page: a "Forgot your password?" disclosure saying there is no email
  reset; an admin hands over a temporary password and the user chooses their
  own on first login.
- Settings, admins only: "Reset a password" picks an account, takes or
  generates a temporary password, and shows what to tell the user. Self is
  not offered.
- Keyboard: `cli.py reset-password <username>`.

## Architecture notes
- `GET /auth/users` (admin only) feeds the picker: id, username, display
  name, admin bit, rotation-pending flag. No hashes.
- The existing `POST /auth/users/{id}/password` does the reset: argon2 hash,
  rotation owed, every session ended. The CLI verb mirrors it.
- Tests: the list route's gates and shape; the CLI verb end to end.

## Lane → Review owner (DETERMINISTIC)
- **Lane**: server and client. Review owner: **Claudette**.

## Builder
- **Builder**: keyboard (BuildGable).

## Expected diff tier
Tier 2 (`server/app/routers/auth.py` and `server/cli.py` are protected).

## Review record
- Claudette, #custodian seq 2649: PASS, merge stands. The eviction half is
  present and the migration test is the real upgrade path. Findings carried
  forward: (1) the rotation gate exists only over HTTP, a rotation-pending
  user can still open `/ws` and read the feed, filed as proposal seq 2657;
  (2) `POST /auth/login` has no throttle; (3) admin reset re-authenticates
  nothing and leaves no record.

## Confirm record
- **Confirmed by**: plink
- **#custodian seq**: 2636 (override-merge line)
- **Confirmed at**: 2026-09-15

## Status
`merged`
