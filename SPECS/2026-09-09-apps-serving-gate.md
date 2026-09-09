# Spec: APPS v1 stage 3 — the serving gate, `live`, cards, remix

## Request
- **Verbatim**: "getting the apps tab initiative 100% in production" (plink, keyboard, 2026-09-09)
- **Requester**: plink
- **Origin**: parent spec `SPECS/2026-08-30-apps-tab-v1.md`, confirmed #custodian seq 2211; Round 13 names stage 3 = "serving gate on Funnel :8443 (currently mapped to :8080 — re-point), CSP, iframe, cards, share dialog, remix" and rules that stages 2 and 3 burn under that confirm. Broker FLAG #2454 (first real build had no UI to be viewed in) is the incident that makes this the critical path.

## Agreed UX (parent spec, Rounds 4–5, restated as the build target)
- The modal's preview panel holds a real iframe of the app. While a turn runs the frame is **frozen** (overlay, banner, stage bar, no pointer events); when the builder idles it **thaws** and the user can click the app. The frame reloads when a turn reaches `deployed`.
- **Live** is the user's explicit "done": one button, enabled when the builder idles and at least one turn has deployed. Only `live` deploys to the public root and posts cards. **Open** and **Share** enable at `live`. **Revert** appears once a previous live deploy exists. **Change something** focuses the composer when the builder idles.
- A shared app appears in a channel as a card: name, description, builder, Open. Anyone entitled can open it full-tab. The gate, not the URL, decides who gets in.
- Remix: from a card or the chooser, "Remix" copies the app into a new app the remixer owns, lineage recorded, and opens a build session on it.
- First open of someone else's app: dismissable banner "this is an app built in the house; the house will never ask for your password here" (parent Round 5, chrome-bar ruling).

## Walls (unchanged from the parent; this section says how each is enforced by code)
1. **Serving isolation**: apps origin = `https://<house-host>:<port>/<app-id>/`. **Port: 10000** (keyboard D1 below), not 8443 — Funnel's :8443 currently fronts Nextcloud on this host and re-pointing it is plink's NAS decision, not an apps one. Round 9's substance was "a separate port on the same host because there is no wildcard cert"; the number is one knob (`APPS_ORIGIN_BASE`). Re-rulable at review.
2. **Visibility is enforced, not promised** (Amendment B1): the gate serves nothing without a house-signed grant bound to one app id. The house mints grants only for entitled viewers: owner, `app_shares` members, or anyone when `visibility = public`. Grants expire.
3. **Per-app CSP on every response including errors**: `default-src 'self'; connect-src 'self'; form-action 'self'; base-uri 'self'; frame-ancestors <house-origin>`, plus `Referrer-Policy: no-referrer`, `X-Content-Type-Options: nosniff`. The gate sets its own cookie, never reads the house cookie, and the house never reads the gate's.
4. **Embed**: `<iframe sandbox="allow-scripts allow-same-origin">` and nothing else.
5. **The gate has no database and no house credential.** It holds one HMAC secret shared with the house and a read-only view of `/srv/apps-www`. It cannot mint; it can only verify.

## Architecture notes — keyboard decisions (D-series; each is a sentence Claudette can strike)

