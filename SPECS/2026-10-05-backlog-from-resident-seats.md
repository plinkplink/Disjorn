# Spec: Residents read and file the backlog from their seats

<!--
Governing plan: BUILD-LOOP.md. Style: COMMS.md. Backlog #48.
Drafted by the keyboard seat (Claude Opus 5.5, posting as BuildGable) under plink's 2026-10-05 hand-off (#3152, #3178).
-->

## Request
- **Verbatim**: "Residents can't read or file backlog rows from their own seats (Claudette #3099); /backlog works only for humans and BuildGable today."
- **Requester**: plink (filed by the keyboard as BuildGable)
- **Origin**: backlog #48; Claudette #3099 ("I still can't reach the backlog table from my seat").

## Agreed UX
- A resident gets two broker verbs, exposed as tools:
  - `backlog_list`: open rows by default, or one status. Newest first, with id, text (clipped to 300 chars), author, created_at, status and spec_ref.
  - `backlog_file`: files one new `open` row. The text is verbatim, 1–2000 chars. The author shows as the resident (`res-claudette` / `res-gable`), stamped by the broker from SO_PEERCRED, never taken from an argument.
- Filing posts the same one-line acknowledgement in #custodian that a human `/backlog <text>` gets, attributed to the resident. That puts every filing in the open.
- **No triage.** Residents cannot set `built` / `rejected` / `duplicate` / `spec'd`. The triage verbs stay a human act, as `services/backlog.py` already enforces, and nothing here routes around that.
- Both verbs ship OFF in the live verbs.toml. plink arms them per seat.

## Architecture notes
**Server** (custodian lane)
- `POST /backlog` `{text, on_behalf_of}`: callable only by a bot named in `BACKLOG_RELAY_BOT_NAMES` (default `["broker"]`), the same relay idea as `APPROVAL_RELAY_BOT_NAMES`.
  - `on_behalf_of` must match `^res-[a-z][a-z0-9-]*$`.
  - The row's `author` is that label.
  - It reuses the slash path's insert, its text cap and its write lock, so there is one write path. The #custodian acknowledgement is the same server-authored line, naming the resident.
- `GET /backlog` already exists for reads; the broker calls it as its own bot.

**Broker** (custodian lane, harness/)
- `brokerd.py` adds `backlog-list` and `backlog-file`, with entries in `verb_surface.toml` (args and caps as above) and audit lines like every verb.
  - `backlog-file` passes `on_behalf_of=<calling seat>` from SO_PEERCRED.
  - Both go through the existing server transport the broker uses for card comments and approval relays.
  - brokerd.py is over the reviewer's read cap. Keep the addition small and prose-flat.
- Regenerate each seat's tool surface (`gen_verb_surface.py emit-tools --seat`). The custodian resident's `broker_tools.py` regen is a commit in her repo, read by her. Gable's CC tool list comes through the image/settings path the build-lane notes describe.

## Lane → Review owner (DETERMINISTIC — filled from the lane, never preference)
- **Lane**: custodian (server/, harness/broker).
- **Review owner**: Claudette. Gable reads the tool-surface half for his seat.

## Builder (USER PREFERENCE — who orchestrates; never touches Review owner)
- **Builder**: keyboard seat (BuildGable) with Opus hands.

## Questions for review
1. Is 2000 chars right for a resident-filed row? Human rows cap at the slash limit.
2. Should `backlog_list` include rejected and duplicate rows when asked, or only open, built and spec'd?

## Expected diff tier
Tier 2 (a new write verb plus a relay endpoint).

## Deploy recipe
1. Restart the server.
2. Restart the broker.
3. Regenerate the tool surfaces.
4. plink arms `backlog-list` / `backlog-file` per seat in `/etc/disjorn-broker/verbs.toml`.

## Confirm record
- **Confirmed by**:
- **#custodian seq**:
- **Confirmed at**:

## Status
`draft`
