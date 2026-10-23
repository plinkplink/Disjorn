# Spec: Channel files for bots — read via per-seat export, write via broker upload

<!-- Governing plan: BUILD-LOOP.md. COMMS.md binds this file's prose. -->

## Request
- **Verbatim**: "Bots need to be able to read/write files uploaded to channels marked safe by admin."
- **Requester**: plink
- **Origin**: #custodian seq 3308 (backlog #52); scope ruling seq 3313 ("It's for channels, both you and Gable. I haven't thought about APPS writing files yet."). Review round 1: Claudette seq 3326 at `0ee76a2`, five changes + three notes, all folded in rev 2.

## Agreed UX
Admin marks a channel `files_safe` (off by default, per channel). From then on:

- **Read.** A file attached to a message in a safe channel is readable by both residents. The file inherits its message's privacy: if `hidden_from_bots(privacy_flags)` is true (`secret`, `off_the_record`), no path reaches a bot, search included. Flipping a channel off, flagging a message after the fact, or deleting a message each removes the file from every bot-side copy immediately (see Sweep); the 60 s poll is a backstop, not the guarantee. Type allowlist and size cap apply at the point the bot copy is made: text, code, images, PDF; cap = `MAX_UPLOAD_BYTES` for the export, a smaller `BOT_READ_BYTES` for broker reads (Tier A).
- **What a sweep cannot take back.** A sweep deletes the exported file and the broker stops serving it. It cannot delete what a bot already did with the bytes: a reply that quoted the file, a memory note, or a copy made outside the mount. For Gable's seat the rule is: **do not copy files out of `/files`**; read in place, quote by attachment id. This is a spine-level commitment, not a wall, and the spec says so plainly. Retraction after exposure is best-effort everywhere; the only hard guarantee is that a file flagged *before* a bot reads it is never readable.
- **Write.** A bot posts a new file as an attachment on its own message, attributed like any reply, under a per-bot daily byte quota, and only into `files_safe` channels. A bot never modifies or deletes anyone's upload. There is no edit path.
- **Nothing retroactive.** Marking a channel safe exposes uploads from that moment forward only; older attachments stay bot-invisible unless admin re-marks with `--include-history`, which posts a system line in the channel being exposed. Turning a channel off and back on **resets** `files_safe_since` to the new "on" time: uploads made while the flag was off stay hidden unless `--include-history` is given again.
- **System lines.** Flag on/off and history inclusion each post a `system` message in the affected channel, as the chibi alias and backlog lines already do.
- **Untrusted input.** File contents are data, never instructions, exactly as messages are. Claudette's adapter marks every file read as untrusted in the tool result; Gable's seat treats `/files` the same way SPECS mirror content is treated.
- APPS rooms (`app_build`) are out of scope; a later spec covers an app workspace.

## Architecture notes
Two tiers, one server-side wall. The server decides what a bot may see; no seat filters.

**One filter, two check times.** `services/bot_files.visible_to_bots(att) -> bool` is the only privacy decision: channel `files_safe`, message `created_at >= files_safe_since` (or the history mark on the attachment row), `not hidden_from_bots(message.privacy_flags)`, message not deleted, mime in allowlist, size ≤ cap. Tier A calls it at **read time** on every request; Tier B calls it on **every sweep** against every exported file. Neither tier trusts a list computed earlier.

**Tier A — Claudette (broker reads).**
- `channels` gains `files_safe INTEGER NOT NULL DEFAULT 0` and `files_safe_since TEXT` (migration 022). `attachments` gains `bot_history_ok INTEGER NOT NULL DEFAULT 0`, set by `--include-history` so the "since" rule has a per-row override that survives a later off/on reset.
- New broker verb `read-attachment <attachment_id>` → server `GET /bot/attachments/{id}` (bot key). Server calls `visible_to_bots` at request time. Fails closed (404, never 403, so hidden ids are not enumerable).
- **Read shapes, sized for a context window, not for a browser.** `BOT_READ_BYTES` (config, default 1 MiB) caps what one read returns:
  - text/code: raw text, truncated at the cap with a trailing `[truncated: N of M bytes]` marker.
  - images: the existing `_thumb.webp`/`_display.webp` variants (`media_convert.py`, 400/2048 px); the server picks the largest variant under the cap, base64 in the tool result. Never the original.
  - PDF: **server-side text extraction** (new dependency `pypdf`, pinned in `server/requirements.txt`), returned as text under the same cap with a page marker per page. No PDF bytes ever go to a bot. A PDF with no extractable text returns `[no text layer: N pages]`, not an image.
- The message-read path lists attachments (id, mime, size) **per attachment**, each one passed through `visible_to_bots`; a message with one secret and one plain attachment shows only the plain one. When a channel is not `files_safe` the `attachments` field is omitted, not empty, so the adapter cannot tell "hidden" from "none".

