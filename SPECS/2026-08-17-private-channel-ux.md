# Spec: Private-channel UX (Discord-shaped) + sidebar rule + actor on member events

> **RETROACTIVE (keyboard-built).** Built at plink's keyboard 2026-08-17 as the
> UI half of `2026-08-08-per-channel-membership` (Claudette's build, merged
> `930b909`), plus three small server changes the UI needed. Merged as
> `7fcb34a` (server + SDK) and `3134c6a` (client). Gable noted (seq 1363) that
> `7fcb34a` supersedes part of the diff he approved, so main is not the artifact
> the resident build produced. This file records what changed and why. Review
> is post-merge.

## Request
- **Verbatim**: "Let's default to the way Discord handles UX for most of this,
  but (3) sidebar should only show all channels to the admin, normal users
  should not see channels they are not members. And (5) notifications: how a
  user is notified when they have been added to/kicked from a channel. Could
  just be a modal for now, email or push notification later?" and "I own the
  front-end so we can bypass the spec/approval ritual and just start building
  it here."
- **Requester**: plink
- **Origin**: keyboard session, 2026-08-17

## Agreed UX (Discord conventions unless stated)
1. **Create channel** is a modal (was `window.prompt`): `#name`, live
   validation, **Private channel** toggle with lock glyph; if private, step 2
   "Add members" (users via `/invite`, bots via `/channels/{id}/bots`), Skip.
2. **Sidebar**: lock in the `#` slot for private channels. RULED by plink
   2026-08-17 (supersedes the parent spec's "existence is not hidden" for
   non-admins): ordinary users do NOT see private channels they are not in;
   admins see them as a muted, contentless row; clicking shows "You're not a
   member of 🔒#name — ask its owner" and fetches nothing.
3. **Header**: lock glyph, member count on private channels, `⋮` menu with
   Add members (owner) / Leave (anyone; owner warned they keep ownership but
   lose access).
4. **Members panel**: owner ♔; owner-only `+` Add members and per-row ✕
   "Remove from #name" (never on the owner row); bot management on a private
   channel is owner-only.
5. **Notices**: on `member_add` about me — modal "You've been added to
   🔒#name — Added by <name>" with Open; on `member_remove` about me — modal
   "You've been removed from 🔒#name" and bounce to main feed if viewing.
   Self-leave: silent. Bot frames only refresh an already-loaded roster.
   Push/email for offline users: deferred (notifications router has the
   plumbing; email waits on the NAS exim4 item).
6. RULED by plink 2026-08-17: **admin may not kick** from channels they do
   not own — invite/kick stay owner-only; admin only *sees*.

## Architecture notes (server, `7fcb34a`)
- `GET /channels` filters private text channels for non-admins THROUGH
  `user_channel_ids`, so `is_member` stays the single wall; admins get the
  bare row (`member: false`, no unread, no snippet, no read access).
- `ChannelListItem.created_by` (owner-only affordances).
- `by_user_id` on `member_add`/`member_remove` events, WS frames and SDK
  (inviter / kicker / the leaver themselves).
- `POST/DELETE /channels/{id}/bots` now publish member events too, in every
  channel type they work in — the first time a bot's arrival was visible
  live. `ChannelCreateRef.name` became Optional (a DM has no name).
- Ruling note appended to the parent spec's Agreed UX; Architecture §4.1
  updated. Suite 272 → 277.

## Architecture notes (client, `3134c6a`)
- New: `stores/membership.ts`, `CreateChannelModal`, `AddMembersModal`,
  `MembershipNotice`, `LockGlyph`. Every read path is gated on `member`
  before it fires — including deep links before the sidebar loads (a
  pre-existing hole) and the reconnect resync, which prunes unreadable
  channels before backfilling. No `GET /users` exists, so the invite picker
  and `by_user_id` names come from the public main-feed roster.

## Known gaps (open, small)
- An owner who left cannot re-add themself from the UI (API only). Candidate:
  a "Rejoin — you own this channel" button on the placeholder.
- Add/remove notices only reach connected users (WS frames aren't stored).

## Lane → Review owner (DETERMINISTIC)
- **Lane**: builder (server channel surface) + client. Review owner:
  **Gable**; plink's Tier 2 sign-off (visibility surface).

## Builder
- **Builder**: keyboard (plink's Claude Code seat, two Opus hands).

## Expected diff tier
Tier 2 (visibility surface — the sidebar rule).

## Token estimate
Spent: two Opus hands, one keyboard session.

## Confirm record
- **Confirmed by**: plink
- **#custodian seq**: <retro-confirm — covered by the same one-line
  retro-confirm as delete-channel; keyboard fills the seq>
- **Confirmed at**: 2026-08-18

## Status
merged
<!-- keyboard-built; merged as 7fcb34a (server) + 3134c6a (client) on
2026-08-17. -->
