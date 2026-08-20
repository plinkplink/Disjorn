# Spec: `read_repo_file` grows a `rev` — branch vision for the bot seat

<!-- DRAFT written at the keyboard (plink's session, 2026-08-19) from
res-claudette's proposal at #custodian seq 1323, so the next step is her
review of it rather than a blank page. A keyboard draft authorizes nothing:
the Confirm record below is empty until a human confirms in #custodian. -->

## Request
- **Verbatim**: "`read_repo_file` gains an optional `rev`, defaulting to
  today's main working tree, accepting `main`, a sha, or
  `gatehouse/<repo>/<branch>`, resolved read-only against the mirror. Plus a
  cheap mode returning just the blob sha / last commit touching a path, which
  is what kills the read-main-twice token burn when I am checking whether a
  file moved."
- **Requester**: res-claudette (proposal, Ask A); ordered built by plink,
  2026-08-19, keyboard session ("we need to get that built").
- **Origin**: #custodian seq 1323 (file-vision follow-on). Deferral being
  discharged: SPECS/2026-08-14-file-vision.md shake-out ("deferred to the
  tools discussion, plink 08-15") — this spec IS that discussion's landing.

## Agreed UX
1. **`rev` argument.** `read_repo_file` accepts an optional `rev` string.
   Absent → exactly today's behaviour (checked-out main working tree; zero
   change for existing callers). Present → the path is resolved at that rev
   via read-only git plumbing against the mirror (`git -C /opt/disjorn show
   <rev>:<path>` or `cat-file`), never a checkout, never a write. Accepted
   forms: `main`, a sha, or `gatehouse/<repo>/<branch>` — validated with the
   broker's existing rev charset rule (`[A-Za-z0-9._~^/{}-]`, max 200, no
   leading dash) PLUS an explicit `..` substring rejection: `.` is in the
   charset, so "no range operators" must be a check of its own, not an
   implication (Claudette review, 2026-08-19). The `gatehouse/<repo>/<branch>`
   form is normalized to the full `refs/gatehouse/<repo>/<branch>` inside the
   tool and tested as such — today it only resolves via git's DWIM ref search
   order, a lookup precedence nobody in this house will remember in November.
   The rev branch carries the SAME 200 KB cap and truncation message as the
   working-tree branch (a blob at a rev eats context just as fast), and an
   `ls-tree` listing caps its entry count too. A directory path at a rev
   lists via `ls-tree`, same shape as today's listing. Failures answer
   plainly and DISTINCTLY: "rev unknown in the mirror — refresh_mirror
   first?" is a different 3am diagnosis from "path absent at that rev", and
   the tool must never collapse the two.
2. **`sha_only` mode.** Optional boolean. True → return only the sha of
   `<rev>:<path>`, no content: blob sha for a file, TREE sha for a
   directory (a directory that moved is motion too), with the same
   distinct absent-path / unknown-rev answers as item 1. Two such calls —
   the branch and `main` — are the motion ping from file-vision item 2,
   finally executable from her seat: zero file reads, zero content tokens.
   This closes the half-closed CLOSES claim in the file-vision shake-out
   (line: "half-closed for her seat"). NOTE the seam: absent `rev` reads
   the checked-out working tree while `rev="main"` reads the object store,
   and during a reaper refresh those can momentarily disagree — so a
   motion ping passes `main` EXPLICITLY and compares object store to
   object store (folded into the PROTOCOL.md footnote, item 4).
3. **Catalogue membership (proposal Ask B) — DECIDED: (a), a second table,
   with its inertness load-bearing** (Claudette review, 2026-08-19).
   `read_repo_file` is adapter-side, so it sits OUTSIDE the generated-schema
   drift check that file-vision item 3 built. A second table in
   verb_surface.toml describes adapter tools — but verb_surface.toml's whole
   premise is that it describes a surface the broker authorizes from
   verbs.toml, and for adapter tools there is NO verbs.toml row: no third
   authority exists. So the adapter table must be explicitly INERT to the
   generator — checked by a test against core.py's actual schema, never fed
   into schema generation — and it carries its own version of the header's
   "THIS FILE GRANTS NOTHING" sentence, written for the case where nothing
   else grants either. Get that wrong and editing a config file becomes a
   new path that hands a bot a tool — the exact opposite of why the file
   exists. test_verb_surface.py extends to cover the new table in both
   directions (a described tool the adapter lacks, an adapter tool the
   table misses).
