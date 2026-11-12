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
  3. computes sha256 and **refuses if it matches any attachment row that `visible_to_bots` does not currently pass** — this stops a verbatim (byte-identical) copy only: a bot cannot post back the exact bytes of a swept, secret, or pre-flag file by copying it to the outbox. A copy changed by one byte passes; nothing here closes that;
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
- **Confirmed by**: plink
- **#custodian seq**: 3485
- **Confirmed at**: 2026-11-11
<!-- No Confirm record → no build. This is the gate. -->

## Build record, slice A
- Branch `loop/2026-10-15-channel-files-for-bots`. Slice B (export sweep, `/files` and `/files-outbox` mounts, `post-file`, quota) is not built.
- The spec's "migration 022" is `023_channel_files_for_bots.sql` on the branch; main took 022 for another change.
- `server/app/services/bot_files.py`: `visible_to_bots(att)` (async, the conjunction above with size ≤ `MAX_UPLOAD_BYTES`), `for_bot(payload)` (the bot listing), `set_channel_safe`, `read(attachment_id)` (the three read shapes under `BOT_READ_BYTES`).
- `server/app/routers/bot_files.py`: `GET /bot/attachments/{id}`. 404 for an id that is unknown, hidden, or has no readable shape (an image with no stored variant under the cap, a PDF pypdf cannot open).
- The read route requires the reading bot's membership of the file's channel, by the same `is_member` rule as the message reads, and answers 404 without it. The broker sends `?on_behalf_of=<seat>` stamped from the seat's peer credentials; the server takes it only from a bot on `FILES_RELAY_BOT_NAMES` (default `broker`), reads `res-<name>` as the bot named `<name>`, and checks that bot's membership. Any other bot sending the field gets 404. Each attempt is one server log line: caller, reading bot, attachment id, allowed or refused.
- Accepted inference: when a file is attached, a bot in the channel still receives a `message_edit` frame whose content is unchanged, so it can infer that something was attached, never what.
- `/files-safe` on a private channel requires the admin's own membership of it; otherwise the reply is the no-such-channel refusal.
- Bot listing `[{id, mime_type, size_bytes}]` on `GET /channels/{id}/messages`, `GET /search` and the WS `message_create` / `message_edit` frames sent to bots. Attachments are not indexed by search.
- `/files-safe #channel on|off [--include-history]` in `slash.py`; the channel is `#main` or a named text channel.
- Broker verb `read-attachment`, OFF in both seat sections of the `verbs.toml` template (the template lists every seat verb in every seat section); audit summary `attachment read <id>`.
- A truncated PDF ends `[truncated: page P of T]`; a page marker is `[page N]`. Extraction stops at `BOT_READ_BYTES` or 200 pages, whichever comes first.
- Tests: `server/tests/test_bot_files.py`, `harness/broker/tests/test_broker_attachments.py`.

