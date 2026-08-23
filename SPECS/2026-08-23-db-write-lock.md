# Spec: db.py write lock — serialize transaction() and autocommit execute() (backlog #6)

## Request
- **Verbatim**: "backlog entries can race an error and cause users to create duplicates" (/backlog row 6)
- **Requester**: plink
- **Origin**: #custodian seq 1590 (filing); evidence seq 1585 (journal trace, BuildGable); diagnosis seqs 1586 + 1605 (Claudette, from the file)
- **Revision 2** (2026-08-23): folds Claudette's blocking review finding on the first build (#1644, branch `loop/2026-08-23-db-write-lock` at cd6b43d): the bool ContextVar guard is inherited by child tasks and the parent's reset cannot reach their copies — replaced with a mutable sentinel. Round 1 preserved as `loop/2026-08-23-db-write-lock-r1`. This revision requires a fresh confirm seq (revision rule, #1462); the round-1 confirm (seq 1625) does not carry over.

## Agreed UX
No visible feature. The failure disappears: no more 500-after-the-write on
concurrent requests (whose retry files a duplicate), and no more silent
commit of another handler's half-finished transaction block. The silent
sibling is the actual defect this spec exists for — `execute(commit=True)`
on the shared connection commits whatever any concurrent `transaction()`
block has done so far, no error, and the later `rollback()` rolls back
nothing. The duplicate rows were the loud cousin.

## Architecture notes
Per Claudette's #1605 + #1615, all in `server/app/db.py`:
- One module-global `asyncio.Lock`.
- `transaction()` acquires it around the entire `BEGIN IMMEDIATE` …
  commit/rollback block.
- `execute()` acquires it only when `commit=True`, around the
  execute-and-commit pair.
- `execute(commit=False)` must NOT acquire it — it already runs inside a
  block that holds the lock, and `asyncio.Lock` is not reentrant; acquiring
  there deadlocks the whole server on the first transaction.
- **The wedge trap (Claudette #1615):** `execute()` defaults to
  `commit=True`, so any call site inside a `transaction()` block that
  forgot `commit=False` is today's silent corrupter — and post-lock it
  becomes a hard deadlock of the whole server (a task acquiring a lock it
  already holds). The naive lock converts "quietly wrong" into "everything
  stops." Two required countermeasures:
  - **Call-site audit before the diff is written**: every `db.execute(...)`
    call inside a `transaction()` block. Callers using the yielded
    `conn.execute` directly are unaffected; only `db.execute(...)` ones
    matter.
  - **Reentrancy guard, so correctness doesn't rest on the audit staying
    true**: a `contextvars.ContextVar` set inside `transaction()`;
    `execute(commit=True)` checks it, and if the current call is inside a
    live block it skips the acquire AND skips the commit — joining the
    enclosing block instead of committing it out from under itself. That
    is the semantics every such caller meant, and it fails soft instead
    of hanging.
  - **The guard is a mutable sentinel object, not a bool (Claudette
    #1644, blocking finding on round 1 — supersedes the plain-bool shape
    from #1619).** Contexts are *copied into child tasks* — `asyncio.gather`
    inside a block is enough — so a child inherits the flag while holding
    no lock, and the parent's `reset(token)` runs in the parent's context
    and cannot reach the child's copy: a task that outlives the block
    would carry `True` forever, and every subsequent `execute(commit=True)`
    in that task silently declines to commit, for the life of the task.
    (Comparing `current_task()` instead deadlocks the awaited-gather case —
    the wedge trap a third time.) Instead the ContextVar holds a
    `_WriteBlock` object with a mutable `active` field: children inherit
    the *reference*, so the parent's `finally` does both
    `block.active = False` (visible in every inherited context) and
    `var.reset(token)`. `execute(commit=True)` joins the block only when
    the sentinel exists AND `active` is true. A child awaited inside the
    block joins it — no deadlock; a child that outlives the block sees
    `active=False`, takes the lock, and commits for real; the parent
    after the block is unchanged. General shape, recorded for the next
    reader: a ContextVar bool answers "was I created inside a block,"
    never "do I hold the lock" — those are the same question only until
    someone spawns a task.
- **Stated behavior change (Claudette #1619), recorded as intent:** a
  nested `execute(commit=True)` now rolls back with the enclosing block.
  A write that used to become durable on its own (by committing the
  enclosing block early — the defect) now joins the block and dies if
  the outer block fails. That is what every such caller meant; it is
  stated here so a future diff-reader finds it as intent rather than
  inferring it from a ContextVar.
- `run_migrations` untouched (startup-only, single task) — but the diff
  says so in a comment where it writes around the lock, so the exemption
  is declared, not discovered (#1644).
- **Docstring obligations (#1644, non-blocking round-1 findings, folded
  so the rebuild lands them in one pass):** `transaction()`'s docstring
  states that it holds a process-global lock across every await the
  caller makes inside the block — slow work (an HTTP call) inside a
  block stalls every writer on the server. Note in `close()` that the
  lock is rebuilt by `connect()` because an `asyncio.Lock` binds to its
  loop. `execute(commit=False)` called *outside* any transaction is the
  one remaining unguarded write path; state it where the parameter is
  documented.
- `busy_timeout=5000` stays but is irrelevant to this bug: one connection in
  one process, SQLite never sees the contention — the race is entirely ours.

Regression tests (the part that makes it real), four cases:
1. Two concurrent tasks, one inside `transaction()` with an `await`
   mid-block, one doing an ordinary `execute()`; assert no
   `OperationalError` and no partial write made durable.
2. The silent-commit shape: assert the concurrent autocommit `execute()`
   does not make the transaction's half-finished writes durable.
3. Nested case: `execute(commit=True)` called from inside a
   `transaction()` block completes without deadlock and does NOT commit
   the enclosing block early — the write becomes durable only when the
   block commits. After the block exits, a subsequent
   `execute(commit=True)` in the same task commits normally — this half
   of the test is what proves the `finally` reset.
4. Child-task case (#1644, proves the sentinel): a task spawned inside a
   `transaction()` block whose `execute(commit=True)` runs *after* the
   block has exited must acquire the lock and become durable on its own —
   durability asserted through a second connection, like the others.
Tests 1–2 must fail before the lock lands — if they pass on unpatched
code, the test is wrong. Test 4 is different and the build seat should
know it: it passes trivially on unpatched `main` (no guard exists to
mis-inherit) and fails exactly on the round-1 bool guard (cd6b43d, which
the build seat cannot see — single-branch clone, by design). Its job is
to pin the sentinel semantics so any future refactor back to a bare bool
fails loudly; fail-first discipline for it is against round 1, on the
record here, not something the rebuild can rerun. Round 1 survives as
`loop/2026-08-23-db-write-lock-r1` in the gatehouse, so this is
reproducible, not merely attested: check out `-r1`, run test 4, watch it
fail (Claudette, #1659).

## Lane → Review owner (DETERMINISTIC — filled from the lane, never preference)
- **Lane**: custodian — `server/app/` is Claudette's surface.
- **Review owner**: Claudette.

## Builder (USER PREFERENCE — who orchestrates; never touches Review owner)
- **Builder**: Gable (Claudette #1615: she wrote the fix shape in channel;
  building it herself and then reviewing her own diff is what the lane
  split exists to prevent).

## Expected diff tier
Tier 2 expected — the write path under every request in the house.
Advisory; the classifier gates the actual result at merge.

## Token estimate
Small fraction of a slot — the lock plus the sentinel guard is ~30
lines, plus four regression tests and the call-site audit. The rebuild
re-implements the whole spec from `main` (it cannot see round 1's
branch); this file therefore describes the complete fix, not a delta.

## Confirm record
- **Confirmed by**: plink
- **#custodian seq**: 1668
- **Confirmed at**: 2026-08-23
<!-- No Confirm record → no build. This is the gate. Revision 2 needs a
     fresh seq; round 1's seq 1625 confirmed different bytes. Seq 1668
     confirms revision-2 bytes ("this message is the confirm ... for the
     db-write-lock revision"). -->

## Status
failed
<!-- set by the broker on 2026-08-23 17:08Z (start-build, 2026-08-23-db-write-lock): build failed: exit 1: Running as unit: disjorn-build-2026-08-23-db-write-lock.service; invocation ID: 8cde1c5ad0ba4bcfa02965bd47b926a9 warning: You appear to have cloned an empty repository. run-build: auth: CLAUDE_CODE_OAUTH_TOKEN from /srv/disjorn-build-config/gable/env run-build: container exited 1 — not publi. To allow another build, set this back to `confirmed` (the confirm record above still stands). -->
<!-- set by the broker on 2026-08-23 17:07Z (start-build, 2026-08-23-db-write-lock): build running as disjorn-build-2026-08-23-db-write-lock.service -> loop/2026-08-23-db-write-lock, launched by gable (confirmed by plink, #custodian seq 1668). Not buildable again until this line moves. -->
