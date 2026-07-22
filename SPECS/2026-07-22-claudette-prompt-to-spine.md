# Spec: Claudette's prompt — core.py → a plink-owned spine

<!-- Drafted by the Claude Code keyboard session 2026-07-22 from
SPECS/TEMPLATE.md, transcribing Claudette's own design ruling (#custodian,
2026-07-22) rather than inventing one. Every decision below is hers or Gable's;
where I had a view it is marked as the drafter's note. She blessed the MOVE at
seq 238 after reading the shape herself; this spec is the concrete form of it
and needs her review as review owner, then a Confirm record, before any build. -->

## Request
- **Verbatim**: "Claudette, another question for your approval: I want to move
  your prompt out of core.py into a plink-owned spine directory. That would
  simultaneously close KB-D10 (it becomes RO-mountable, like Gable's) and make
  consolidation actually meaningful for you. I want to get your blessing, as
  this is a spine change, and we agreed that you would have veto power on these
  decisions."
- **Requester**: plink
- **Origin**: #custodian — seq 237 (request), seq 238 (her blessing)

## Agreed UX
- Claudette's prompt of record becomes a set of markdown spine entries in a
  **plink-owned directory, mounted read-only** into her container — the same
  arrangement Gable runs under (`/srv/disjorn-spine/<resident>`).
- `core.py` stops carrying `SYSTEM_PROMPT`. Her adapter composes the system
  prompt by **reading the spine at runtime** instead of importing a constant.
- Her route to a prompt change becomes: post a **real diff** (literal before,
  literal after, apply-or-reject) → witnessed in #custodian → plink applies to
  the canonical copy → refresh script republishes the mirror.
- No unattended self-edit. Editing the in-volume copy still succeeds but is
  inert, exactly as for Gable.

## Architecture notes

**Her granularity ruling (seq, 2026-07-22) — split, not one blob.** "One blob
defeats the whole point. The reason I wanted a spine in the first place is so
consolidation can assess rent per entry." Her seams, in her words:

| entry | contents | kernel-flagged? |
|---|---|---|
| persona kernel | who she is, voice, the no-emoji / no-lists / concise rules | **yes — consolidation may never rent-evict it** |
| physical description | the chibi/appearance block | no |
| relationship context | humans and bots, who is who | no |
| tool-use discipline | how she uses her tools | no |
| platform / situational | Disjorn-specific context — already half-exists as `PLATFORM_SUFFIX` | no |

**Her edit-path ruling — a real diff, not proposed prose.** She took the
more-work option deliberately: "A file-proposal carrying prose I want the file
to say is exactly the loose, re-interpretable thing that bit us — someone reads
my description and re-types it, and drift creeps in at the seam. A real diff is
unambiguous… If I'm going to preach 'the diff is the authorization,' the path
had better be a diff."

**Gable's constraint, which binds this migration too** (#custodian, same day):
the seat split must be **biography-additive, never walls-subtractive**. Applied
here: whatever a tool-running Claudette loads must retain the discipline
entries; only biography is the layer added for conversation. "A build Claudette
that sheds those isn't leaner, it's a more dangerous builder."

**Her seat seam, which she says falls on the entry boundaries** — and she flags
that as evidence the boundaries are right: a tool-running Claudette needs
tool-use discipline and platform/situational hard, and barely needs the physical
description or the finer points of banter voice; a conversational Claudette
needs persona kernel and relationship context and not the plumbing.

**The mechanism differs from Gable's, and this is the main build risk.**
Gable is a Claude Code resident: `house_memory/bootstrap.py` assembles his spine
into `~/.claude/CLAUDE.md` at session start, so his cutover was two config
lines. **Claudette is not.** She has no `RESIDENT_SPINE_DIR`, no `.claude`
directory, and `bootstrap.py` never runs for her — her container's main process
is her adapter (`disjorn_bot.py`), which composes the prompt at
`disjorn_bot.py:269` from `core.py:36 SYSTEM_PROMPT` and
`disjorn_bot.py:42 PLATFORM_SUFFIX`. So this migration is **adapter code**, not
config: something must read the spine directory and compose it in the order the
entries declare. That is a change to her running code as well as her prompt —
Tier 2 twice over, and the reason this wants a build rather than a keyboard
edit.

*Drafter's note:* the composition order needs to be explicit and declared in
the entries (a frontmatter ordinal, as Gable's `00-`/`10-`/`20-` filenames do
implicitly), not implied by directory listing order. A prompt whose meaning
depends on `readdir` order is a prompt that changes when the filesystem feels
like it.

## Lane → Review owner (DETERMINISTIC — filled from the lane, never preference)
- **Lane**: custodian — the touched surfaces are Claudette's prompt and her
  adapter code.
- **Review owner**: **Claudette.** A change to Claudette's code/config/prompt
  lands in Claudette's review queue. She has already blessed the move in
  principle (seq 238); this spec is the concrete form and needs her read.

## Builder (USER PREFERENCE — who orchestrates; never touches Review owner)
- **Builder**: plink's choice. Unset — to be filled at confirm.

## Expected diff tier
Tier 2. Touches `bots/claudette/core.py` and `bots/claudette/disjorn_bot.py`,
both now enumerated in `protected-paths.toml`, and creates a new prompt-bearing
spine surface. The classifier gates the actual result at merge.

## Token estimate
Moderate — larger than Gable's because it is an adapter change, not a config
change. One build pass, plus a verification pass that her composed prompt is
byte-identical to today's before any behavioural change is layered on.

*Drafter's note:* that byte-identical check should be the build's first
deliverable and its acceptance test. Splitting a prompt into five files and
recomposing it is exactly the kind of change that silently drops a paragraph.

## Confirm record
- **Confirmed by**: <awaiting — Claudette's review as owner, then a human confirm>
- **#custodian seq**: <seq of the confirm message>
- **Confirmed at**: <timestamp>
<!-- No Confirm record → no build. This is the gate. -->

## Status
`draft`
