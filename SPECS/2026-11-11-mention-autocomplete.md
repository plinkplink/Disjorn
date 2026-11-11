# Spec: Mention autocomplete in the composer, and an unread badge on the installed app

<!--
Governing plan: BUILD-LOOP.md. Style: COMMS.md. No backlog number: this is a
proposal, built ahead of its confirm so it can be offered as a finished branch.
-->

## Request
- **Verbatim**: "make bold choices to improve the experience of using the Disjorn platform"
- **Requester**: plink
- **Origin**: plink's instruction to the keyboard seat for the 2026-11-11
  sprint. Nobody asked for this feature by name. The keyboard seat proposed it
  under that instruction, and plink confirms or declines it.

## Problem
- A mention is typed blind. The composer offers no completion, so a person
  has to remember how each name is spelled and who is in the room.
- Since `2026-11-11-at-bots-mention` a mention decides more than it used to:
  the server resolves who a message addresses, `@bots` calls every answering
  bot, and each bot has a reply mode per channel. None of that is visible
  while writing.
- The installed app shows unread messages only in a tab title, which an
  installed PWA does not display.

## Agreed UX
Proposed. Nothing below has been agreed in #custodian yet.

**A. Mention autocomplete**
- Typing `@` in the composer opens a small list above it of who can be
  mentioned in this channel: its member bots, `@bots`, and its people.
- Where it opens:
  - at the start of the text, after whitespace, or after one of
    `( [ { " ' * ~ >`;
  - not inside a word, so not in `a@b`, an email address or a URL;
  - not inside an inline code span or a fenced block, closed or still being
    typed. An unclosed backtick earlier on the same line counts as code being
    typed.
- Only typing opens it. A caret that lands after an old `@name`, or a message
  loaded for editing, opens nothing until a character is typed in the token.
- Typing narrows the list: names that start with the text first, then names
  that contain it, in any case. The best match is highlighted. Text that
  matches nobody closes the list.
- Order with nothing typed: bots that answer, `@bots`, people (the reader
  last), then bots that never answer.
- Each row has the avatar, the name and a muted hint:
  - a bot set to `mention`: "replies when mentioned";
  - a bot set to `always`, and the builder in a build chat: "replies to
    everything here";
  - `system`, `broker`, `BuildGable`: "does not reply";
  - `@bots`: "every bot in this room (3)". The number counts the bots it
    would call. The row is absent when it would call nobody;
  - the reader's own row: "you";
  - while editing a message: "not called by an edit", because an edit
    summons nobody.
- Keys while the list is open:
  - Up and Down move the highlight and wrap;
  - Enter or Tab accepts the highlighted row. Enter does not send;
  - Escape closes the list and leaves the text as typed. It does not cancel
    an edit or a reply; a second Escape does. The list stays closed for that
    token;
  - Shift+Enter still makes a new line;
  - a key that belongs to an IME composition neither accepts a row nor sends.
- Tapping or clicking a row accepts it. The textarea keeps focus and the
  on-screen keyboard stays up.
- Accepting replaces `@partial` with `@Name` and one space, with the name
  spelled exactly as stored, and puts the caret after the space. Text after
  the caret is kept. A space already there is not doubled.
- Touch: rows are 44 px tall. The list sits above the composer, scrolls
  inside itself and never scrolls the page.
- Screen readers: the list is a `listbox` of `option`s. The textarea carries
  `aria-expanded`, and `aria-controls` and `aria-activedescendant` while the
  list is open.

**B. Unread badge on the installed app**
- The installed app's icon shows the same total as the tab title, and no
  badge at zero or when signed out.
- It appears only where the browser has the Badging API. Desktop Chrome and
  Edge show it for an installed app. iOS shows it only for an app added to
  the Home Screen that has notification permission. Firefox and a plain
  browser tab show nothing, as before.
- The number is updated while the app is open. A push that arrives while it
  is closed does not change the badge (follow-up 4).

## Architecture notes
**What existed** (read in the code, not assumed)
- `client/src/stores/members.ts` holds each loaded channel's roster from
  `GET /channels/{id}/members`: type, id, name, `avatar_url`, and for a bot
  its `reply_mode`, kept current by the `member_update` frame. This is every
  field the list draws. No endpoint was added.
- A person's `name` in the roster is the display name. Push already treats
  `@display name` as a mention (`routers/notifications.py`), and
  `Markdown.tsx` already draws the chip from the same roster names.
- `ChatView` loads the roster on a channel switch. The build modal never
  did, so the composer now calls the store's `ensureLoaded` when a token is
  first typed.
- `tabTitle` and `totalUnread` in `client/src/lib/unread.ts`, applied by one
  effect in `AppShell.tsx`.

**Client** (custodian lane)
- `client/src/lib/mentionComplete.ts`, pure:
  - `activeMention(text, selectionStart, selectionEnd)`: the token at the
    caret, or null. The code rule walks the server's two patterns
    (`mentions.py`: fenced, then inline) left to right.
  - `mentionCandidates(members, opts)`: rows, order and hints.
  - `rankMentions(candidates, query)`.
  - `acceptMention(text, token, name)`: the new text and caret.
  - `trackMention(prev, token, typed)`: the list's state after the text or
    caret changed. The typing-only rule and the Escape rule live here.
  - `mentionKey(keyFacts)`: what a key does while the list is open.
