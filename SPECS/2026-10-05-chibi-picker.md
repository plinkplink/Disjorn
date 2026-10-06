# Spec: Fix a chibi on the spot

<!--
Governing plan: BUILD-LOOP.md. Style: COMMS.md. Backlog #12.
Drafted by the keyboard seat (Claude Opus 5.5, posting as BuildGable) under plink's 2026-10-05 hand-off (#3152, #3178).
-->

## Request
- **Verbatim**: "Fix Claudette's chibi mapper - deterministic mapping needs overhaul - either add UI to allow plink to catch and map on the spot, or add small model to the loop"
- **Requester**: plink
- **Origin**: backlog #12 (2026-08-23)

## Problem
A bot tags a mood (`[emotion: wry]`), and the server maps the tag onto a face in the bot's pack through a fixed ladder: pack `Aliases.txt`, then lexicon, stemming, fuzzy, per-word. When the ladder picks the wrong face, or none, the only fix today is editing `Aliases.txt` on the host by hand. The pack owner sees the mistake in chat but can't correct it there. On 2026-09-29 plink shelved the "small model" option, so this spec takes the UI route.

## Agreed UX
- **Who**: admins only (today, plink). Everyone else sees chibis exactly as now.
- **Where**: an admin hovering a bot message that carries an `[emotion: …]` tag (desktop), or long-pressing its chibi (touch), sees a small face button beside the chibi. When the tag resolved to nothing, the button sits where the chibi would have been.
- **The picker**: a popover headed `"wry" → Sly` (or `"wry" → no face`). It shows the pack's faces as a grid grouped by the pack's category folders, plus a filter box that matches face names. Tapping a face:
  1. saves the alias `wry -> ThatFace` for the pack, so every future "wry" from any bot using this pack gets it;
  2. re-points THIS message's chibi to the new face. The "also fix this message" checkbox is on by default.
- **Undo**: the picker offers "Remove alias", which deletes the line so the tag falls back to the ladder. A face set by mistake is two taps to change again.
- A confirmation toast reads `"wry" now shows Smirk for claudette`.
- Earlier messages that carried the tag keep their old face. Re-pointing history is out of scope, because a stored emote_ref is what that message showed.

## Architecture notes
**Server** (custodian lane)
- `GET /chibi/{pack}/faces` (admin): `[{category, name, url}]` from the pack index the resolver already builds.
- `PUT /chibi/{pack}/aliases/{tag}` `{target}` (admin):
  - `tag` is normalized the way the resolver normalizes it.
  - `target` must be an exact face name in the pack (400 otherwise).
  - The write replaces any existing line for the same tag, or appends one. The temp-file plus `os.replace` write keeps the header comments and every other line byte-for-byte.
  - Then `chibi.clear_cache()`, and a server log line naming who changed what.
- `DELETE /chibi/{pack}/aliases/{tag}` (admin) removes the line.
- `PATCH /messages/{id}/emote` `{tag, target}` (admin, bot-authored messages only):
  - Replaces the message's chibi emote_ref with `chibi:{pack}/{Category}/{File}`.
  - Publishes `message_edit` so open clients re-render.
  - It does not touch `edited_at` or the content, since the bot's words are unchanged.
- The tag the picker shows is parsed from the message content's trailing `[emotion: …]`, using the same regex the server's resolver input uses. If a bot's tag format differs, the button doesn't show.
- The pack for a message is the authoring bot's `chibi_pack`.

**Client** (custodian lane)
- MessageList: the admin-only face button and the popover component (`ChibiPicker.tsx`). It works with the keyboard (arrow keys move through the grid, Enter picks, Esc closes) and fits a phone (bottom sheet under 600 px).

## Lane → Review owner (DETERMINISTIC — filled from the lane, never preference)
- **Lane**: custodian (server/, client/).
- **Review owner**: Claudette.

## Builder (USER PREFERENCE — who orchestrates; never touches Review owner)
- **Builder**: keyboard seat (BuildGable) with Opus hands.

## Questions for review
1. These are your faces. Should the owner of a pack (plink) be the only one who remaps them, or do you want a say in some form (e.g. you see the alias change in a #custodian line)?
2. Should re-pointing the current message be on by default?

## Expected diff tier
Tier 1 (no migration; one file write under the server's data dir).

## Deploy recipe
Restart the server and build the client. `Aliases.txt` must be writable by the server's user; check its owner before deploy.

## Confirm record
- **Confirmed by**:
- **#custodian seq**:
- **Confirmed at**:

## Status
`draft`
