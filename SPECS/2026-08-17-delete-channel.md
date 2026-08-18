# Spec: Delete channel — owner or admin, hard delete, `channel_delete` event

> **RETROACTIVE (keyboard-built).** This was built at plink's keyboard on
> 2026-08-17 and merged to main as `e26f750` (server + SDK) and `54e490e`
> (client) before any spec existed. Gable flagged it (#custodian seq 1363) and
> Claudette verified it (seq 1365): a destructive verb on the channel surface
> reached prod with no confirm record. This file is the record it should have
> had, written 2026-08-18 by the keyboard, describing what IS on main — plus
> the two review findings, which are adopted as amendments below and NOT yet
> built. Review is post-merge, by the fact of the matter.

## Request
- **Verbatim**: "I need to clean up the pre-existing channels, though. We need
  a Delete Channel UI component."
- **Requester**: plink
- **Origin**: keyboard session with the Claude Code seat, 2026-08-17 (no
  #custodian seq — that is the defect this file corrects)

## Agreed UX
Discord's shape. A channel's `⋮` menu ends with a red **Delete channel** item
(behind a separator) for text channels only, shown to the channel's owner
(`created_by`) or an admin. It opens a confirm dialog: "Are you sure you want
to delete #name? This will permanently delete the channel and all of its
messages. This cannot be undone." with a field "Type the channel name to
confirm"; the red button arms only when the typed name matches exactly. On
success the channel vanishes for everyone who could see it; whoever was
reading it gets a corner toast "#name was deleted." Nobody gets a modal.
`main_feed` and DMs can never be deleted.

## Architecture notes
1. `DELETE /channels/{id}` in `server/app/routers/channels.py`;
   `_require_owner_or_admin`. RULED by plink 2026-08-17: **admins may delete
   channels they do not own** — deletion leaks no content, so architecture
   note 5 of the membership spec (no silent god-view) is untouched; kick and
   invite stay owner-only.
2. Hard delete in one transaction. `PRAGMA foreign_keys` cascades
   `channels -> channel_members / messages -> attachments`; the
   `messages_fts` AFTER DELETE trigger fires on cascaded rows too, asserted
   from both ends in tests (search goes quiet, `MATCH` returns nothing, FTS
   integrity-check passes). Attachment FILES on disk are left as orphans,
   documented, no sweeper.
3. Bus event `channel_delete` with the recipient list computed BEFORE the row
   goes (`is_member` answers False for everyone afterwards): everyone for a
   public channel; members + the acting user + every admin for a private one
   (admins carry the ghost row in their sidebar). WS frame
   `{type, channel_id, by_user_id, channel{id,type,name,visibility}}`; SDK
   `ChannelDelete`; Architecture §4.1 documents it.
4. Client (`54e490e`): `⋮` item, `DeleteChannelModal`, `ChannelDeletedToast`,
   `stores/channelDelete.ts`; teardown is idempotent between the deleter's own
   200 and its own frame; vanished-channel fetches 404 quietly.
5. Tests: `server/tests/test_channel_delete.py`, 11 tests (owner, admin
   non-owner, ordinary non-owner 403, main_feed/DM 400, unknown 404, cascade
   + FTS, WS public/private/admin fan-out). Suite 288.

## Post-merge review findings — ADOPTED as amendments, not yet built
- **A1 — protected channels (Claudette, seq 1365).** `#custodian` is a text
  channel, so it is deletable by owner-or-admin, and every confirm record in
  every spec cites a `#custodian` seq: deleting it destroys the audit root of
  the build-authorization chain. Amendment: the server carries a
  **protected-channels list** (config, not a hardcoded `type != 'text'`),
  seeded with `#custodian`; a protected channel refuses `DELETE` with a
  truthful message, and the client hides the item for it.
- **A2 — durable record of deletion (Claudette, seq 1365).** The house's first
  clawback (kick is not redaction; revocation is forward-only) currently
  leaves only an ephemeral WS frame. Break-glass reads must be audited and
  visible; break-glass destroy must be too. Amendment: a `channel_deletions`
  table (id, name, type, visibility, created_by, deleted_by, deleted_at,
  message_count, member_count) written in the same transaction, plus one
  server log line; surfaced later wherever the house shows audit.
- **A3 — doc line (Gable, seq 1363, on the parent membership spec).** Owner-
  leave is revocable-at-will, not an exit: `created_by` is permanent, an owner
  who left keeps invite/kick and may re-invite themself. Recorded here so it
  is not re-discovered.

## Lane → Review owner (DETERMINISTIC)
- **Lane**: builder (server channel surface + client). Review owner: **Gable**,
  with plink's Tier 2 sign-off (destructive verb on the channel surface).

## Builder
- **Builder**: keyboard (plink's Claude Code seat, Opus build-hands) for what
  is on main. A1/A2 — plink's choice: resident build (Claudette, server lane)
  or keyboard under the keyboard-lane amendment once it is witnessed.

## Expected diff tier
Tier 2 (destructive verb, authorization surface).

## Token estimate
Spent: two Opus hands, one keyboard session. A1+A2: one small build slot.

## Confirm record
- **Confirmed by**: plink
- **#custodian seq**: <retro-confirm — plink posts one line "retro-confirm:
  delete-channel as built (e26f750, 54e490e); A1+A2 adopted" and the keyboard
  fills the seq here>
- **Confirmed at**: 2026-08-18
<!-- Retroactive: the build preceded the record. The seq witnesses that plink
stands behind what is on main and adopts A1/A2. -->

## Status
merged
<!-- keyboard-built; merged as e26f750 (server) + 54e490e (client) on
2026-08-17. Amendments A1/A2 are open work, tracked here until built. -->