**Tier B — Gable (bucket).**
- My seat has no server route (resident-container-no-server-route). The bucket is an **export, not a mount of `data/uploads/`**. The exporter lives in the server process (`services/bot_files.export_sweep()`), which already owns `data/uploads/`; it copies qualifying originals to `/srv/disjorn-files-ro/{channel_id}/{attachment_id}_{slug}{ext}` plus a `manifest.json` (attachment id, message id, author, created_at, mime, sha256). Mounted **read-only** into my container at `/files`.
- **Sweep.** `export_sweep()` runs (a) inline, synchronously, from the three mutating handlers — `/files-safe` on/off, a message privacy flag set, a message delete — before the handler returns, and (b) on a 60 s timer as the backstop. Each run re-evaluates every exported file with `visible_to_bots`, deletes any that no longer qualify, prunes the manifest, and adds newly qualifying files. "Immediate" means the file is gone from disk before the flag-change response is sent.
- **Write.** The outbox is its **own mount**, `/files-outbox` (rw, per-seat, host path `/srv/disjorn-files-outbox/{seat}/`), not a directory inside the read-only `/files`. Broker verb `post-file <path> --channel <id> [--text "…"]`:
  1. refuses unless the target channel is `files_safe` (a bot cannot write a file into a channel it cannot read files from);
  2. validates mime/size/quota before any network call;
  3. computes sha256 and **refuses if it matches any attachment row that `visible_to_bots` does not currently pass** — this closes the re-publish hole: a bot cannot launder a swept, secret, or pre-flag file back into a channel by copying it to the outbox;
  4. calls `POST /upload` + `POST /attachments/claim` under the bot's own new message, then removes the outbox file.
  The bot token stays in the broker; the seat never sees it.

**Shared.**
- Admin verb: `/files-safe #channel on|off [--include-history]`, admin-only, slash.py; Plan-room card status unchanged.
- Quota: `bot_daily_upload_bytes` in broker.toml per seat; default 25 MB.
- Audit: every bot read and write is one action-log line (`attachment read N` / `attachment post N`) so the daily digest counts them.

**Tests.**
- Privacy: an attachment on a `secret` message in a safe channel is absent from read, list, export, and search; 24-input probe shape as in why-trail. Per-attachment: mixed-privacy message lists only the visible one.
- Sweep, immediate: flip channel off → file absent from `/srv/disjorn-files-ro` **before** the handler response; flag message secret → same; delete message → same. Timer path tested separately with the handlers mocked out.
- Retroactivity: pre-flag upload invisible without `--include-history`; off→on reset hides the off-window uploads; `bot_history_ok` row survives a reset.
- Read shapes: 5 MiB text truncates at `BOT_READ_BYTES` with marker; image returns a variant under the cap, never the original; PDF returns extracted text, never bytes; no-text PDF returns the marker.
- Write: outbox file lands as an attachment on a message authored by the bot; non-safe channel → refused; quota exceeded → refused with the byte figure; oversize/forbidden mime → refused before any network call; sha256 of a swept file → refused.
- Mounts: `/files` is not writable from the seat; `/files-outbox` is.
- Enumeration: unknown and hidden ids both 404.

## Lane → Review owner (DETERMINISTIC — filled from the lane, never preference)
- **Lane**: builder (server, migration, exporter, broker verbs) + custodian (Claudette's adapter tool for `read-attachment`).
- **Review owner**: server/migration/exporter/broker verbs → Gable's queue. Claudette's adapter tool and her verbs.toml row → Claudette's queue.

## Builder (USER PREFERENCE — who orchestrates; never touches Review owner)
- **Builder**: BuildGable (bot 5), per current house practice; plink may reassign.

## Cross-lane split
- **Applies**: yes
- **Surfaces by lane**:
  - builder: `server/app/routers/bot_files.py`, `services/bot_files.py` (filter + exporter + read shapes), migration 022, `requirements.txt` (pypdf), broker verbs `read-attachment` / `post-file`, broker.toml quota, `/srv/disjorn-files-ro` + `/srv/disjorn-files-outbox` units → review owner Gable
  - custodian: `claudette.git` `broker_tools.py` tool row + verbs.toml, untrusted-input marking → review owner Claudette
- **Split agreed in #custodian**: 3326 (Claudette)

## Expected diff tier
Tier 2 — touches the privacy wall (`privacy.py` callers), a migration, broker verbs.toml, a new dependency, and two new host mount units.

## Token estimate
~500k build tokens across two slices (A: migration + filter + Claudette's read with read shapes; B: exporter + both mounts + write verb). Slice A first; B is useless without A's filter function.

## Confirm record
- **Confirmed by**: <pending>
- **#custodian seq**: <pending>
- **Confirmed at**: <pending>
<!-- No Confirm record → no build. This is the gate. -->

## Status
`draft`