## Build record, slice B
- Branch `loop/2026-10-15-channel-files-for-bots-b`. No migration, no new dependency.
- **Exporter.** `services/bot_files.export_sweep()`. Settings: `BOT_FILES_EXPORT_ROOT` (default `/srv/disjorn-files-ro`), `BOT_FILES_EXPORT_SEATS` (default `["res-gable"]`), `BOT_FILES_SWEEP_SECONDS` (default 60; 0 runs no timer). A file is at `<root>/<seat>/{channel_id}/{attachment_id}_{slug}{ext}`, mode 0640, written as `.tmp-<name>` and renamed. `manifest.json` in each channel directory is a list of `{attachment_id, message_id, author, created_at, mime_type, file, sha256}`, rewritten only when it changes.
- **Deviation, per-seat layout and membership.** The spec's paths carry no seat and slice A's review added the rule that a bot reads a file only in a channel it is a member of. The export is laid out per seat and a seat's directory holds only channels its bot is a member of, by the same `is_member` call as the read route. The seat's bot is found by the read route's name rule, now one function, `bot_files.seat_bot`: `res-<name>` is the one bot whose lowercased name is `<name>`. A seat with no one such bot, or a label that is not `res-<name>`, exports nothing.
- **A seat directory is never created by the server.** A seat whose directory does not exist, or is a symlink, has no export and its sweep does nothing. The host step that creates the directory is what turns the export on.
- **Where the sweep runs.** Inline, awaited before the handler answers: `/files-safe` (after `set_channel_safe`), `PATCH /messages/{id}` (after the UPDATE, when the stored flags hide the message from bots), `DELETE /messages/{id}`, a channel's hard delete (`_destroy_channel`, which also covers leaving a bot DM), and `DELETE /channels/{id}/bots/{bot_id}`. Also once at server start, and on the timer, an asyncio task the lifespan starts and cancels. The server had no periodic task before this one. Upload and claim run no sweep: a new file appears on the next sweep of any kind.
- **Order, fail closed.** A handler commits its change and then awaits the sweep; nothing else runs between the two. One lock serialises sweeps, so a handler's sweep always starts after any sweep that was already copying. Within a sweep: every file, directory, symlink and leftover temp file the current decision does not name is removed; then each manifest is rewritten without the removed files; only then are missing files copied and the manifest rewritten again. A sweep that raises anywhere empties that seat's whole directory and logs; `export_sweep` never raises into a handler. A copy that fails is logged, its partial file removed, and the next sweep retries it. A manifest that cannot be written is removed. The worst case is a file removed too eagerly and re-added by the next sweep. The one case not covered is the server process dying between the commit and the end of its sweep: the file stays until the start-up sweep.
- **Mounts.** `harness/cc/run-resident.sh` mounts `${RESIDENT_FILES_RO:-/srv/disjorn-files-ro/res-<name>}` at `/files:ro` and `${RESIDENT_FILES_OUTBOX:-/srv/disjorn-files-outbox/res-<name>}` at `/files-outbox`, each only when the directory exists. `run-build.sh` is unchanged and mounts neither.
- **Deviation, who posts.** The broker holds one key, its own bot's; a resident's key lives in that resident's adapter. `post-file` therefore goes to a new server route, `POST /bot/attachments`, as the broker's bot with `on_behalf_of=<seat>` stamped from the caller's peer identity, exactly as `read-attachment` does. The server takes the field only from a bot on `FILES_RELAY_BOT_NAMES`, and creates the upload, the message and the claim as the seat's bot by calling the `/upload` and `/attachments/claim` handlers in-process. No other caller can post through the route. A post that fails after the upload deletes the staged row and its stored files.
- **One server decision, in the spec's order.** (1) the channel is `files_safe` and the seat's bot is a member of it, one refusal sentence for unknown, closed and not-a-member; (2) type, size, quota; (3) sha256; (4) the write. Refusals are 403 with a sentence the seat reads. Each attempt is one server log line: caller, posting bot, channel, attachment id, allowed or the refusal.
- **Deviation, quota.** `bot_daily_upload_bytes` is read per seat from `[budgets.<seat>]` in broker.toml (default 26214400) and sent with the post. The server counts the day's bytes from the bot's own attachment rows (`uploader`, `created_at` since 00:00 UTC) and refuses in the same request, so the count survives a broker restart and cannot drift. The quota is therefore checked after one network call, not before; the broker refuses, before any network call, a forbidden type and a file larger than the whole daily quota. The refusal reads `daily upload quota is Q bytes; U are used today and this file is S`.
- **sha256 without a column.** The check hashes the stored original of every attachment row whose `size_bytes` equals the posted file's and that `visible_to_bots` does not pass, staged uploads included. A same-size hidden row that cannot be hashed refuses the post. Bytes that are hidden in one place are refused even if the same bytes are also visible in another. The check stops a byte-identical copy only: a hidden file re-encoded, trimmed or changed by one byte has another hash and is posted.
- **Types on post.** The type is guessed from the file's name alone, by `mimetypes`, and must be on slice A's read allowlist. `text/html`, `text/xml`, `application/xml` and `image/svg+xml` are refused on post although readable: the media route serves an original inline from the house origin. The broker's copy of the three lists is pinned to the server's by test.
- **The verb.** `post-file --path /files-outbox/<name> --channel <id> [--text "…"]`: the generated CLI has flags only, so the path is a flag. The outbox is flat: one file directly in it, no subdirectory. The broker opens `<[paths].files_outbox_root>/<seat>/<name>` without following a link at either level, and reads it only if `fstat` says a regular file owned by a uid `[uids]` maps to the calling seat, of 1 byte to the quota. The outbox file is removed after the server accepts it. OFF in both seat sections of the `verbs.toml` template; audit summary `attachment post <id>`.
- Tests: `server/tests/test_bot_files_export.py`, `harness/broker/tests/test_broker_attachments.py`, `harness/cc/tests/test_run_wrappers.py`. The immediate-sweep tests read the export directory from an ASGI wrapper at the moment the response starts. The mount tests assert the podman argv; nothing was run under podman.