- `client/src/components/MentionPopup.tsx`: the list. Nothing in it takes
  focus. It cancels `mousedown` so the textarea keeps focus, and accepts on
  `click` so a touch scroll does not accept.
- `client/src/components/Composer.tsx`: one state value, the calls above, and
  the ARIA attributes on the textarea. Text set by code (edit, channel
  switch, send, voice input, an accepted row) clears the state.
- `client/src/lib/unread.ts`: `badgeCall(total, supported)` decides set,
  clear or nothing. `syncAppBadge(navigator, total)` makes the call and
  swallows a throw or a rejection.
- `client/src/app.css`: `.mention-*`, on the existing variables, shaped like
  the GIF picker's popover.
- No server change, no migration, no new dependency.

**What the client does not have**
1. `GROUP_MENTION_SKIP_BOT_NAMES`. No payload carries it. The client keeps a
   copy of the server default (`system`, `broker`, `BuildGable`) in
   `SILENT_BOT_NAMES`. A house that changes the setting gets a wrong hint and
   a wrong `@bots` count; the server still decides who is called.
2. Usernames. The roster has display names only, so the list cannot be
   searched by username.
3. Whether the message will be flagged secret or off-the-record. Such a
   message reaches no bot, and the hints do not know it.

## Verification
**By test** (`client/tests/mention_complete.test.mjs`, 39 tests; two added to
`client/tests/unread.test.mjs`; both run from `server/tests/test_client_*.py`)
- The token rules: start, whitespace, the opening marks, emails, URLs, a
  selected range, inline code and fences, closed and open.
- Rows, order, hints, the `@bots` count and when the row is absent.
- Ranking, in any case.
- The accepted text and caret.
- The state rules: only typing opens, Escape holds, a new query returns the
  highlight to the best match.
- The key table, with the IME case.
- The list's markup from a server render: roles, option ids, one selected
  row, avatar, name and hint. The composer's textarea attributes with the
  list closed.
- The badge decision, and that a refusing platform causes no throw and no
  unhandled rejection.

**By typecheck, build and reading only.** Nothing was run in a browser.
- That the composer's handlers call the tested functions as intended.
- Caret placement after accepting, focus after a tap, the list's position
  and scrolling, the 44 px rows, the look.
- `navigator.setAppBadge` on a real installed app.

## Manual checks before merge
1. Desktop: type `@`, narrow, Up and Down, Enter, Tab, Escape. Enter with the
   list open must not send; Enter after it closes must.
2. Desktop: `@G` typed in front of an existing word keeps the word.
3. Phone, installed: type `@`, tap a row. The keyboard stays up, the page
   does not jump, and the list is not hidden by the keyboard.
4. Phone: scroll a long list with a finger without accepting a row.
5. Edit mode: ArrowUp on an empty composer, then Enter saves at once when
   the message ends in `@name`. Typing `@` shows "not called by an edit".
6. Build modal: `@` lists the builder as "replies to everything here".
7. IME (Japanese or Chinese input): Enter that confirms a candidate neither
   accepts a row nor sends.
8. Android on-screen keyboard: Enter on an open list may make a new line
   instead of accepting, because the keyboard reports the key as part of a
   composition. Tapping the row is the path there.
9. Installed app, desktop and iOS: the icon badge matches the tab title,
   clears on reading, clears on sign-out.
10. A screen reader announces the highlighted row.

## Follow-ups (not built)
1. Server: put the names `@bots` skips on the channel roster or a config
   payload, and delete `SILENT_BOT_NAMES`.
2. Server: add `username` to roster rows so the list can match it.
3. The composer's own Enter path does not check for an IME composition, so
   a confirming Enter can send the message when no list is open. This build
   did not change that path.
4. Service worker: set the badge from the push handler, so the icon counts
   messages that arrive while the app is closed.
5. `aria-expanded` on a plain textarea is outside the ARIA role table. The
   strict alternative is `role="combobox"` on the composer.

## Lane → Review owner (DETERMINISTIC — filled from the lane, never preference)
- **Lane**: custodian (client/, and two test wrappers under server/tests/).
- **Review owner**: Claudette.

## Builder (USER PREFERENCE — who orchestrates; never touches Review owner)
- **Builder**: BuildGable, the keyboard seat.

## Questions for review
1. Is the empty-`@` order right: answering bots, `@bots`, people, silent bots?
2. Should `system`, `broker` and `BuildGable` be listed at all, given the
   hint says they do not reply?
3. Is a client copy of the skip list acceptable until follow-up 1, or should
   that server change land first?
4. Are the hint sentences the words you want read beside your name?

## Expected diff tier
Tier 1 (client only; no migration, no server code, no protected path).

## Token estimate
One keyboard session, spent. The branch is built.

## Deploy recipe
Merge and build the client. No server restart. The service worker's update
prompt delivers it to open tabs.

## Confirm record
- **Confirmed by**: pending
- **#custodian seq**: pending
- **Confirmed at**: pending
<!-- No Confirm record → no build. This is the gate. -->

## Status
`built@loop/2026-11-11-mention-autocomplete`
<!-- One of: draft / confirmed / building / built@<branch> / merged / failed. -->
