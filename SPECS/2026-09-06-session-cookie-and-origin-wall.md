# Spec: Session cookie hardening and Origin wall

## Request
- **Verbatim**: "Claudette, we should do the house cards first. Do you have enough to spec those in one build?"
- **Requester**: plink
- **Origin**: #custodian / seq 2231

## Agreed UX
Humans are logged out exactly once when this deploys and log back in normally. Nothing else changes for them. Bots see no change at all: they authenticate by `X-API-Key` or the first-frame WS auth op, never by cookie, and the rule is keyed on the cookie. A browser tab running app JS at `:8443` can no longer make an authenticated write to the house or open an authenticated WebSocket to it.

## Architecture notes
Apps v1 serves untrusted house-built JS from `https://<house-host>:8443`. Cookies ignore ports, so app JS can write cookies for the bare host that ride to the house at `:443`, and a WS handshake from `:8443` carries the house cookie and authenticates today (`ws.py:590` — no Origin check, no CORS middleware anywhere). Worst case is login-CSRF by a member who already holds a valid token, plus a shadow-cookie forced logout. This is the house-side wall. It gates apps going live on `:8443`; it does not gate the apps build card.

**Change 1 — `__Host-` cookie prefix.** Rename the session cookie slot to `__Host-disjorn_session`. The prefix forces `Secure`, `Path=/`, no `Domain`, so a JS write must target the identical slot and `HttpOnly` rejects it; the longer-Path shadow-cookie variant becomes impossible. Server code reads `COOKIE_NAME` at `auth.py:45` everywhere, so the rename is one constant; `delete_cookie` already sends `Path=/` and `Secure`. The name is also pinned at `tests/test_auth.py:61`, `:108`, `:120`, `tests/test_password.py:142` and `:143`, and named in a comment at `client/src/ws.ts:3`.

**Change 2 — `COOKIE_SECURE` boot assertion.** `config.py:22` defaults it False. A prefixed cookie sent without `Secure` is dropped silently by the browser; the symptom is a login loop with nothing in any log. Assert true at startup and refuse to boot otherwise. Verified true in prod at Round 11 — the assertion is so it stays that way.

**Change 3 — Origin middleware.** Keyed on the session cookie, not on the route (per Gable, mirror 6e68128): a route-wide check would log out every bot. Rule: if a request carries the session cookie AND is an unsafe method, or is the `/ws` handshake, its `Origin` must exactly match a configured house origin — absent or mismatched is a 403. No cookie: fall through to normal auth, untouched.

`main.py` has no middleware today, and Starlette HTTP middleware never sees the websocket scope. Implement as **one pure ASGI middleware covering both scopes**; if that proves awkward, the fallback is HTTP middleware plus an explicit check in `ws_endpoint` before `accept()` at `ws.py:587`. Reject before accept so it is a 403 handshake and not a post-accept close code.

`HOUSE_ORIGINS` is an exact-string-match list from config, never a suffix check. v1 value: `["https://debian.tailca81ba.ts.net"]` and nothing else; `:8443` explicitly excluded. Dev origins are added to the list, never special-cased in code. **An empty list is a house-wide write lockout with no log, so refuse to boot on an empty list** — same treatment as `COOKIE_SECURE`.

**Measured vs asserted.** Strict absent-Origin rejection is safe because our API is JSON-only via fetch, which always sends `Origin`, and browsers always send it on cross-origin unsafe requests and on WS handshakes. Pre-2020 Chrome and older Safari omitted it on *same-origin form* POSTs — that is asserted from spec knowledge, not probed against actual house clients; a browser that old fails elsewhere here first. If the strict rule ever bites, the fallback is allow-absent with a WARN log, not a route exemption.

**Test-harness consequences (both are load-bearing; the builder must not improvise these).**
- `tests/conftest.py:25` pins `COOKIE_SECURE=false` because httpx never returns Secure cookies over `http://test`. The boot assertion would refuse every test app, and flipping the env alone would drop every session cookie. So: conftest `base_url` becomes `https://test`, `COOKIE_SECURE` is true in tests, and the `"Secure" not in set_cookie` pin at `tests/test_auth.py:63` inverts.
- Strict absent-Origin would 403 every cookie-authenticated unsafe call in the suite — fourteen test files make them. So: the conftest client sends a default `Origin` header equal to the configured house origin.

## Lane → Review owner (DETERMINISTIC — filled from the lane, never preference)
- **Lane**: custodian
- **Review owner**: Claudette

## Builder (USER PREFERENCE — who orchestrates; never touches Review owner)
- **Builder**: resident build hand — Claudette presses `start_build` once the confirm record is filled.

## Expected diff tier
Tier 2 — touches auth, session cookies and the request path for every route.

## Token estimate
Small. One constant rename across six pinned sites, two boot assertions, ~15–25 lines of ASGI middleware, conftest changes, and the tests below.

## Tests
- Cookie name pins updated at `test_auth.py:61`, `:108`, `:120`, `test_password.py:142`, `:143`; the Secure pin at `test_auth.py:63` inverted.
- Boot: `COOKIE_SECURE` false refuses to start. Empty `HOUSE_ORIGINS` refuses to start.
- Middleware: cookie + unsafe method + good Origin passes; + bad Origin 403s; + absent Origin 403s.
- No-cookie `X-API-Key` request with no Origin passes on all three body-less routes (`leave`, `join`, `logout`) — these are the simple requests that fire with cookies and no preflight, which is why the middleware exists rather than a wall per route.
- WS handshake with cookie and an `:8443` Origin is rejected **before** `accept()`, as a 403 handshake.
- WS handshake with cookie and the house Origin still connects; bot first-frame auth op still connects with no Origin.

## Deploy note
Prod `.env` gets `HOUSE_ORIGINS=["https://debian.tailca81ba.ts.net"]` before the restart, or the house refuses to boot. The rename logs every human out once — say so in channel before restarting. Bots ride through on `X-API-Key`.

## Confirm record
- **Confirmed by**: plink
- **#custodian seq**: 2244
- **Confirmed at**: 9/6/2026

## Status
`confirmed`