## Build record, turn-on hardening
- Branch `fix/2026-11-11-files-turn-on-hardening`. Migration `026_bot_file_posts.sql`; 025 is `025_apps_delete.sql` on a branch in flight. No new dependency. Nothing is turned on: both verbs stay OFF and no channel is files-safe.
- **Attribution on a posted file.** `POST /bot/attachments` takes an optional `attribution`, validated by the message route's own `Attribution` model (unknown key or wrong type 422, `model` and `summoner` 100 characters each). The server stores `{model, verified: false, summoner}` whatever `verified` said, and `{}` when neither `model` nor `summoner` is given. A posted file may therefore be unattributed.
- **Where the values come from.** The summon adapter launches each session with `DISJORN_SUMMON_MODEL` (its model pin, empty when unpinned) and `DISJORN_SUMMONER` (the trigger's summoner) in the environment; `run-resident.sh` passes each into the container by name when it is non-empty; the broker CLI reads them for `post-file` and sends them as the verb's `model` and `summoner` args. The CLI has no flag for either, so no text a model types reaches the field by ordinary use. `verb_surface.toml` gains the arg key `env` for this: an optional, cli-only arg read from the named variable, dropped when it fails its rules. The arg is absent from a bot seat's tool schema. The model sent is the pin, not what the session reported, and a session can set either variable itself: this is why the path never stores `verified: true`. A wake session and a bot seat that passes neither arg post unattributed.
- **A removal that fails.** `_remove` answers whether anything is still at the path; an entry that still exists is stuck whatever its size or mode, because the entry's name is built from the uploader's filename and the folder is listable. A file that will not unlink is opened read-only without following a link, set to mode 0200, truncated through a second open checked to be the same inode, set to mode 000, and unlinked again; the read-only first open closes a file that has lost its owner write bit. A file whose inode has another link is set to mode 0600 and not truncated, so a stored original linked there by mistake keeps its bytes. A directory has every file in it treated so.
- **A stuck entry's name.** When an entry is stuck, the channel folder that lists it is set to mode 0700, so the seat can neither list nor enter it; a channel folder that cannot itself be removed is the one shut. A stuck entry makes the sweep raise before it rewrites a manifest or copies a file, which empties the seat's directory by the existing path, and what is still there after that is one ERROR line, `N entries that must go are still in the export of <seat>; the next sweep retries`. No line on the removal path names a file. `export_sweep` still never raises into a handler.
- **How a shut folder opens again.** It is not chmodded back. A pass that finds a channel folder with no group or other bits removes the whole folder; once that succeeds, the same pass makes it again with `mkdir` at `EXPORT_DIR_MODE` and copies its files. The reason is the setgid bit: the host's seat directory is mode 2750 with the seat's group, a folder made inside it takes that group and the bit, and the server's user is not in that group, so the kernel drops the bit on any chmod the server makes. A folder made again has it; a folder chmodded back would not, and files copied into it afterwards would carry the server's group and be unreadable to the seat.
- **The seat directory's mode is never changed**, for the same reason: the server could not put its setgid bit back. Two things follow. The name of a shut channel folder, which is a channel id, stays listed. An entry stuck directly in the seat directory is emptied and reported but its name stays listed; the server writes only channel folders there, so such a name is not an uploader's.
- **A retry.** The broker sends `idempotency_key`, the sha256 of the JSON list `[channel, name, text, sha256(file)]`; nothing is stored beside the outbox file. The server keeps `(bot_id, idempotency_key) → (attachment_id, message_id, created_at)` in `bot_file_posts`. A post whose key the same bot used in the last 24 hours, and whose message is not deleted, is answered with the first post's result before any check, and writes nothing. Rows older than 24 hours are deleted when a post is recorded. One lock serialises `POST /bot/attachments`, so a retry that arrives while its first attempt is still running waits and gets that attempt's answer; the lock also makes the quota check and the write one step. A post without a key behaves as before.
- **Not covered by the key.** The server dying between the message insert and the key insert leaves a post with no key, and a retry posts again. A key that cannot be stored is logged and the post is still answered. A retry after the message was deleted is a new post, which the sha256 check then refuses for the same bytes.
- **Timeout and encoding.** The broker waits `[disjorn].file_post_timeout_sec` (default 120) for `POST /bot/attachments`; every other call keeps 20 seconds. The file still travels as base64 in JSON. The broker's HTTP helper is a JSON call behind a callable the tests stub; multipart needs no new dependency but would add a second transport and change the route's shape, and is not built.
- **Sweep cost.** `visible_to_bots(att, facts=None)` takes the five message and channel columns it reads when the caller's query already joined them; given none, it runs its own query as before. `visible_rows()` is the sweep's one query: every attachment in a files-safe channel with those columns, each row decided by `visible_to_bots(att, att)`. There is no second copy of the rule. A sweep runs, per seat, one query for the seat's bot, one for the rows, and the membership queries of each channel that has a visible file.
- **Ceiling.** `BOT_FILES_EXPORT_MAX_BYTES` (default 2 GiB) is per seat. Files are taken newest first by attachment id; one that does not fit in what the newer ones left is not exported, and a smaller, older one may still fit. Sizes are the rows' `size_bytes`. A file already exported that no longer fits is removed. One WARNING per change of figures names the ceiling, the files and bytes left out, and the bytes exported.
- **Wording.** Write step 3 and the sha256 line above said the check closes the re-publish hole. It stops a byte-identical copy only.
- Tests: `server/tests/test_bot_files_export.py`, `harness/broker/tests/test_broker_attachments.py`, `harness/broker/tests/test_verb_surface.py`, `harness/cc/tests/test_broker_cli.py`, `harness/cc/tests/test_run_wrappers.py`, `harness/residency/tests/test_attribution.py`, `harness/residency/tests/test_launcher.py`. The removal tests fail `os.unlink` for one name, and for one case also every open for writing of it. They read the folder as the seat would by its mode bits: the tests run as the folder's owner, and a folder with no group or other bits counts as unlistable. Nothing was run under podman.
- Deploy: migration 026 at server restart; broker restart; `harness/residency` and `run-resident.sh` synced to their deployed copies; the resident image rebuilt, because the broker CLI's generated table changed.

## Status
merged
<!-- advanced from `built@loop/2026-10-15-channel-files-for-bots-b (slice b)` by `board --mark-merged` on 2026-11-12: build merged as 1935aac. The word `built@loop/2026-10-15-channel-files-for-bots-b (slice b)` on a merged spec made it indistinguishable from a buildable one. -->