- **D1 Gate = second ASGI app in the server package**, `server/app/gate.py`, run by its own unit `deploy/disjorn-apps-gate.service` as `plink` on `127.0.0.1:8402`; Funnel `:10000 → 127.0.0.1:8402`. It imports nothing from `app.main`, `app.db` or the routers: a gate that could reach the house DB would be a house. Config from the same `server/.env`: `APPS_GATE_SECRET` (≥32 bytes, boot refuses shorter), `APPS_WWW_ROOT=/srv/apps-www`, `HOUSE_ORIGINS` (first entry = `frame-ancestors`), `APPS_ORIGIN_BASE`. The house reads the same three keys to mint.
- **D2 Grant = signed, stateless.** `base64url(json).base64url(hmac_sha256(secret, json))`, payload `{v:1, app, roots:["live"] | ["live","preview"], exp, ctx}` where `ctx` is the parent's context blob (`{version:1, user:{id,name,avatar_url}, shared_with:[...], theme:{}}`). `user.id` is the **opaque per-app id**: `base32(hmac(secret, f"{user_id}:{app_id}"))[:16]`, stable per (user, app), never the account id. Preview root is granted only to the owner. Grant TTL 12 h; the house mints a fresh one on every Open/embed, so revocation is bounded by the TTL.
- **D3 Entry and cookie.** `GET /<app>/?t=<grant>` (or `/<app>/preview/?t=`): verify, set `disjorn_gate=<grant>; Secure; HttpOnly; SameSite=Lax; Path=/<app>/; Max-Age=<until exp>`, 302 to the same URL with `t` stripped. Every other request reads the cookie, verifies signature + exp + that the cookie's `app` equals the path's first segment + that the requested root is in `roots`. Failure = 403 with the CSP headers and a one-line body naming the house as the place to open the app from. No cookie ever rides to another app's path.
- **D4 Roots and files.** `/<app>/…` serves `/srv/apps-www/<app>/live/…`; `/<app>/preview/…` serves `…/preview/…`. Directory → `index.html`. Dotfiles and traversal refused (resolve, then prefix-check against the root). No `live/` yet → 404 "not live yet" under the CSP. `Cache-Control: no-store` on HTML and on `__disjorn.js`; other files `private, max-age=60`.
- **D5 The context blob is a same-origin script, not inline.** The gate serves `/<app>/__disjorn.js` (and `/<app>/preview/__disjorn.js`) generated from the cookie's `ctx`: `window.__DISJORN__ = {...}`. When serving `index.html` the gate inserts `<script src="__disjorn.js"></script>` immediately after the first `<head>` (or before the first `<script>`, or at the top, in that order of fallback). Inline would violate the CSP the builder brief promises; the brief's "delivered before your scripts run" holds by insertion order. The brief's "in the preview there is no gate" sentence is now false and is amended: the preview is served by the gate too, with the owner's blob.
- **D6 `live`, `revert` and `remix` are host operations of the apps seat.** `disjorn-apps-launch` gains three modes, each running as `res-appsbuilding`, each shape-checked, each a fixed argv: `publish res-<x> <app>` (snapshot `preview/` → `live.tmp.<ts>` by rsync with the slice (i) flags, rotate `live` → `live.prev`, rename in; the previous `live.prev` is removed), `revert res-<x> <app>` (swap `live` and `live.prev`), `remix res-<x> <parent> <child>` (`git clone` parent's repo into `/srv/apps/<child>` with the parent's HEAD as the child's first commit, copy `preview/` if present; refuses if the child dir exists). Sudoers `92-disjorn-apps` gains one alias per mode for the `plink` uid. The **house server** calls them via `sudo -n` fixed argv from `POST /apps/{id}/live`, `/revert`, `/remix` — the server already runs as `plink`, the same uid the broker's line serves. Noted for review: this is the first time the server, not the broker, uses a sudoers line. The alternative (server marks, broker polls, like `stop`) adds a poll for an action with nobody waiting on a unit; ruled out at the keyboard, re-rulable.
- **D7 Server endpoints** (custodian lane): `POST /apps/{id}/open` → `{url}` (mints; `?root=preview` allowed for the owner only); `POST /apps/sessions/{sid}/live` (owner, session open, builder idle, ≥1 deployed turn; runs `publish`; sets `apps.status='live'`, posts stage `live` via the existing publisher path so the modal's bar and the §H system line fire; writes the B9 summary line into the build room as the `system` bot: app name, live URL, turns, files — the resident reads it in-room, which is the existing inbound path); `POST /apps/{id}/revert` (owner; `live.prev` must exist); `POST /apps/{id}/share {channel_id, visibility?}` (owner; adds every current member of that channel to `app_shares` at share time, sets `visibility='shared'` unless `public`, posts the card message as the sharing user); `POST /apps/{id}/remix` (viewer entitled to the app; mints child id, row with `parent_app_id`, runs `remix`, opens a session exactly as the chooser does and returns it); `GET /apps/{id}/card` (name, description, builder, status, owner name, `has_previous_live`, `parent`, `can_remix`). `GET /apps/config` gains `origin_base`.
- **D8 The card is a message.** Share posts a normal message authored by the sharing user: `shared an app: **<name>** — <origin_base>/<id>/`. The client renders any message body whose first apps-origin link matches `origin_base` as an `AppCard` (fetching `/apps/{id}/card`), with Open (→ `/open` then `window.open`) and Remix. No new message type, no schema change: the unfurl path already turns links into cards, and a client that predates this build shows a link that works.
- **D9 Screenshot at `live` (B8): DEFERRED, FLAG.** No headless browser on the host; adding Chromium to the box is plink's call. The card shows a generated tile (name initial on the theme colour). The seam is the `card` endpoint's `image_url`, null today.
- **D10 Client**: `AppBuildModal` gets the iframe (`src` from `/open?root=preview` at modal open, reloaded on `deployed`), the freeze/thaw overlay keyed on whether a turn is running (session `stage` in a running set, cleared by `files_written`/`deployed`/halted), Live / Open / Share / Revert / Change something wired as above, and — folding docket item 4 — the typing indicator (`usePresence(s => s.typing[channelId])`, the same subscription `ChatView` makes). `AppsChooserModal` gets Remix on entries that have a parent-able app. Share dialog = channel picker over the user's channels + visibility toggle.
- **D11 Funnel**: `tailscale funnel --bg --https=10000 http://127.0.0.1:8402` (exact flags verified at deploy). `:8443 → Nextcloud` untouched.

## Lane → Review owner (DETERMINISTIC — filled from the lane, never preference)
- **Lane**: custodian (server routers, gate, client) with a builder-lane strip (launcher modes, sudoers, unit file, `10-appsbuilding.sh`).
- **Review owner**: Claudette for the custodian surfaces; Gable for the launcher/sudoers/unit strip (the same split the parent and stage 2 used).

## Builder (USER PREFERENCE — who orchestrates; never touches Review owner)
- **Builder**: plink's keyboard lane (Fable orchestrates, Opus hands write) — the parent's ruling for every stage.

## Cross-lane split
- **Applies**: yes
- **Surfaces by lane**:
  - custodian: `server/app/gate.py`, `server/app/routers/apps.py`, client → Claudette
  - builder: `harness/cc/apps/disjorn-apps-launch`, `harness/keyboard/92-disjorn-apps.sudoers`, `deploy/disjorn-apps-gate.service`, `harness/keyboard/10-appsbuilding.sh`, `BUILDER-BRIEF.md` amendment → Gable
- **Split agreed in #custodian**: <pending — same split as stage 2; posted with the build banner>

## Expected diff tier
Tier 2 — new listening process, new sudoers aliases, new server endpoints.

## Token estimate
Large: ~1M build tokens across three hands (gate + tests; server + client; launcher + unit + sudoers + brief).

## Gates
Server suite green including gate tests (grant round-trip, expired, wrong app, wrong root, traversal, dotfile, CSP on 403/404, script insertion, cookie attributes); harness/cc suite green (three new launcher modes refused on every bad shape); a live proof on the host: `egnnhtz3ppwi` opened in the modal iframe (preview), pressed Live, opened full-tab on :10000 with `window.__DISJORN__` populated, shared into a channel, card rendered, Open from the card by a second account, revert, remix. Classifier at merge.

## Confirm record
- **Confirmed by**: plink
- **#custodian seq**: 2211 (parent confirm; Round 13: "stages 2 and 3 burn under this confirm")
- **Confirmed at**: 2026-09-05 (parent); stage 3 opened at the keyboard 2026-09-09

## Status
`building`
