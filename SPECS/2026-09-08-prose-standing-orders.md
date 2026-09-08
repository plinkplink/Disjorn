# Spec: prose ceiling in code — standing order, gate test, and the first sweep

## Request
- **Verbatim**: "we need stronger standing orders across all residents, agents,
  subagenys, and keyboards that are bloating up the place" / "The standing
  orders as you've laid out plus a line preventing gratuitous history lessons.
  If something is changed, superceded, agreed upon, etc., we rarely need the
  full provenance in excruciating detail. No one dumber than you will be reading
  this code in the future, so what's apparent to you will be obvious to them.
  Exceptions for costly lessons very likely to be repeated, of course."
- **Requester**: plink
- **Origin**: #custodian seq 2379, 2383

## Measured (main `62a2aa5`, 2026-09-08)
Prose = pure-comment lines + docstrings, counted with `tokenize`/`ast`.

| file | bytes | prose bytes | ratio |
|---|---|---|---|
| harness/broker/brokerd.py | 280 KB | 128 KB | 45% |
| harness/cc/apps/apps_harvest.py | | | 52% |
| server/app/routers/apps.py | | | 45% |
| harness/metrics/metrics.py | | | 37% |
| repo, 181 .py files | | | 33% |

Most of the mass is narrative: who ruled what at which seq, what the code
used to do, the incident that motivated the line. That record already exists
in git, SPECS/ and DEFERRED.md. In code it costs every reader and every build
seat tokens on every read, and it put brokerd.py over the 200 KB read cap that
blocked the reviewer of record for two rounds (#2366, #2373).

## Agreed UX
Nothing user-facing. The house sees:
1. **One rule text, three copies.** COMMS.md "Code comments" section grows
   two lines; `harness/cc/build-kernel.md` rule 5 and
   `harness/keyboard/README-KEYBOARD.md` carry the same words. Binds
   residents, build seats, subagents, and the keyboard. The rule:
   - A comment or docstring states a constraint the code cannot show, in
     one sentence. What the code does is not a comment.
   - No provenance in code: no seq citations, dates, names, "ruled by",
     "used to", "the day this was added", or incident narration. Git holds
     who and when; SPECS/ and DEFERRED.md hold why at length. A comment may
     point at a spec slug or DEFERRED heading in five words or fewer.
   - Exception, stated as a constraint: a lesson that was expensive and is
     likely to be repeated keeps one line saying what must not be done and
     what breaks if it is. Never the story of how it was learned.
   - Prose allowance per `.py`/`.sh` file: the larger of 25% of bytes and
     1 KB, so a short helper may carry a short docstring. A new file stays
     within it. An existing file's prose bytes may not rise above its
     recorded baseline, and a build that touches a file over the allowance
     must lower it or say in the banner why it could not.
2. **A wall, not a request.** `harness/tests/test_prose_ratio.py` runs in every
   build-seat suite and at keyboard merge. It reads `harness/prose-baseline.toml`
   (file → prose bytes at adoption) and fails on: any `.py`/`.sh` not in the
   baseline over its allowance; any baseline file whose prose bytes exceed
   its recorded number. The baseline ratchets on prose bytes, not ratio:
   deleting code never trips it, and the only way to pass is to write less
   prose. A build that lowers a file's number rewrites its line, and the
   test fails if a line is ever raised. A second check in the same test
   greps comment and docstring text in lines changed since the merge-base
   for provenance shapes, numeric only: `#` followed by three or more
   digits, `seq` followed by a number, an ISO date. Word shapes (`ruled
   by`, `superseded`, `used to`) are in the rule text, not the grep, because
   code whose subject is seqs and supersession (the broker's seq-taking
   verbs, the memory tools) has to use those words. Escape hatch for a
   numeric hit that is a constraint and not a story: a
   `[citation_allow]` table in the baseline file, one entry per file and
   exact matched text, so the exemption is a reviewed line in the diff and
   not a rewording. Runs on `git diff <merge-base>`; a checkout with no
   merge-base (clean main) skips the citation half and says so.
3. **The digest reports it.** GATE DRIFT gains one line: `prose: worst
   <file> <ratio>; over baseline: <n>`. Report only; the test is the wall.
4. **The first sweep** is its own slice, one build per lane owner: strip
   brokerd.py, apps_harvest.py, routers/apps.py, metrics.py to the ceiling.
   Proof that only prose moved: `ast.dump` of each file with docstrings
   removed is byte-identical before and after, checked by a script committed
   with the sweep and run by the reviewer. The reviewer reads the retained
   comments, not a 100 KB diff. Suites green is necessary, not sufficient.

## Architecture notes
- Counter: one module `harness/prose/ratio.py` (tokenize for `#` lines,
  ast for docstrings, `.sh` = lines starting `#` after whitespace, shebang
  excluded). Used by the test, the digest, and the sweep proof. No other
  copy of the arithmetic.
- Baseline written once at adoption from main, committed as
  `harness/prose-baseline.toml`. Files within their allowance are not
  listed. brokerd.py has 23 lines matching the numeric shapes today; the
  sweep removes them, so the allow table starts near empty.
- The pre-receive hook stays as it is (paths plus a trailer). The wall is
  the suite, which the build seat and the keyboard merge both already run.
- Client `.ts`/`.tsx` not covered by this spec; measure after and amend.
- Nothing here weakens COMMS.md's chat register or spec prose; specs are
  prose by design and are pruned at close, as already written.

## Lane → Review owner (DETERMINISTIC — filled from the lane, never preference)
- **Lane**: cross-lane — see split.
- **Review owner**: per surface, below.

## Builder (USER PREFERENCE — who orchestrates; never touches Review owner)
- **Builder**: plink's call. BuildGable for the counter, test, digest line and
  baseline; sweep slices per lane below.

## Cross-lane split
- **Applies**: yes
- **Surfaces by lane**:
  - shared rule text: COMMS.md, `harness/cc/build-kernel.md`,
    `harness/keyboard/README-KEYBOARD.md` → both residents' queues (COMMS.md
    header rule).
  - custodian (harness/tests, harness/prose, harness/metrics digest line,
    broker sweep of brokerd.py) → review owner Claudette.
  - gable (harness/cc/apps sweep, harness/residency if touched) → review
    owner Gable.
  - server (routers/apps.py sweep) → review owner per the server lane's
    owner of record.
- **Split agreed in #custodian**: binds at the confirm seq for this spec.

## Expected diff tier
Slice 1 (rule text, counter, test, baseline, digest line): Tier 1. Sweep
slices: Tier 2 — the files are protected surfaces, reviewed on the AST proof.

## Token estimate
Slice 1 small: one module, one test, one digest line, three doc edits, one
generated baseline. Each sweep slice medium: mechanical, bounded by the file,
one build slot each. brokerd.py first because it is the one behind the read cap.

## Review record
- Round 1 (Claudette #2400; folded here 2026-09-08): ratio ratchet paid a
  bounty on stripping comments after deleting code → ratchet on prose bytes;
  citation grep fired on code about seqs → numeric shapes only, word shapes
  moved to rule text, allow table as the reviewed escape; 25% with no floor
  hit short helpers → allowance = max(25%, 1 KB).

## Confirm record
- **Confirmed by**:
- **#custodian seq**:
- **Confirmed at**:

## Status
`draft`
