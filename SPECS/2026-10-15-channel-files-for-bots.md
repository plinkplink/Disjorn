# Spec: Channel files for bots — read via per-seat export, write via broker upload

<!-- Governing plan: BUILD-LOOP.md. COMMS.md binds this file's prose. -->

## Request
- **Verbatim**: "Bots need to be able to read/write files uploaded to channels marked safe by admin."
- **Requester**: plink
- **Origin**: #custodian seq 3308 (backlog #52); scope ruling seq 3313 ("It's for channels, both you and Gable. I haven't thought about APPS writing files yet.")

## Agreed UX
Admin marks a channel `files_safe` (off by default, per channel). From then on:

- **Read.** A file attached to a message in a safe channel is readable by both residents. The file inherits its message's privacy: if `hidden_from_bots(privacy_flags)` is true (`secret`, `off_the_record`), no path reaches a bot, search included. Flipping a channel off, or flagging a message after the fact, removes the file from every bot-side copy within one sweep interval. Type allowlist and size cap apply at the point the bot copy is made: text, code, images, PDF; cap = `MAX_UPLOAD_BYTES`.
- **Write.** A bot posts a new file as an attachment on its own message, attributed like any reply, under a per-bot daily byte quota. A bot never modifies or deletes anyone's upload. There is no edit path.
- **Nothing retroactive.** Marking a channel safe exposes uploads from that moment forward only; older attachments stay bot-invisible unless admin re-marks with `--include-history`, which posts a system line in the channel.
- **System lines.** Flag on/off and history inclusion each post a `system` message in the channel, as the chibi alias and backlog lines already do.
- APPS rooms (`app_build`) are out of scope; a later spec covers an app workspace.

## Architecture notes
Two tiers, one server-side wall. The server decides what a bot may see; no seat filters.

**Tier A — Claudette (broker reads).**
- `channels` gains `files_safe INTEGER NOT NULL DEFAULT 0` and `files_safe_since TEXT` (migration 022).
- New broker verb `read-attachment <attachment_id>` → server `GET /bot/attachments/{id}` (bot key). Server checks: channel `files_safe`, message `created_at >= files_safe_since` (or history flag), `not hidden_from_bots`, mime in allowlist, size ≤ cap. Fails closed (404, never 403, so hidden ids are not enumerable). Text and code return as text; images and PDFs return the display variant bytes, base64 in the tool result.
- The existing message-read path lists attachment ids + mime + size only when the channel is safe; otherwise the `attachments` field is omitted, not empty, so the adapter cannot tell "hidden" from "none".

**Tier B — Gable (bucket).**
- My seat has no server route (resident-container-no-server-route). The bucket is an **export, not a mount of `data/uploads/`**. A server-side exporter (same process family as the SPECS mirror ff) copies qualifying originals to `/srv/disjorn-files-ro/{channel_id}/{attachment_id}_{slug}{ext}` plus a `manifest.json` (attachment id, message id, author, created_at, mime, sha256). Mounted read-only into my container at `/files`.
- Export filter is the same function as Tier A's server check — one implementation, `services/bot_files.visible_to_bots(att)`, called by both.
- **Sweep.** Every 60 s and on every flag change: re-evaluate every exported file; delete any that no longer qualify; prune the manifest. A flag flip is therefore honoured within ≤60 s on disk. A message hard-delete removes the file in the same sweep.
- **Write.** Outbox dir `/files/outbox/` (rw, per-seat). Broker verb `post-file <path> --channel <id> [--text "…"]` validates mime/size/quota, calls `POST /upload` + `POST /attachments/claim` under the bot's own new message, then removes the outbox file. The bot token stays in the broker; the seat never sees it.

**Shared.**
- Admin verb: `/files-safe #channel on|off [--include-history]`, admin-only, slash.py; Plan-room card status unchanged.
- Quota: `bot_daily_upload_bytes` in broker.toml per seat; default 25 MB.
- Audit: every bot read and write is one action-log line (`attachment read N` / `attachment post N`) so the daily digest counts them.

**Tests.**
- Privacy: an attachment on a `secret` message in a safe channel is absent from read, list, export, and search; 24-input probe shape as in why-trail.
- Sweep: flip channel off → file gone from `/srv/disjorn-files-ro` within one tick; flag message after export → same.
- Retroactivity: pre-flag upload invisible without `--include-history`.
- Write: outbox file lands as an attachment on a message authored by the bot; quota exceeded → refused with the byte figure; oversize/forbidden mime → refused before any network call.
- Enumeration: unknown and hidden ids both 404.

## Lane → Review owner (DETERMINISTIC — filled from the lane, never preference)
- **Lane**: builder (server, migration, exporter, broker verbs) + custodian (Claudette's adapter tool for `read-attachment`).
- **Review owner**: server/migration/exporter/broker verbs → Gable's queue. Claudette's adapter tool and her verbs.toml row → Claudette's queue.

## Builder (USER PREFERENCE — who orchestrates; never touches Review owner)
- **Builder**: BuildGable (bot 5), per current house practice; plink may reassign.

## Cross-lane split
- **Applies**: yes
- **Surfaces by lane**:
  - builder: `server/app/routers/bot_files.py`, `services/bot_files.py`, migration 022, `harness/exporter/files_export.py`, broker verbs `read-attachment` / `post-file`, broker.toml quota, `/srv/disjorn-files-ro` unit → review owner Gable
  - custodian: `claudette.git` `broker_tools.py` tool row + verbs.toml → review owner Claudette
- **Split agreed in #custodian**: <seq — pending>

## Expected diff tier
Tier 2 — touches the privacy wall (`privacy.py` callers), a migration, broker verbs.toml, and a new host mount unit.

## Token estimate
~450k build tokens across two slices (A: server + Claudette tool; B: exporter + bucket + write verb). Slice A first; B is useless without A's filter function.

## Confirm record
- **Confirmed by**: <pending>
- **#custodian seq**: <pending>
- **Confirmed at**: <pending>
<!-- No Confirm record → no build. This is the gate. -->

## Status
`draft`
