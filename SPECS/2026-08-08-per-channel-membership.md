# Spec: Per-channel membership as an enforced wall

## Request
- Verbatim: "Channels are all visible and open to all users. Need to get these at least these in place before I invite anyone else to the party." (plink, #custodian, 2026-08-08)
- Requester: plink
- Origin: #custodian (channel 4), 08-08 regroup; design converged 2026-07-22 with plink's rulings recorded then.

## Agreed UX
Channels gain a visibility mode. A private channel's content is unreadable to non-members — its history does not load and its messages never appear in search results for non-members. Its existence is not hidden: the channel list is honest server-side, and any cosmetic filtering of not-my-channels in the client is display polish, not a wall. Error shapes need not be leak-proofed; 403 is a fine and truthful answer. Members are added by invite; anyone can leave; kick exists for the channel's owner. Existing channels are grandfathered public, so nothing changes for anyone on the day it lands.

**RULED** by plink, 2026-08-17: the sidebar lists a private channel to non-members only if they are admin — `GET /channels` omits private channels an ordinary user is not a member of, superseding "its existence is not hidden" above. An admin still gets the row in the shape this spec describes (`member: false`, no unread, no snippet) and gains no read access whatsoever, so architecture note 5 (no silent god-view) stands unchanged. Also additive on the same date, for the client's owner-only affordances: `GET /channels` rows carry `created_by`, and `member_add`/`member_remove` carry `by_user_id` (the inviter, the kicker, or — on leave — the leaving member themselves). `POST`/`DELETE /channels/{id}/bots` now publish those same events with `member_type: "bot"` too, in every channel type they work in (architecture note 4, same wall for bots, applied to the fanout as well as to the reads) — previously a bot's arrival was the one membership change no client could see happen.

## Architecture notes
1. EXTEND, DON'T INVENT: the ChannelMember table (channel_id, member_type, member_id, last_read_seq) already exists and is today merely informational. This spec turns it into the enforced wall. No second mechanism.
2. VERBS as WS events: invite / join / leave / kick, plus member_add / member_remove fanout. channel_create for a private channel fans out only to its members.
3. ORTHOGONALITY: channel-level ACL stays independent of message-level flags (secret / off_the_record). Two walls that compose; neither is implemented in terms of the other.
4. SAME WALL, NO CARVE, FOR BOTS: a resident that isn't a member sees no content, exactly as a human non-member doesn't. No bot-shaped exception, in either direction.
5. NO SILENT GOD-VIEW: plink owns the box, the DB and the logs, so he can always look — but the app must not ship a quiet in-product read button for admins, or "private" is a lie to everyone else. If break-glass in-app reads ever exist they are audited and visible, never silent.
6. ENFORCE AT THE READ PATH, NOT ONLY AT FANOUT: history fetch, seq fetch, and platform search must each filter by membership. Search is the likeliest leak and gets its own test.

**RULED** by plink, 8/12/2026: only the channel owner (creator) may invite.

Implementation consequence, not in the original draft: `channels` has no owner
column (`migrations/005_text_channels.sql`: id, type, name, created_at). This
spec therefore adds `channels.created_by INTEGER REFERENCES users(id)`, with a
backfill setting existing text channels to the first admin. Owner-only invite
applies to `type='text'` only; `main_feed` and DM channels have no creator and
are unaffected.

## Lane -> Review owner (DETERMINISTIC)
- Lane: server tree (accounts/channels). Not Gable's host-build surface, no overlap with the build-lane spec. Review owner: Gable, with plink's sign-off required as the Tier 2 human gate.

## Builder
- Resident-built (Claudette), one build slot.

## Expected diff tier
Tier 2 (visibility/authorization surface).

## Token estimate
One build slot; test-heavy, implementation is small.

## Confirm record
- **Confirmed by**: plink
- **#custodian seq**: 1007
- **Confirmed at**: 2026-08-12

## Status
merged
<!-- advanced from `built@loop/2026-08-08-per-channel-membership` by `board --mark-merged` on 2026-08-17: build merged as 930b909. The word `built@loop/2026-08-08-per-channel-membership` on a merged spec made it indistinguishable from a buildable one. -->
<!-- set by the broker on 2026-08-17 21:14Z (start-build, 2026-08-08-per-channel-membership): build published: disjorn.git f95a65989b592d7937f9d235d553e9159078a637 — on the branch for review, nothing merged. `board --mark-merged` advances this to `merged` once the merge lands. -->
<!-- set by the broker on 2026-08-17 20:55Z (start-build, 2026-08-08-per-channel-membership): build running as disjorn-build-2026-08-08-per-channel-membership.service -> loop/2026-08-08-per-channel-membership, launched by claudette (confirmed by plink, #custodian seq 1007). Not buildable again until this line moves. -->
<!-- set by the broker on 2026-08-17 18:20Z (start-build, 2026-08-08-per-channel-membership): build failed: exit 1: Running as unit: disjorn-build-2026-08-08-per-channel-membership.service; invocation ID: 045166f665dc4ba18052d2fb17803ba9 run-build: auth: CLAUDE_CODE_OAUTH_TOKEN from /srv/disjorn-build-config/claudette/env run-build: container exited 1 — not publishing. The workspace clones under /home/res. To allow another build, set this back to `confirmed` (the confirm record above still stands). -->
<!-- set by the broker on 2026-08-17 17:45Z (start-build, 2026-08-08-per-channel-membership): build running as disjorn-build-2026-08-08-per-channel-membership.service -> loop/2026-08-08-per-channel-membership, launched by claudette (confirmed by plink, #custodian seq 1007). Not buildable again until this line moves. -->