4. **PROTOCOL.md footnote (proposal Ask C).** The motion-ping example gains
   BuildGable's field note from #1322: run the two revs as separate
   `rev-parse` calls — the two-arg form gets mangled by quoting under
   `podman exec ... bash -lc`. Also updated to name the bot-seat path now
   that it exists (the "no shell for this yet" parenthetical at
   PROTOCOL.md:112-114 comes out), and to say a motion ping passes `main`
   EXPLICITLY rather than leaning on the no-rev default: the default reads
   the working tree, `main` reads the object store, and during a reaper
   refresh the two can disagree — a ping must compare object store to
   object store.

## Architecture notes
- `bots/claudette/core.py`: `READ_REPO_FILE_TOOL` schema gains `rev` +
  `sha_only`; `_read_repo_file` grows the git-plumbing branch beside the
  existing working-tree branch. The path-escape check stays for the
  working-tree branch; the rev branch never touches the filesystem, so its
  wall is the rev/path validation plus git answering from the mirror's own
  object store.
- Same change lands in `bots/fable` if Gable's adapter carries the tool
  (verify during build; if absent, note it and move on).
- `harness/broker/verb_surface.toml` + `gen_verb_surface.py` +
  `tests/test_verb_surface.py`: the adapter-tools table per item 3 —
  descriptive only, generator-inert (asserted by test), never an input to
  emit-tools.
- `harness/broker/PROTOCOL.md`: item 4.
- Deploy note: adapter code runs from the DEPLOYED copies, not the repo
  (2026-07-23 lesson) — the build's landing step must name the copy-out, and
  Claudette knows it's live when the arg shows up in her own schema (her
  words, seq 1369).

## Lane → Review owner (DETERMINISTIC — filled from the lane, never preference)
- **Lane**: cross-lane (see split).
- **Review owner**: per surface, below.

## Builder (USER PREFERENCE — who orchestrates; never touches Review owner)
- **Builder**: plink's call at confirm.

## Cross-lane split
- **Applies**: yes
- **Surfaces by lane**:
  - Claudette's adapter (`bots/claudette/core.py`, her tool schema) → review
    owner Claudette.
  - custodian (`harness/broker/verb_surface.toml`, `gen_verb_surface.py`,
    its tests, `PROTOCOL.md`) → review owner Gable.
- **Split agreed in #custodian**: <seq — record at confirm; stated here for
  the confirm to witness, same shape as file-vision's>.

## Expected diff tier
Tier 2 advisory — `bots/claudette/core.py` is a protected path
(protected-paths.toml), and verb_surface.toml is broker config.

## Token estimate
One small build slot — an argument, a plumbing branch, a schema table or a
recorded exemption, a doc footnote, tests for each.

## Merge gate note
Gable reads the full diff before this one merges (plink, 2026-08-19): the
change slightly widens Claudette's reach, and a reach-widening change gets a
second pair of eyes before it lands — recorded here as the practice for any
change of that shape, not just this one.

## Shake-out (2026-08-19 — Claudette review, folded)
Approved with six deltas, all folded above: (1) item 3's (a) decided, with
the adapter table explicitly inert to the generator, test-checked against
core.py, carrying its own "grants nothing" header sentence; (2) the rev
branch gets the working-tree branch's 200 KB cap + truncation message, and
ls-tree caps its entry count; (3) `..` rejected by explicit substring check,
not by charset implication; (4) `gatehouse/<repo>/<branch>` normalized to
the full ref, not left to DWIM lookup order; (5) `sha_only` answers for
directories (tree sha), and absent-path vs unknown-rev are distinct
answers; (6) the PROTOCOL.md footnote says a motion ping passes `main`
explicitly — object store to object store, never working tree vs object
store mid-refresh.

## Confirm record
- **Confirmed by**: <pending>
- **#custodian seq**: <pending>
- **Confirmed at**: <pending>
<!-- No Confirm record → no build. This is the gate. -->

## Status
`draft`
