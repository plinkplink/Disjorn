# Spec: Account recovery without email, and the three open auth findings

> **RETROACTIVE (keyboard-built).** Built on `fix/2026-10-06-account-recovery` during the 2026-10-05/06 backlog sprint as a keyboard fix branch with no spec file. plink asked "which spec file is this?" on 2026-10-13, and this file is the answer. Review was paid before merge (below).

## Request
- **Verbatim**: "Self-service password reset flow" (backlog #19), plus the three findings carried forward in `SPECS/2026-09-15-password-reset.md`'s Review record:
  1. The rotation gate exists only over HTTP.
  2. `POST /auth/login` has no throttle.
  3. An admin reset re-authenticates nothing and leaves no record.
- **Requester**: plink (backlog #19, 2026-09-02); findings by Claudette (#2649).
- **Origin**: backlog #19; #custodian #3253–#3256.

## Agreed UX
- **Asking for a reset.** The house has no outgoing email. The login page's "Forgot your password?" disclosure gains "Ask an admin to reset it": a username and an optional note of up to 200 characters.
  - The answer is always the same sentence, whether or not the account exists.
  - Admins get a push. Settings → "Reset a password" lists open requests on top, with "Reset now" and "Dismiss".
  - A request's note is labelled "Unverified — anyone can file this. The first request's note:".
  - A reset, from Settings or `cli.py reset-password`, closes the request.
- **After a reset.** The user's next login shows once: "Your password was reset by <admin> on <date>."
- **Throttled sign-in.** Repeated failures answer 429 with Retry-After. A successful sign-in is never throttled by one's own earlier typos from the same address.
- **Ended sessions end everywhere.** A session that is deleted, expired or owes a password change loses its live socket too, within a minute, and the client signs out without looping.

## Architecture notes
- Migration `021_password_reset_requests.sql` adds two tables:
  - `password_reset_requests`: at most one open request per account.
  - `account_events`: who reset whom and when, never the password.
- `POST /auth/reset-requests` (unauthenticated):
  - Limited to 3 per hour per address and per username.
  - It answers 202 before it looks the username up. The lookup, insert and push run in a background task.
- `services/throttle.py`:
  - Counters are in-process sliding windows, because the server is one uvicorn process.
  - `attempt()` checks and counts every limiter with no await in between, so a parallel burst can't slip past.
  - Login limits per 15 minutes: 5 per (address, username), 50 per username, 10 per address.
  - An IPv6 client is keyed by its /64.
  - Client addresses come from uvicorn's forwarded-header trust, which is 127.0.0.1 only.
- `ws.py`:
  - The socket uses the HTTP gate's session lookup.
  - Each user socket keeps its session token and re-checks it on every op and every 60 s. It closes 4401 for a dead session and 4403 for a rotation owed.
  - An admin reset closes the account's sockets at once.
- Client:
  - A 4403 close stops reconnecting and shows the change-password screen.
  - A 4401 close asks `/me` before it reconnects.
  - Any API 401 except a failed login signs the user out.

## Lane → Review owner (DETERMINISTIC)
- **Lane**: server and client. Review owner: **Claudette**.

## Builder
- **Builder**: keyboard (BuildGable) with Opus hands.

## Expected diff tier
Tier 2 (`server/app/routers/auth.py`, `server/cli.py`, `server/app/ws.py`).

## Review record
- Claudette, #custodian 3254: BLOCK. A burst of parallel logins skipped the throttle, because the count came after an await. Fixed in `e0fbfda`-line.
- Claudette, #custodian 3256: PASS at `ee3dc2e`. Her IPv6 note is folded: v6 addresses are keyed by /64.
- Left open, her notes:
  - A self password change leaves other sockets open for up to 60 s.
  - Reset requests can be crowded out by someone rotating addresses. The admin still sees that a reset was asked for.

## Confirm record
- **Confirmed by**: plink, at the keyboard, 2026-10-13 ("let's get 1-5 out the door"). This is the Tier 2 signature the review asked for.
- **#custodian seq**: 3256 (the PASS); the signature is a keyboard instruction and is recorded in the merge closeout post.
- **Confirmed at**: 2026-10-13

## Status
`merged`
