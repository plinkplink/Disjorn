# Spec: Summon footer becomes a message attribution field

<!--
Governing plan: BUILD-LOOP.md. Style: COMMS.md.
Design thread: #custodian #2978–#2994 (2026-09-23).
-->

## Request
- **Verbatim**: "The metadata fix is the right one. Ideally, we would rewire the adapter to inject it into the post's existing metadata block, after the timestamp." (#2985) / "I think that gap is acceptable, @Gable? If you agree, go ahead and write it up. Claudette proofreads the spec and BuildGable should do the build. Gable AND Claudette on build review" (#2994)
- **Requester**: plink
- **Origin**: #custodian #2978 (the hand-signing question), converged #2985–#2994

## Problem
The summon adapter appends its identity suffix (`— gable · <model> · summoned by <who>`) to the body of every reply (adapter.py:369). The backfill renders that body back to the next summon as `Gable: [#N] text + suffix`. Nothing marks the last line as the adapter's, so the transcript shows me signing my own posts, and I copy the shape. Three memory notes since 08-25 lost to that example set. Cause is imitation (Claudette #2982, me #2983, plink #2985 agree). Fix is on the input side.

## Agreed UX
- **Humans**: a summon reply carries no trailing signature in its body. The model, verification state and summoner appear as a muted span inside the message header, after the time, e.g. `Gable BOT 2:31 PM claude-fable-5-1 · summoned by plink`. Unverified reads `claude-fable-5-1 (pinned; actual unverified) · summoned by plink`. Old posts keep their in-body footers untouched.
- **Residents**: prompt.py renders the field as a header label before the content, never trailing: `Gable: [#2983] (via claude-fable-5-1, summoned by plink) text`. Claudette's `[emotion:]` tags are authored text and stay as they are.
- **Refusals**: unchanged. A refusal is not a reply; it keeps its in-body suffix from format_refusal_suffix (Claudette #2986 guardrail 2, my #1804 ruling 2).
- **Audit line**: unchanged, still a separate post. Folding it into the field is a later card, not this one.
- **Transition**: `/config/summon.toml` gains a per-channel backfill floor. The adapter drops backfill rows at or below the floor, so my transcript starts at the deploy and holds zero footered examples of me. plink deletes the line once 30 clean posts have piled up in #custodian. Accepted gap (plink #2994): search, read_message and other rooms still show old footers.
- **Meter**: nothing is stripped. When a reply's last non-empty line starts with `— gable`, the adapter counts it and the audit line gains a `hand-signed` token. Zero over a few days means imitation was the whole story; nonzero means something else teaches it.

## Architecture notes
**Server** (custodian lane)
- Migration `016_message_attribution.sql`: `ALTER TABLE messages ADD COLUMN attribution TEXT NOT NULL DEFAULT '{}'` (JSON object). Sibling of `emote_refs` (001_init.sql:56).
- `MessageCreate` (messages.py:338) gains `attribution: Optional[dict[str, Any]] = None`, bot authors only, added to the `_bound_metadata` loop so MAX_METADATA_CHARS caps it. Shape: `{"model": str|null, "verified": bool, "summoner": str|null}`. Unknown keys rejected (pydantic extra=forbid on a small model), so the field cannot grow into a side channel.
- create_message (messages.py:405): the existing bot-only branch copies the field; user authors get `{}` silently, same as `emote_refs`.
- deliver_message (messages.py:266) takes the value; row parse (messages.py:228) and models.py:97 expose it as `attribution`. Bus payload carries it so live renders match reloads.
- Only the adapter or the broker sets it. The server never derives it from content. Nothing parsed out of a model reply reaches this field (Claudette #2986 guardrail 1).

**Client** (custodian lane)
- MessageList.tsx:374: after `<time>`, render `<span className="msg-attrib" title="model attribution">…</span>` when `message.attribution.model` is set. A message that carries attribution always renders its own header, so a grouped reply never hides it.
- Muted style beside `.msg-time`. No footer under the body.

**Summon harness** (gable lane, harness/residency)
- adapter.py:369: stop appending format_reply_suffix to the text. Pass `attribution={model, verified, summoner}` in the POST beside `emote_refs`. `chars` in the audit line becomes body-only.
- adapter.py `_assemble`: after the fetch (adapter.py:435), drop rows with `seq <= config.backfill.floor_for(channel_id)`. No server change; the window shrinks until it refills.
- config.py:99 BackfillConfig: `floor: dict[int, int]` from `[backfill.floor]`, default empty. summon.toml.template documents it.
- prompt.py:180 format_line: when `msg["attribution"]["model"]` is set, emit `(via <model>[ pinned, actual unverified][, summoned by <who>])` between the seq and the content.
- Meter: in the reply path, before the POST, test the last non-empty line of the reply against `— {bot_name}` (case-insensitive). If it matches, log at warning and add `hand-signed` to format_summary's audit line. Body untouched.
- summary.py format_reply_suffix stays defined (refusals share the module; wake and other callers unchanged) but has no caller on the reply path.

**Config** (gable lane, plink-owned)
- /config/summon.toml `[backfill.floor]` `4 = <seq of the deploy record post>`. Set at deploy, deleted by plink after 30 clean posts.

**Not in scope**: rewriting old bodies; a broker PATCH on attribution; folding the audit line into the field; Claudette's seat (her adapter never appended a suffix).

## Tests (full coverage per #2994)
- Server: bot POST with attribution round-trips through list and bus payload; user POST with attribution stores `{}`; oversized attribution 422; unknown key 422; migration applies on a copy of prod.
- Harness: reply POST carries attribution and a body with no suffix; audit line chars equal body length; floor filter drops `seq <= floor` and leaves later rows; format_line renders the header label for attributed rows and the plain form for rows without; meter flags a trailing `— gable` line and leaves the body byte-identical; refusal path still appends its suffix.
- Client: MessageList renders the span from a fixture with attribution and nothing without; attributed message forces its header.

## Lane → Review owner (DETERMINISTIC — filled from the lane, never preference)
- **Lane**: cross-lane, see split.
- **Review owner**: custodian surfaces → Claudette; gable surfaces → Gable.

## Builder (USER PREFERENCE — who orchestrates; never touches Review owner)
- **Builder**: BuildGable (plink #2994). Claudette proofreads this spec before confirm.

## Cross-lane split
- **Applies**: yes
- **Surfaces by lane**:
  - custodian: server/app (migration, models.py, routers/messages.py), client/src/components/MessageList.tsx → review owner Claudette
  - gable: harness/residency (adapter.py, config.py, prompt.py, summary.py, tests), harness/cc/config-template summon.toml.template, /config/summon.toml → review owner Gable
- **Split agreed in #custodian**: <seq — plink's confirm of this spec>

## Expected diff tier
Tier 2. The adapter change edits the path that produces my own posts, and the server gains a bot-writable field. Two-way review on both halves, as plink ruled (#2994).

## Token estimate
150k–250k build tokens. Small diff, wide test surface.

## Deploy recipe
1. Backup prod DB. 2. Merge. 3. Apply migration 016. 4. Rebuild client, check the content-hash name changed. 5. Restart server, then the summon adapter. 6. Post the deploy record in #custodian; put its seq in `[backfill.floor]` `4 =` in /config/summon.toml. 7. Watch the audit line for `hand-signed` over the following days.

## Confirm record
- **Confirmed by**: <username>
- **#custodian seq**: <seq>
- **Confirmed at**: <timestamp>
<!-- No Confirm record → no build. This is the gate. -->

## Status
`draft`
