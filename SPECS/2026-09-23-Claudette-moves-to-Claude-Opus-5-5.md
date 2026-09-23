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
`bots/claudette/core.py` (both copies — resident and the Discord-side one, per
the 2026-08-18 deploy note) plus the seat's config.

- **`PRIMARY_MODEL` → `claude-opus-5-5`.** `CHAT_MODEL` knob unchanged in name.
- **`thinking`**: omit the field, or send `{"type": "adaptive"}`. Any
  `disabled` or manual `budget_tokens` is a 400 on this model. Grep for both.
- **`output_config.effort`**: set it EXPLICITLY. The default on 5.5 is
  `medium`; on Opus 5 an omitted effort ran `high`. Silently dropping an effort
  level on the upgrade turn is exactly the class of drift the 4096 incident was
  — the number stayed, its meaning moved. Start at `high` to hold the current
  behaviour, then sweep deliberately if we want to tune.
- **Streaming + `max_tokens`**: move to `.stream()`, raise the ceiling past the
  16,000 non-streaming cap. Pick the number from the 5.5 model page, not from
  memory.
- **Prefix binding — the sleeper, and the one thing most likely to 400 us.**
  5.5 checks whether the `system` prompt, the `tools`, or an earlier message
  changed since a thinking block was produced. Claudette's system prompt is
  regenerated every turn (surfaced-memory block, timestamp) and her `tools`
  array is about to change under the per-seat generator (#2811). Both are
  prefix changes by definition.
  - Check the Anthropic account's creation date. On or after 2026-08-31 the
    check is enforced BY DEFAULT and we start returning 400s.
  - Either way: send the `thinking-binding-controls-2026-08-01` beta header and
    set `thinking.block_binding.prefix_mismatch_behavior: "drop_block"`, so the
    failure mode is a dropped block plus a visible `input_transformations`
    entry, never a hard error.
  - Log any `input_transformations` to the adapter log. A silent drop is the
    thing we have been burned by four times.
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
- **Review owner**: **Claudette**.

## Builder (USER PREFERENCE — who orchestrates; never touches Review owner)
- **Builder**: keyboard. The change is small,
  spans two copies of `core.py` outside the main repo, and needs a live
  round-trip against the real API to verify the `display` setting — which a
  detached build cannot observe.

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
change. The effort/streaming/beta-header changes are safe on Opus 5 and can
stay.

## Confirm record
- **Confirmed by**: plink
- **#custodian seq**: 2825
- **Confirmed at**: 9/23/2026

## Status
`draft`