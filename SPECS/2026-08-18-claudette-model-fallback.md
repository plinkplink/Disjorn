# Spec: Claudette's chat model falls back to Opus 4.8 on 529, per turn, tagged

> **RETROACTIVE (keyboard-built, custodian lane).** Built and deployed at the
> keyboard 2026-08-18 after Claudette's turns died with "upstream error: 529"
> (Anthropic answered 529 Overloaded for `claude-opus-5` for ~10 minutes while
> Fable and Haiku answered 200). This touches Claudette's own code (`core.py`),
> which under the quorum rule plink alone confirms — and he ruled it in
> session. Recorded here so Claudette reviews a change to who she runs as, in
> the file, not from a chat tag.

## Request
- **Verbatim**: "Sure: Claudette falls back to Opus 4.8, Gable I guess doesn't
  get a fallback until they release another Fable-class model. Must include a
  note to the resident and a tag in the chat when it happens. Must
  automatically re-engage the correct model when available (each turn checks)."
- **Requester**: plink
- **Origin**: keyboard session, 2026-08-18

## Agreed UX
- Every turn starts on `claude-opus-5`. Only a **529** from it moves the rest
  of THAT turn (its tool rounds) to `claude-opus-4-8`; the next turn starts on
  Opus 5 again. Nothing sticks. Any other status never triggers a fallback; a
  529 from the fallback itself still surfaces as `upstream error: 529`.
- **The resident is told**: every fallback call carries a system-prompt note
  — what she is on, why, that the primary is retried next turn, that 4.8 does
  not run adaptive thinking here.
- **The channel is tagged**: the reply ends with `[fallback: claude-opus-4-8 —
  claude-opus-5 was overloaded (529); the usual model is retried
  automatically next turn]`. Error replies are not tagged.
- **Gable gets no fallback** — there is no other Fable-class model.

## Architecture notes
- `bots/claudette/core.py`: `_TurnModel` (per-turn state; `create()` wraps
  `messages.create`), constants `PRIMARY_MODEL` / `FALLBACK_MODEL` with
  config knobs `CHAT_MODEL` / `CHAT_FALLBACK_MODEL`; `process_query` wraps
  `_process_query_turn` and tags the reply. Verified against a fake SDK
  client: switch on 529, sticky within the turn, fresh turn on primary, both
  overloaded raises, 401 does not fall back, tag/no-tag.
- Deployed to BOTH copies: `/home/plink/bots/claudette/core.py` (Discord-side
  `claudette.service`, kept for now) and
  `/home/res-claudette/resident-home/bots/claudette/core.py` (Disjorn resident,
  `resident-cc.service` restarted 2026-08-18 ~17:00Z).

## Lane → Review owner (DETERMINISTIC)
- **Lane**: custodian (Claudette's code). Review owner: **Claudette**.

## Builder
- **Builder**: keyboard (plink's Claude Code seat, Fable).

## Expected diff tier
Tier 2 (resident surface — model identity).

## Token estimate
Spent: one keyboard session.

## Confirm record
- **Confirmed by**: plink
- **#custodian seq**: <retro-confirm — same one-liner; keyboard fills>
- **Confirmed at**: 2026-08-18

## Status
merged
<!-- keyboard-built and deployed 2026-08-18. CORRECTED 2026-08-20: the original
note here claimed "not a Disjorn-repo commit (bots/ is outside the repo)" —
wrong, bots/claudette is its own git repo (branch disjorn-port), and the
uncommitted tree this reasoning left behind blocked the read-repo-file-rev
merge two days later. The artifact is commit 0094de7 in bots/claudette,
committed retroactively when the block was found. -->
