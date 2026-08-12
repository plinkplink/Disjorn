# Spec: Password change and first-login rotation

## Request
- Verbatim: "there's no ability to change passwords, so I have every users password forever. Not great." (plink, #custodian, 2026-08-08)
- Requester: plink
- Origin: #custodian (channel 4), 08-08 regroup; gate on inviting additional humans.

## Agreed UX
A logged-in user changes their own password by supplying the current one and a new one. Success signs out every OTHER session they hold and keeps the calling one alive. An account created by an admin is marked as needing rotation: the user can log in with the password the admin handed over, but every authenticated surface answers 403 with a "password change required" marker until they set their own. After that nobody but the user knows it, which is the whole ticket.

## Architecture notes (from the tree)
1. NO NEW CRYPTO. hash_password / verify_password already exist (argon2id, argon2-cffi) and are shared with cli.py. The endpoint calls those two. Login already does check_needs_rehash, so KDF upgrades are someone else's story already told.
2. ENDPOINT: POST /auth/password, body {current_password, new_password}, gated on get_current_user. Verify current first; wrong current is 403 and changes nothing. Deliberately NOT a PATCH on /me — a password is not profile data and must not share a body with display_name.
3. SESSION INVALIDATION IS THE HALF THAT MATTERS: DELETE FROM sessions WHERE user_id = ? AND token != ?, keeping the caller's cookie. Without it, a change evicts nobody holding a stolen or handed-over session and the feature is theater. This is the acceptance test I care most about.
4. FIRST-LOGIN ROTATION: add users.must_change_password (bool, default 0); cli.py account creation sets 1. get_current_user gains a check — with the flag set, every route except POST /auth/password, GET /me and POST /auth/logout raises 403 with a distinguishable detail so a client can route to the change form. Flag clears in the same transaction as the hash update.
5. ADMIN RESET, NARROW: an admin may set another user's password (lockout recovery); doing so sets must_change_password = 1 and kills all that user's sessions. Admins cannot read a password — there is nothing to read. This is the only admin verb over another account's credentials.
6. VALIDATION, BORING ON PURPOSE: min length 12, no composition rules, no expiry, no reuse history; new must differ from current. Anything cleverer stays out of scope.
7. BOTS UNTOUCHED. Bot API keys are SHA-256 and rotate through the existing admin surface; key rotation is a separate ticket and bots_admin.py does not widen here.

## Open questions for the reviewer (both cheap, both mine to default if unanswered)
- Is an admin reset surfaced to the affected user, or is it enough that their sessions die and they're forced to rotate? Default proposed: no notification surface in v1.
- Do EXISTING accounts get must_change_password = 1 at migration? Default proposed: yes for human accounts, because "plink has every password forever" is only fixed once the current ones rotate too. Say the word and I flip it.

## Lane -> Review owner (DETERMINISTIC)
- Lane: server tree (accounts/auth). Not Gable's host-build surface; disjoint files from per-channel membership (seq 978). Review owner: Gable, plink's sign-off as the Tier 2 human gate.

## Builder
- Resident-built (Claudette), one build slot.

## Expected diff tier
Tier 2 (authentication surface).

## Token estimate
One slot. Small implementation, test-heavy: wrong-current-password, session eviction, flag gating, admin reset, migration of existing rows.

## Confirm record
- **Confirmed by**: plink
- **#custodian seq**: 1006
- **Confirmed at**: 2026-08-12

## Status
confirmed
