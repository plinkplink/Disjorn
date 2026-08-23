"""Regression tests for the db.py write lock (backlog #6).

The bug: db.py hands every request the SAME aiosqlite connection, and
`execute()` defaults to `commit=True`. Two concurrent handlers therefore
share one transaction state machine. The loud symptom was an
OperationalError after the write (whose retry filed a duplicate row); the
quiet one — the reason this file exists — is that an autocommit
`execute()` in handler B commits whatever handler A's open
`transaction()` block has written so far, with no error at all, and A's
later `rollback()` then rolls back nothing.

Durability is asserted through a SECOND connection throughout. That is
the only honest way to ask "is this committed?" — the shared connection
can see its own uncommitted work, so reading through it would prove
nothing.

Tests 1 and 2 fail on unpatched code (that is what makes them a
regression test). Test 4 is different, and deliberately so: it passes on
unpatched code, where no guard exists to be mis-inherited, and fails on
the round-1 bool-guard build. Its job is to pin the mutable-sentinel
semantics so a future refactor back to a bare bool fails loudly.
"""

import asyncio
import contextlib

import aiosqlite
import pytest

from app import db

# Long enough that the sibling's autocommit would certainly have landed on
# unpatched code; short enough to keep the suite quick. When the lock IS in
# place this elapses in full, because the sibling is correctly held off.
SIBLING_WINDOW = 0.5

# Any real deadlock (a task acquiring a lock it already holds) hangs
# forever, so every nested/child await is bounded: a broken build must
# fail the suite, not wedge it.
DEADLOCK_TIMEOUT = 5.0


@pytest.fixture
async def probe_db(tmp_db_path):
    """Shared db.py connection over a fresh file, plus a one-column table."""
    await db.close()
    conn = await db.connect(tmp_db_path)
    await conn.execute("CREATE TABLE lock_probe (v TEXT PRIMARY KEY)")
    await conn.commit()
    yield tmp_db_path
    await db.close()


async def durable(path) -> set[str]:
    """Rows visible to a SECOND connection, i.e. rows actually committed."""
    async with aiosqlite.connect(path) as other:
        async with other.execute("SELECT v FROM lock_probe") as cur:
            return {r[0] for r in await cur.fetchall()}


async def _insert(v: str, *, commit: bool = True) -> None:
    await db.execute("INSERT INTO lock_probe (v) VALUES (?)", (v,), commit=commit)


# ---------------------------------------------------------------------------
# 1. transaction() racing an ordinary execute(): no error, no partial write
# ---------------------------------------------------------------------------

async def test_concurrent_execute_does_not_break_open_transaction(probe_db):
    """A block that awaits mid-flight must not leak a partial write.

    Task A writes two rows either side of an await. Task B does an
    ordinary autocommit execute() in that window. Neither task may raise,
    and at no point may A's FIRST row be durable without its second —
    that half-written state is exactly what B's stray commit publishes on
    unpatched code.
    """
    a_open = asyncio.Event()
    b_done = asyncio.Event()
    partial: set[str] = set()

    async def in_transaction() -> None:
        async with db.transaction():
            await _insert("a1", commit=False)
            a_open.set()
            # Give B its window. Under the lock B cannot run to completion
            # here, so this times out — which is the point.
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(b_done.wait(), timeout=SIBLING_WINDOW)
            partial.update(await durable(probe_db))
            await _insert("a2", commit=False)

    async def ordinary_execute() -> None:
        await a_open.wait()
        await _insert("b1")
        b_done.set()

    results = await asyncio.gather(
        in_transaction(), ordinary_execute(), return_exceptions=True
    )
    errors = [r for r in results if isinstance(r, BaseException)]
    assert not errors, f"concurrent writers raised: {errors!r}"

    # The heart of it: mid-block, A had written a1 but not a2. Neither may
    # have been durable. On unpatched code B's commit made a1 durable.
    assert "a1" not in partial, "partial transaction was committed by a sibling"
    assert "a2" not in partial

    # And once everything settles, all three rows are durable exactly once.
    assert await durable(probe_db) == {"a1", "a2", "b1"}


