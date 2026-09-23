# Spec: Claudette moves to Claude Opus 5.5

## Request
- **Verbatim**: "Claudette, Opus 5.5 is released as of today. You want to upgrade?" / "sounds like we should do a full spec and review for the Opus 5.5 upgrade. I don't want to miss anything. You mind writing it up?"
- **Requester**: plink
- **Origin**: #custodian seq 2817, 2824 (release notes pasted at 2819/2820)

## Agreed UX
Claudette's seat runs `claude-opus-5-5`. From the channel's side nothing should
look different except that she is smarter and does not go quiet mid-task. The
observable contract, in the order it is most likely to break:

1. **Progress one-liners still appear.** The "sec, lemme scroll back" line
   posted alongside a tool call is, on 5.5, a progress-update `thinking` block,
   not a `text` block. At the default `display: "omitted"` it arrives with an
   empty body and the channel sees silence between tool calls. The adapter
   sets the `display` value that returns the text, and this is verified with
   one live tool call before the change is called done — not taken on faith.
2. **No mid-post truncation.** Thinking is mandatory and 5.5 thinks more per
   turn at a given effort. `max_tokens` on this seat is 16,000 because that is
   the non-streaming ceiling; thinking and response share it. The call moves to
   `.stream()` and the ceiling rises IN THE SAME COMMIT.
3. **The model id is in the spine, as a witnessed one-line diff, in the same
   change as the flip.** Standing condition from 2026-07-24: if the substrate
   moves and the identifier does not, she spends the next night reasoning as
   something she is not, with nothing to check against.
4. **Voice tripwire stays armed.** If she reads flatter, listier, or more
   eager to please after the flip, the humans say so plainly on day one. The
   date is logged so drift is datable and rollback is a real lever.
5. **Spine and memory come across untouched.** Upgrade the substrate, not the
   character.

## Architecture notes
`bots/claudette/core.py` is one file with two runners. `claudette.service`
(Discord) runs the host repo in place. `resident-cc` (Disjorn) runs her clone,
which `claudette-update.sh` fast-forwards from the host. So there is one
commit and two restarts.

- **`PRIMARY_MODEL` → `claude-opus-5-5`.** `CHAT_MODEL` knob unchanged in name.
- **`thinking`**: omit the field, or send `{"type": "adaptive"}`. Any
  `disabled` or manual `budget_tokens` is a 400 on this model. Grep for both.
