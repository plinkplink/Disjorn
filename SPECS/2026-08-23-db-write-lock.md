# Spec: db.py write lock — serialize transaction() and autocommit execute() (backlog #6)

## Request
- **Verbatim**: "backlog entries can race an error and cause users to create duplicates" (/backlog row 6)
- **Requester**: plink
- **Origin**: #custodian seq 1590 (filing); evidence seq 1585 (journal trace, BuildGable); diagnosis seqs 1586 + 1605 (Claudette, from the file)

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
    `execute(commit=True)` checks it, and if the current task already
    holds the write lock it skips the acquire AND skips the commit —
    joining the enclosing block instead of committing it out from under
    itself. That is the semantics every such caller meant, and it fails
    soft instead of hanging.
- `run_migrations` untouched (startup-only, single task).
- `busy_timeout=5000` stays but is irrelevant to this bug: one connection in
  one process, SQLite never sees the contention — the race is entirely ours.

Regression tests (the part that makes it real), three cases:
1. Two concurrent tasks, one inside `transaction()` with an `await`
   mid-block, one doing an ordinary `execute()`; assert no
   `OperationalError` and no partial write made durable.
2. The silent-commit shape: assert the concurrent autocommit `execute()`
   does not make the transaction's half-finished writes durable.
3. Nested case: `execute(commit=True)` called from inside a
   `transaction()` block completes without deadlock and does NOT commit
   the enclosing block early — the write becomes durable only when the
   block commits.
Tests 1–2 must fail before the lock lands — if they pass on unpatched
code, the test is wrong.

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
Small fraction of a slot — the lock plus the ContextVar guard is ~25
lines, plus three regression tests and the call-site audit.

## Confirm record
- **Confirmed by**: <pending>
- **#custodian seq**: 1625
- **Confirmed at**: 8/23/2026
<!-- No Confirm record → no build. This is the gate. -->

## Status
`confirmed`