# ---------------------------------------------------------------------------
# 2. The silent-commit shape (the defect this spec exists for)
# ---------------------------------------------------------------------------

async def test_autocommit_execute_does_not_commit_a_siblings_block(probe_db):
    """B's autocommit must not make A's half-finished block durable.

    No exception is involved anywhere in this test. That is the whole
    complaint: on unpatched code this corruption is completely silent.
    """
    a_open = asyncio.Event()
    b_done = asyncio.Event()
    seen_mid_block: set[str] = set()

    async def in_transaction() -> None:
        async with db.transaction():
            await _insert("secret", commit=False)
            a_open.set()
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(b_done.wait(), timeout=SIBLING_WINDOW)
            seen_mid_block.update(await durable(probe_db))

    async def ordinary_execute() -> None:
        await a_open.wait()
        await _insert("unrelated")
        b_done.set()

    await asyncio.gather(in_transaction(), ordinary_execute())

    assert "secret" not in seen_mid_block, (
        "a concurrent autocommit execute() committed another handler's "
        "open transaction"
    )
    assert await durable(probe_db) == {"secret", "unrelated"}


# ---------------------------------------------------------------------------
# 3. Nested execute(commit=True): joins the block, never deadlocks
# ---------------------------------------------------------------------------

async def test_nested_autocommit_joins_the_block_and_resets_after(probe_db):
    """A forgotten commit=False must fail soft, not hang the server.

    Pre-lock this call silently committed the enclosing block early.
    Post-lock, without the reentrancy guard, it would deadlock outright —
    a task waiting on a lock it already holds. It must instead join the
    enclosing block, and the guard must be cleared on exit so the very
    next autocommit in the same task commits for real.
    """
    async with db.transaction():
        await _insert("outer", commit=False)
        # commit=True, inside the block: the wedge case.
        await asyncio.wait_for(_insert("nested"), timeout=DEADLOCK_TIMEOUT)
        assert await durable(probe_db) == set(), (
            "nested execute(commit=True) committed the enclosing block early"
        )

    assert await durable(probe_db) == {"outer", "nested"}

    # The other half, and the one that actually proves the finally reset:
    # the same task, now outside any block, must commit normally again.
    await asyncio.wait_for(_insert("after"), timeout=DEADLOCK_TIMEOUT)
    assert await durable(probe_db) == {"outer", "nested", "after"}


# ---------------------------------------------------------------------------
# 4. A child task that outlives the block still commits on its own
# ---------------------------------------------------------------------------

async def test_child_task_outliving_the_block_commits_for_real(probe_db):
    """The sentinel test. A bool ContextVar guard fails here.

    Contexts are COPIED into child tasks, so a task created inside a
    block inherits the guard while holding no lock — and the parent's
    reset(token) runs in the parent's context and cannot reach the
    child's copy. With a plain bool the child would carry "I am inside a
    block" forever and silently decline to commit for the rest of its
    life. Because the guard is a mutable sentinel, the child inherits the
    same OBJECT, sees active=False once the parent's finally clears it,
    takes the lock, and commits for real.
    """
    release = asyncio.Event()
    child_done = asyncio.Event()

    async def child() -> None:
        # Deliberately outlives the block: it does not write until the
        # parent's transaction has fully exited.
        await release.wait()
        await _insert("from_child")
        child_done.set()

    async with db.transaction():
        await _insert("from_parent", commit=False)
        task = asyncio.create_task(child())  # inherits a COPY of the context

    release.set()
    await asyncio.wait_for(task, timeout=DEADLOCK_TIMEOUT)
    assert child_done.is_set()

    # Durable through a second connection: the child really committed,
    # rather than quietly skipping the commit on an inherited flag.
    assert await durable(probe_db) == {"from_parent", "from_child"}
