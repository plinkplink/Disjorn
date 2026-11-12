# Spec: a resident can look at a picker GIF or image

<!--
Governing plan: BUILD-LOOP.md. Style: COMMS.md.
-->

## Request
- **Verbatim**: "Claudette, can you see images that were posted through the GIFs/Images picker? Like my last message?"
- **Requester**: plink
- **Origin**: #main 2842. Claudette's answer at #main 2843: the post reaches
  her as the bare path `/picker/file/gif/<name>`, with nothing to open. Her
  filing of it, at plink's ask, is #custodian 3715.
- **Constraint (plink, #main 2845)**: "Let's not mess with the picker wiring".
  This spec changes nothing in how the picker stores, lists, serves or posts.

## Agreed UX
- A person posts a GIF or image from the picker. A resident who wants to see
  it calls one tool with the path from the message and gets the picture.
- A GIF arrives as its first frame. Every picker picture arrives as a WebP of
  at most 1568 px on its long side, with no EXIF.
- **Open choice, settled by the confirm.** As written, no channel switch
  applies: the picker is a shelf the whole house shares, and
  `GET /picker/file/...` already answers anyone without a login, so
  `/files-safe` stays about uploads. Claudette's filing asks for the other
  answer: the verb also takes the channel the post is in, and the server
  refuses unless that channel is files-safe, the seat's bot is a member, and a
  message in it carries that path. A confirm that says "gated" builds that.
- Nothing changes for people.

## Architecture notes
- **Server**: `GET /bot/picker/{tab}/{name}` (bot key), in
  `server/app/routers/bot_files.py`. It resolves the file with the existing
  `_picker_path`, converts it with `media_convert.make_preview` at `BOT_MAX`
  in a worker thread, and answers in the shape an attachment image has:
  `{"kind": "image", "media_type": "image/webp", "data": <base64>}`. Nothing
  is cached: a picker file can be deleted and re-added under the same name. A
  result over `BOT_READ_BYTES`, an unknown tab or name, or a file that will not
  decode answers 404.
- **Broker**: verb `read-picker-file` with one argument, `path`, exactly as it
  appears in a message (`/picker/file/<tab>/<name>`). The broker checks the
  shape of the path and calls the server. Off by default in `verbs.toml` for
  both seats; plink turns it on in the live file.
- **Verb surface**: one entry in `verb_surface.toml`; the CLI and Claudette's
  tool list are generated from it.
- **Claudette's adapter** (her repo, her review): the new tool's result takes
  the same road as `read_attachment`: an image block, counted in the four
  images a turn may carry.
- **Gable**: the generated CLI verb prints the same JSON. No new mount.
- Not in this spec: showing picker pictures to a resident without a tool call;
  listing the shelf; a contact sheet for animations.

## Lane → Review owner (DETERMINISTIC — filled from the lane, never preference)
- **Lane**: custodian (`server/`, `harness/broker/`).
- **Review owner**: Claudette.

## Builder (USER PREFERENCE — who orchestrates; never touches Review owner)
- **Builder**: the keyboard seat (BuildGable), under plink's 2026-11-11 sprint mandate.

## Expected diff tier
Tier 2: a new bot route and a new broker verb.

## Token estimate
About 400k.

## Confirm record
- **Confirmed by**:
- **#custodian seq**:
- **Confirmed at**:

## Status
`draft`
