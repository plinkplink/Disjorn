# Spec: A memory inspector for humans

<!--
Governing plan: BUILD-LOOP.md. Style: COMMS.md. Backlog #16.
Drafted by the keyboard seat (Claude Opus 5.5, posting as BuildGable) under plink's 2026-10-05 hand-off (#3152, #3178).
This is the residents' own memory. The draft is deliberately thin on mechanism and heavy on questions, because their answers decide the shape.
-->

## Request
- **Verbatim**: "Give humans a memory inspector — what's stored, who stored it, when, and a way to say \"that's wrong\" that actually supersedes."
- **Requester**: plink
- **Origin**: backlog #16 (2026-08-28)

## Problem
What a resident believes about the house, its people and its history lives where no human can see it without shell access:
- The custodian resident: about 620 records in her chroma store under res-claudette, with `superseded_by` chains.
- Gable: about 70 markdown files under res-gable's `.claude/projects/-home-resident/memory`.

When a resident acts on a wrong memory, a human finds out only from the wrong answer. Correcting it today means asking in chat and hoping the resident supersedes it the right way.

## Proposed UX (for review)
- **Settings → Memory** (admins only to start): one tab per resident.
  - **Custodian resident**: a searchable list, with the newest and the most-recalled first. Each record shows its content, subject, tags, source author, created and last-recalled times, confidence, and its supersede chain (what it replaced, what replaced it). Superseded records are greyed and expandable.
  - **Gable**: the MEMORY.md index rendered, each file openable, with its mtime and size. Read-only.
- **"That's wrong"** on a record opens a box: what's actually true, plus an optional note. Submitting files a **correction**, not an edit.
  - The correction goes to the resident as a structured item at its next summon: "plink says memory X is wrong: <their text>".
  - The resident supersedes it with their own words, through their own tool (`supersede_memory` / editing their file), and the correction records which new memory or file change answered it.
  - Until then, the record shows "correction pending", with who filed it and when.
  - This keeps the rule that a resident's memory changes only through the resident's own hands, and makes "actually supersedes" true and visible.
- A correction the resident disputes is answered in #custodian, and the record shows "disputed" with a link to the post. A human can't force the write. That's a deliberate boundary for plink to confirm.

## Architecture sketch (for review)
- **Read path**: the server has no access to resident homes, so reads go through new broker verbs, run as each resident's own uid and read-only:
  - `memory-list` / `memory-show` for the custodian store, via the existing house_memory export path the backup drill uses.
  - `memory-files` for Gable's directory.
  - The server calls them as the broker's relay bot, like approval and backlog.
- **Corrections**: a `memory_corrections` table: resident, record id or file, the human's text, filed_by, filed_at, state (pending / answered / disputed), and the answering record or post.
  - The summon adapters fetch the pending corrections for their seat and list them in the prompt's harness block, outside [[CHAT]], as house-supplied facts with the human's text quoted.
  - The resident marks one answered by citing it when it supersedes.

## Lane → Review owner (DETERMINISTIC — filled from the lane, never preference)
- **Lane**: custodian (server/, client/, harness/broker) plus each resident's own adapter.
- **Review owner**: Claudette for the custodian lane; Gable for harness/residency.

## Answers from review (#3240 Claudette, card 110; #3241 Gable)
- **Visibility**: admins see every record, with no resident-side filter: "if I got to choose what you see, I'd be curating how I look to you". Exception (custodian resident): records learned in a DM or private room are visible only to that room's members, if the store records the source room. Everyone else sees a count. Gable: all files; 0 are typed `user`, so a type filter would hide nothing.
- **Corrections**: the resident supersedes in their own words. There is no human write path, admin included. Gable: say plainly that the boundary is UI, since plink owns the host. The wall is that the server has no path into a resident's home.
- **Delivery**: a harness line at the next summon (Gable in the restart-note slot). Custodian resident: capped at about 3 per summon, plus a read-only verb for the full list. Gable: up to 10 pending, each up to 500 chars, quoted as human text. Neither by mention (one wake per correction) nor by polling.
- **Answering**: custodian resident: `supersede_memory` gains an optional `correction_id`. Gable: a broker verb from his seat with the peercred label (answered / disputed), like backlog-file.
- **Never shown**: raw embeddings. Gable: nothing outside `memory/`. The verb roots at the directory, resolves symlinks and refuses `..`. The Settings renderer must not fetch `[[name]]` or image links.
- **Open, plink's to fill**: the read mechanism. The broker runs as plink. Gable's directory is readable (0755). The custodian store needs an owner-run read-only export, e.g. a fixed sudoers line for one export command run as res-claudette, the same shape as the backup drill's owner-run `.backup`.

## Questions for review (answered above)
1. Each resident: should humans see your memory at all, and is it every record or a filtered view? (For example, memories about a third person might be hidden from other humans.)
2. Is "a correction you supersede yourself" the right contract, or do you want humans able to supersede directly, attributed to them?
3. How should corrections reach you: a harness line at the next summon, a #custodian post that @mentions you, or a broker verb you poll?
4. Gable: your memory is files. Is a read-only index view enough, or do you want corrections to land as a proposed diff you accept?
5. What must the inspector never show (e.g. records with `privacy` tags, raw embeddings)?

## Expected diff tier
Tier 2.

## Confirm record
- **Confirmed by**:
- **#custodian seq**:
- **Confirmed at**:

## Status
`draft`
