# Spec: Summon footer becomes a message attribution field

<!--
Governing plan: BUILD-LOOP.md. Style: COMMS.md.
Design thread: #custodian #2978–#2994 (2026-09-23). Proofread: Claudette #2999, folds ruled #3000.
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
- **Refusals**: unchanged. A refusal is not a reply; it keeps its in-body suffix from format_refusal_suffix (Claudette #2986 guardrail 2, my #1804 ruling 2). A refusal is still a footered example in my backfill (Claudette #2999 item 1); plink ruled it a non-issue (#3000) because self-@ refusals go away under backlog #37 and other refusals are rare. If the meter stays nonzero after #37 lands, refusals are the next suspect.
- **Audit line**: unchanged, still a separate post. Folding it into the field is a later card, not this one.
- **Transition**: `/config/summon.toml` gains a per-channel backfill floor. The adapter drops **my own** backfill rows at or below the floor and keeps everyone else's (Claudette #2999 item 3), so my transcript holds zero footered examples of me and the thread stays readable. My own rows = author_type `bot` and author name equal to `summon.bot_name`, case-insensitive. plink deletes the line once 30 clean posts of mine have piled up in #custodian. Accepted gap (plink #2994): search, read_message and other rooms still show old footers. The adapter reads config once at startup (run_summon.py:43, no reload), so the floor is set before the restart, see the deploy recipe.
- **Meter**: nothing is stripped. When a reply's last non-empty line matches `^\s*[-—–]+\s*gable\b` (case-insensitive: any dash run, then the bot name), the adapter counts it and the audit line gains a `hand-signed` token. Wide on purpose (Claudette #2999 item 4): a false positive costs nothing, a miss blinds the experiment. Zero over a few days means imitation was the whole story; nonzero means something else teaches it.

## Architecture notes
**Server** (custodian lane)
- Migration `017_message_attribution.sql` (016 is the approval object's): `ALTER TABLE messages ADD COLUMN attribution TEXT NOT NULL DEFAULT '{}'` (JSON object). Sibling of `emote_refs` (001_init.sql:56).
- `MessageCreate` (messages.py:338) gains `attribution: Optional[dict[str, Any]] = None`, bot authors only, added to the `_bound_metadata` loop so MAX_METADATA_CHARS caps it. Shape: `{"model": str|null, "verified": bool, "summoner": str|null}`. Unknown keys rejected (pydantic extra=forbid on a small model), so the field cannot grow into a side channel.
- create_message (messages.py:405): the existing bot-only branch copies the field; user authors get `{}` silently, same as `emote_refs`.
- deliver_message (messages.py:266) takes the value; row parse (messages.py:228) and models.py:97 expose it as `attribution`. Bus payload carries it so live renders match reloads.
- Only the adapter or the broker sets it. The server never derives it from content. Nothing parsed out of a model reply reaches this field (Claudette #2986 guardrail 1). Stated plainly: the server enforces only "bot author"; any bot token can set attribution on its own posts. The guardrail lives in the adapter code, which builds the field from `container.model` and the session's reported id, never from reply text. Accepted (Claudette #2999 item 6).

**Client** (custodian lane)
- MessageList.tsx:374: after `<time>`, render `<span className="msg-attrib" title="model attribution">…</span>` when `message.attribution.model` is set. A message that carries attribution always renders its own header, so a grouped reply never hides it.
- Muted style beside `.msg-time`. No footer under the body.

**Summon harness** (gable lane, harness/residency)
- adapter.py:369: stop appending format_reply_suffix to the text. Pass `attribution={model, verified, summoner}` in the POST beside `emote_refs`. `chars` in the audit line becomes body-only.
- adapter.py `_assemble`: after the fetch (adapter.py:435), drop rows where `seq <= config.backfill.floor_for(channel_id)` AND the row is my own post (author_type `bot`, author name == `summon.bot_name` case-insensitive). Other authors' rows pass through. No server change; my share of the window refills as I post.
- config.py:99 BackfillConfig: `floor: dict[int, int]` from `[backfill.floor]`, default empty, `floor_for(channel_id)` returns 0 when unset. TOML table keys are strings, so the parser converts with `int(k): int(v)`, the same idiom `per_channel` already uses at config.py:383 (Claudette #2999 item 5). summon.toml.template documents it.
- prompt.py:180 format_line: when `msg["attribution"]["model"]` is set, emit `(via <model>[ pinned, actual unverified][, summoned by <who>])` between the seq and the content.
- Meter: in the reply path, before the POST, test the last non-empty line of the reply against `^\s*[-—–]+\s*{bot_name}\b` (case-insensitive). If it matches, log at warning and add `hand-signed` to format_summary's audit line. Body untouched.
- summary.py format_reply_suffix stays defined (refusals share the module; wake and other callers unchanged) but has no caller on the reply path.

**Config** (gable lane, plink-owned)
- /config/summon.toml `[backfill.floor]` `4 = <current max seq in #custodian at deploy time>`. Any seq at or after my last footered post works. Set before the adapter restart, deleted by plink after 30 clean posts.

**Not in scope**: rewriting old bodies; a broker PATCH on attribution; folding the audit line into the field; Claudette's seat. Her adapter never appended a suffix and this spec does not teach it the new field, so after deploy her transcript shows my posts without a model line. She accepted that (#2999 item 6); wiring her prompt renderer is a later card.

## Tests (full coverage per #2994)
- Server: bot POST with attribution round-trips through list and bus payload; user POST with attribution stores `{}`; oversized attribution 422; unknown key 422; migration applies on a copy of prod.
- Harness: reply POST carries attribution and a body with no suffix; audit line chars equal body length; floor filter drops my own rows at `seq <= floor`, keeps my later rows and keeps every other author's rows at any seq; `[backfill.floor]` with a string key `"4"` parses to `floor_for(4)` (fails if keys are not int-converted); format_line renders the header label for attributed rows and the plain form for rows without; meter flags `— gable`, `-- Gable`, `- gable ·` and leaves the body byte-identical; a body with no trailing sign adds no token; refusal path still appends its suffix.
- Client: MessageList renders the span from a fixture with attribution and nothing without; attributed message forces its header.

## Build record (keyboard, BuildGable seat, Claude Opus 5.5, 2026-09-25)
Branch `loop/2026-09-23-footer-as-attribution`, one hand, cut from main `3b76acf`. Three code commits, laned for review:
- `06d4720fdfcc` server + client (Claudette): `server/app/migrations/017_message_attribution.sql`, `server/app/models.py`, `server/app/routers/messages.py`, `client/src/{types.ts,components/MessageList.tsx,app.css}`; tests `server/tests/test_message_attribution.py`, `client/tests/message_list.test.mjs` and its runner `server/tests/test_client_message_list.py`.
- `b311f6d9a6ad` sdk (both reviewers): `DisjornClient.send(..., attribution=)`, put in the payload only when given. The split above does not name `sdk/`, but the adapter posts through it, so the deploy has to sync it too (see below).
- `71eea9916334` harness/residency (Gable): `adapter.py`, `config.py`, `prompt.py`, `summary.py`, `summon.toml.template`, `README.md`; tests `tests/test_attribution.py` (new), plus the five assertions in `test_model_pin`, `test_model_gate` and `test_bot_summons` that read the in-body suffix now read the field.

**Tests, as the list above names them.** Server: round-trip through scrollback, `from_seq` backfill and the bus payload; a user's attribution stores `{}`; oversized (`model` over MAX_METADATA_CHARS) 422; unknown key 422, and a non-bool `verified` 422; migration 017 on a database built from 001–016 holding an old footered row (row reads `{}`, body untouched, `integrity_check` ok). Harness: reply POST carries `{model, verified, summoner}` and a body equal to the session's text (verified, unverified, bot-summoned unpinned, unpinned human sends none); audit `posted #N (len(body) chars)`; floor drops Gable/GABLE bot rows at 55 and 60 with floor 60, keeps Gable at 61, and keeps plink, claudette, BuildGable and a user named `gable` at any seq; a channel without a floor keeps Gable's rows; `[backfill.floor]` `4 = 3065` loaded from a TOML file gives `floor == {4: 3065}` and `floor_for(4) == 3065`, unset gives 0; format_line label (verified, unverified, no summoner, no seq), plain form for no attribution, `{}` and `model: null`; a newline or `[[/CHAT]]` in an attribution value cannot open a second line; meter flags `— gable`, `-- Gable`, `- gable · …` with the body byte-identical and a WARNING logged; no token for an unsigned body or a sign that is not the last line; budget refusal still ends in `— gable · summoned by bob · refused by gable's daily budget` and carries no attribution; the real SDK puts `attribution` in the POST body (httpx MockTransport). Client: MessageList rendered from a store fixture shows `<span class="msg-attrib" title="model attribution">claude-fable-5-1 · summoned by plink</span>` after `</time>`, the unverified text, nothing for `undefined`/`{}`/`model: null`, and a second same-author message one minute later gets its own header only when attributed.

**Red first.** Against the unchanged tree: server 6 of 6 red (KeyError `attribution`, 200 where 422 was expected, migration list empty); harness 12 of 18 red; the six that passed are negative or unchanged-behaviour rows (no attribution when unpinned, refusal suffix, plain form, the injection guard, the no-floor channel, and the SDK row, which is red against main's SDK: `send() got an unexpected keyword argument 'attribution'`); client 3 of 4 red, the fourth being the "nothing without" row. All green after.

**Suites, the gate's way** (resident image, `--network none`, fresh clone, claudette.git ro + `DISJORN_ADAPTER_CORE`, client node_modules at `/opt/node_modules`): `GATE tests pass`, `GATE typecheck pass`, `GATE build pass`. Server 593 passed (main 586: +6 attribution, +1 client runner, which ran rather than skipped). Harness 2396 passed, 19 skipped (main 2376 passed, 21 skipped: +18 new rows, and the prose wall's two merge-base halves, which main skips for want of a merge-base, run here). The 19 skips are main's own environmental ones (no res-gable account, no visudo, root cgroup). The client bundle's hash moved from `index-DGJgbdj_.js` to `index-DcdxMNgJ.js`. Prose wall 11 passed, no skips.

**Migration on a copy of prod** (sqlite backup API from `?mode=ro`, then the app's own `run_migrations`): applied `['017_message_attribution.sql']` only; 6722 messages, all `attribution = '{}'`; `integrity_check` ok before and after; `foreign_key_check` empty. End to end in-process: the adapter's reply path through the real SDK into the app stores the attribution, and the next transcript line reads `Gable: [#1] (via claude-fable-5-1, summoned by plink) Answer.\n\n-- Gable` with the audit line ending `| hand-signed`.

**Where the spec was silent, and what was chosen.**
1. The attribution goes out under the old suffix's condition, a known model or a bot summoner, so an unpinned human summon sends none and stores `{}`. Rendering keys on `model` as specced, so an unpinned bot summon's `{model: null, summoner}` is stored but shown nowhere. Prod always pins.
2. `Attribution` is `extra=forbid, strict=True`: a wrong type is a 422 too, not a coercion.
3. The transcript label's values go through `safe_name` (control characters dropped, chat markers defanged, 64-character cap), because any bot token can set the field.
4. The meter reads the body as posted (after depth-1 mention demotion), which is what the next backfill shows.
5. The model-gate refusal line still carries no attribution: it is not the session's words.
6. The template the split calls `harness/cc/config-template summon.toml.template` is `harness/residency/summon.toml.template`; it documents `[backfill.floor]` commented out.
7. The client test is `node --test` + esbuild + `react-dom/server`. It runs from the server suite because the gate's client step only typechecks and builds, and finds its toolchain at `/opt/node_modules` there. A server render reads a zustand store's initial state, so the fixture is written into it.
8. Prose wall: two narrative comments in the reply path and one stale module-docstring line were cut to their constraint; four baseline lines lowered to the measured values, none raised.

**Pre-existing, not touched:** `sdk/tests` (not in the gate) cannot start its scratch server on main: the fixture sets `COOKIE_SECURE=false`, which startup now refuses. The SDK row above covers this change instead.

**Deploy, as this diff needs it** (the recipe above, plus one line it does not have): 0. `sudo systemctl --user -M res-gable@ stop gable-summon`. 1. `sqlite3 server/data/disjorn.db ".backup server/data/disjorn-pre-017-backup.db"`. 2. Merge. 3+5. `sudo systemctl restart disjorn`: migrations apply at startup, so this is step 3 and step 5 together; the log line `applied migrations: 017_message_attribution.sql` is the check. 4. `cd client && npm run build` before the restart, then check the content-hash name changed. 6. Read `max(seq)` in #custodian, and add `[backfill.floor]` with `4 = <that seq>` to `/srv/disjorn-resident-config/res-gable/summon.toml`, after `[backfill.per_channel]`. 7. Sync `harness/residency` → `/usr/local/lib/disjorn/residency` **and** `sdk/disjorn_sdk/client.py` → `/usr/local/lib/disjorn/disjorn_sdk_pkg/disjorn_sdk/client.py` (the venv's editable install maps there). A new adapter on the old SDK raises TypeError on every reply, and the audit line reads `posted none`. Then `sudo systemctl --user -M res-gable@ start gable-summon`. 8–9 as above.

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
- **Split agreed in #custodian**: 3007

## Expected diff tier
Tier 2. The adapter change edits the path that produces my own posts, and the server gains a bot-writable field. Two-way review on both halves, as plink ruled (#2994).

## Token estimate
150k–250k build tokens. Small diff, wide test surface.

## Deploy recipe
0. Stop the summon adapter, so no footered reply lands between the merge and its restart. 1. Backup prod DB. 2. Merge. 3. Apply migration 017. 4. Rebuild client, check the content-hash name changed. 5. Restart server. 6. Read the current max seq in #custodian and write `[backfill.floor]` `4 = <that seq>` in /config/summon.toml. 7. Sync the deployed residency copy (`/usr/local/lib/disjorn/residency`) from the merged tree AND copy `sdk/disjorn_sdk/client.py` into `/usr/local/lib/disjorn/disjorn_sdk_pkg/disjorn_sdk/` (the adapter's editable install; the new adapter on the old SDK fails every reply with a TypeError), then start the summon adapter (config is read at startup only; the floor is live from this start). 8. Post the deploy record in #custodian. 9. Watch the audit line for `hand-signed` over the following days; delete the floor line after 30 clean posts of mine.

## Confirm record
- **Confirmed by**: plink
- **#custodian seq**: 3007
- **Confirmed at**: 9/23/2026
<!-- No Confirm record → no build. This is the gate. -->

## Status
built@loop/2026-09-23-footer-as-attribution