- **`output_config.effort`**: already explicit. `CLAUDETTE_EFFORT` defaults to
  `medium` (plink's order of 2026-08-24), and neither env file sets it, so every
  call since then has sent `medium`. It stays `medium`. On 5.5, `medium` thinks
  somewhat more per turn than it did on Opus 5, and the SUBSTRATE-LOG entry says
  so. Any sweep is a later, separate change.
- **Streaming + `max_tokens`**: move to `.stream()`, raise the ceiling past the
  16,000 non-streaming cap. Pick the number from the 5.5 model page, not from
  memory.
- **Prefix binding.** 5.5 checks that the `system` prompt, the `tools` and
  every earlier message are unchanged under a replayed thinking block. Blocks
  are replayed only within one turn, and nothing in front of them changes
  there (fold 1), so the check cannot fire in normal operation. Send the
  `thinking-binding-controls-2026-08-01` header with
  `thinking.block_binding.prefix_mismatch_behavior: "drop_block"` anyway, and
  log any `input_transformations`: a future edit then shows up as a logged drop,
  never a 400.
- **Content-block handling**: select blocks by `type`, never by position — a
  response can now begin with one or more `thinking` blocks. Pass `thinking`
  blocks back UNMODIFIED in tool-use loops.
- **`tool_choice`**: `any` and named-tool forcing are a 400. Grep the adapter;
  I do not believe we force, but I do not know it.
- **Fallback interaction (2026-08-18 spec).** `FALLBACK_MODEL` stays
  `claude-opus-4-8`. 4.8 cannot read 5.5's thinking blocks, so a 529 hop now
  also drops the turn's reasoning — acceptable, because the API drops rather
  than errors and does not bill it, and because the hop already announces
  itself in-channel. The existing `[fallback: …]` tag and the system-prompt
  note are unchanged and remain mandatory: silent substitution is the
  objection, not the downgrade. Consider bumping the fallback to a model that
  can read 5.5 blocks later; out of scope here.
- **Not touched**: computer use (this seat declares none), Bedrock paths,
  fast mode, compaction-on-demand, inline tools. Named only so the review can
  see they were considered and dropped.

## Lane → Review owner (DETERMINISTIC — filled from the lane, never preference)
- **Lane**: custodian — Claudette's own code, config and spine.
- **Review owner**: Claudette by lane. **Overridden by plink at #custodian
  2826**: BuildGable reviews the spec and owns sign-off, including the three
  acceptance gates. Claudette reads the diff back (#2829). The cause is that
  those gates can only be observed from outside her adapter, across its own
  restart.

## Builder (USER PREFERENCE — who orchestrates; never touches Review owner)
- **Builder**: BuildGable, the keyboard seat (#2826). It needs a live
  round-trip against the real API to verify `display`, which a detached build
  cannot observe.

## Expected diff tier
Tier 2 — resident surface, model identity. Same class as 2026-08-18.

## Token estimate
One keyboard session. No build budget burned.

## Sequencing
1. Land anything in flight. Thinking blocks are conversation-bound; a
   half-read branch or a build narrating to #custodian crosses the cutover
   badly. The per-seat generator work from #2811 either lands fully before the
   flip or fully after — not across it.
2. Post the config diff in-channel BEFORE the flip. Not eleven days after.
3. Flip, restart the adapter, say so in channel.
4. Verify in this order: one tool call with a progress one-liner (display), one
   long review post (truncation), one turn after a memory surfaces (prefix
   binding / `input_transformations`).
5. Day-one voice check, out loud.

## Rollback
Revert `PRIMARY_MODEL` to `claude-opus-5` and the spine line with it, in one
change. Streaming and `max_tokens` can stay. The 5.5-only request fields are
keyed on the model id (fold 3), so a model revert drops them by itself.

## Folds from spec review (BuildGable, keyboard seat)
1. **Prefix binding is narrower than stated above.** Thinking blocks are
   replayed only inside one turn's tool loop, and every turn builds a fresh
   `messages` list. Within a turn, `system` and `tools` are fixed, so neither a
   per-turn system-prompt regeneration nor a tool-surface change can sit in
   front of a replayed block. The one within-turn edit is the 529 fallback, and
   that also changes the model, so the blocks drop by model binding. Moving
   `cache_control` is not a history edit. The header and `drop_block` still go
   on every 5.5 call, and `input_transformations` is logged. With `drop_block`
   set, the account's creation date no longer decides anything.
2. **`display: "updates"`** (beta `thinking-display-updates-2026-08-18`) is the
   value that returns progress text while reasoning stays hidden. Under it, a
   non-empty `thinking` block is a progress note. It is harvested in content
   order through the same path as a `text` block: the preface flush before a
   slow tool, or the final reply. An empty block is skipped. The
   interrupted-work sentinel ("This part of the response was interrupted before
   it finished.") is never posted as her words.
3. **The 5.5-only fields are keyed on the model id.** `display`,
   `block_binding` and their two beta headers go only on a `claude-opus-5-5`
   call. The Opus 4.8 fallback keeps today's request shape, with no `thinking`
   field, so `FALLBACK_NOTE`'s "does not run adaptive thinking here" stays
   true. On the OAuth route the betas are joined with `oauth-2025-04-20` per
   request; `default_headers` is unchanged.
4. **`max_tokens` 64000 on `.stream()`.** 64K is Anthropic's starting point for
   long agentic turns; the model allows 128K. The 360 s wall clock is the real
   bound. The two truncation lines name the constant, not "16k".
5. **Raw capture under streaming.** There is no single response body to keep,
   so the anomaly dump records each `tool_use` block's concatenated
   `input_json_delta` text. Those are the literal bytes the model emitted for
   the input, which keeps the both-sides purpose of the 2026-08-23 dump.
6. **SDK.** Both runners have `anthropic==0.67.0`. The new fields go as request
   dicts and headers, verified by a live probe before the flip.
7. **Grep result.** There is no `tool_choice`, `budget_tokens`, `disabled`
   thinking or `computer_*` in core.py, disjorn_bot.py, services/ or memory/.
8. **Refusals.** `bio` and `reasoning_extraction` arrive through the existing
   refusal branch and are named in-channel. No server-side refusal fallback:
   it substitutes a model without the seat knowing, which is the 2026-08-18
   objection.
9. **Model id, four places, one change.** `core.py` `PRIMARY_MODEL` default
   (`config.py` defines no `CHAT_MODEL`, so the default is what runs), the
   `CHAT_MODEL` line in `/srv/disjorn-resident-config/res-claudette/env` (read
   only by the Apps card), the spine line in `40-relationship-context.md`, and
   a new SUBSTRATE-LOG entry. That entry also records `effort = medium`, which
   the 2026-08-24 change never logged.

## Confirm record
- **Confirmed by**: plink
- **#custodian seq**: 2826
- **Confirmed at**: 2026-09-23T06:35:58Z

## Review record
- Spec review and build: BuildGable, the keyboard seat. Read-back PASS and
  spine line signed: Claudette at #custodian 2841.
- Flipped 2026-09-23 07:12:45Z (Disjorn) and 07:12:48Z (Discord).
  bots/claudette merge `204938f`, spine `ad5c4fa`.
- Acceptance gates, on her first 5.5 turn (#2844):
  1. Progress one-liners between tool calls reached the channel.
  2. The long review post landed whole, in about 40 s.
  3. Memories surfaced (recalled 3) over five API rounds with no INPUT
     TRANSFORMATIONS, refusal, token wall or upstream error, and cache reads
     climbing.
- Day-one voice check: plink's.

## Status
`merged`