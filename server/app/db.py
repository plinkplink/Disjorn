"""SQLite access layer: single shared aiosqlite connection + helpers + migrations.

No ORM — hand-written SQL through these helpers (project convention).

Usage:
    await connect()                # opens the connection (WAL, foreign_keys ON)
    row  = await fetch_one("SELECT * FROM users WHERE id = ?", (1,))
    rows = await fetch_all("SELECT * FROM channels")
    cur  = await execute("INSERT INTO ... VALUES (?)", (x,))   # auto-commits
    async with transaction():      # BEGIN IMMEDIATE ... COMMIT/ROLLBACK
        await execute(..., commit=False)
    await run_migrations()
    await close()

Writes are serialized by a process-global lock, because the connection above is
shared by every request and so is its transaction state. See transaction() for
what that costs you (it is held across your awaits) and execute() for the one
path it does not cover.

Timestamps are UTC ISO-8601 strings; use `utc_now()`.
"""

import asyncio
import contextvars
import datetime
import re
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator, Sequence

import aiosqlite

from .config import get_settings

MIGRATIONS_DIR = Path(__file__).parent / "migrations"

_conn: aiosqlite.Connection | None = None

# Every request in this process shares the one connection above, so it also
# shares one SQLite transaction state machine. Without this lock, an
# autocommit execute() in handler B commits whatever handler A's open
# transaction() block has written so far — silently, and A's later rollback()
# then rolls back nothing. Built in connect(), dropped in close().
_write_lock: asyncio.Lock | None = None


class _WriteBlock:
    """Mutable marker for 'a transaction() block is live in this context'.

    Deliberately an object with a mutable field rather than a plain bool.
    Contexts are *copied* into child tasks, so a task spawned inside a block
    inherits whatever the ContextVar held at creation time while holding no
    lock at all — and the parent's `reset(token)` runs in the parent's own
    context and can never reach the child's copy. A bare bool would therefore
    strand a long-lived child at True forever, and every later
    execute(commit=True) in that task would silently decline to commit.

    Because children inherit the *reference*, clearing `active` here is
    visible in every inherited copy at once.

    The general shape, for the next reader: a ContextVar bool answers "was I
    created inside a block", never "do I hold the lock". Those are the same
    question only until someone spawns a task.
    """

    __slots__ = ("active",)

    def __init__(self) -> None:
        self.active = True


_write_block: contextvars.ContextVar[_WriteBlock | None] = contextvars.ContextVar(
    "db_write_block", default=None
)


def _get_write_lock() -> asyncio.Lock:
    """Return the write lock; raises if connect() was never called."""
    if _write_lock is None:
        raise RuntimeError("Database not connected — call db.connect() first (app lifespan does this)")
    return _write_lock


def utc_now() -> str:
    """Current UTC time as an ISO-8601 string, e.g. '2026-07-17T12:34:56.789Z'."""
    return (
        datetime.datetime.now(datetime.timezone.utc)
        .strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3]
        + "Z"
    )


async def connect(db_path: str | Path | None = None) -> aiosqlite.Connection:
    """Open (or return) the shared connection. WAL mode + foreign keys enforced."""
    global _conn, _write_lock
    if _conn is not None:
        return _conn
    path = Path(db_path) if db_path is not None else get_settings().db_path
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = await aiosqlite.connect(path)
    conn.row_factory = aiosqlite.Row
    await conn.execute("PRAGMA journal_mode=WAL")
    await conn.execute("PRAGMA foreign_keys=ON")
    # Irrelevant to the write lock: one connection in one process means SQLite
    # never sees the contention. The race this file guards against is ours.
    await conn.execute("PRAGMA busy_timeout=5000")
    _conn = conn
    _write_lock = asyncio.Lock()
    return conn


async def close() -> None:
    """Close the shared connection (no-op if not open).

    Drops the write lock too, because an asyncio.Lock binds to the event loop
    it is first awaited on: a lock carried across a close/connect pair (as the
    tests do, one loop per test) would belong to a dead loop. connect() builds
    a fresh one.
    """
    global _conn, _write_lock
    if _conn is not None:
        await _conn.close()
        _conn = None
    _write_lock = None


def get_conn() -> aiosqlite.Connection:
    """Return the open shared connection; raises if connect() was never called."""
    if _conn is None:
        raise RuntimeError("Database not connected — call db.connect() first (app lifespan does this)")
    return _conn


async def fetch_one(sql: str, params: Sequence[Any] = ()) -> dict[str, Any] | None:
    """Run a query and return the first row as a dict, or None."""
    async with get_conn().execute(sql, params) as cur:
        row = await cur.fetchone()
    return dict(row) if row is not None else None


async def fetch_all(sql: str, params: Sequence[Any] = ()) -> list[dict[str, Any]]:
    """Run a query and return all rows as dicts."""
    async with get_conn().execute(sql, params) as cur:
        rows = await cur.fetchall()
    return [dict(r) for r in rows]


async def execute(sql: str, params: Sequence[Any] = (), *, commit: bool = True) -> aiosqlite.Cursor:
    """Run a statement. Commits by default; pass commit=False inside transaction().

    commit=True takes the process-wide write lock around the execute-and-commit
    pair, so the commit can only ever publish this statement — never a
    concurrent transaction() block's half-finished work.

    commit=False does NOT take the lock: it is meant to be called from inside a
    block that already holds it, and asyncio.Lock is not reentrant, so acquiring
    here would deadlock the server on the very first transaction. The cost is
    that `execute(commit=False)` called *outside* any transaction() is the one
    write path this lock does not cover — it interleaves freely with everything
    else. There are no such call sites today; adding one means opting out of the
    guarantee above.

    Calling this with commit=True from inside a live transaction() block (i.e.
    forgetting commit=False) joins the enclosing block instead: no lock is
    taken, and no commit is issued. Both are load-bearing. Committing would
    publish the enclosing block early — the original defect — and acquiring a
    lock this task already holds would hang the whole server. Note the
    consequence, which is intended: such a write is now rolled back with the
    enclosing block if it fails, where before it made itself durable on its own.
    That is what these call sites always meant.
    """
    conn = get_conn()
    if not commit:
        return await conn.execute(sql, params)

    block = _write_block.get()
    if block is not None and block.active:
        # Inside a live block in THIS context: join it, don't commit it.
        return await conn.execute(sql, params)

    async with _get_write_lock():
        cur = await conn.execute(sql, params)
        await conn.commit()
    return cur


@asynccontextmanager
async def transaction() -> AsyncIterator[aiosqlite.Connection]:
    """BEGIN IMMEDIATE transaction; commits on success, rolls back on error.

    Use `execute(..., commit=False)` (or conn.execute) inside the block.
    Needed e.g. for per-channel seq allocation (WP4).

    Holds a process-global write lock for the whole block, because every
    request shares one connection and therefore one transaction state machine.
    That lock spans every await the caller makes inside the block, not just the
    database calls — so slow work in here (an HTTP request, an unfurl, anything
    on the network) stalls every other writer on the server for its duration.
    Do that work before the block and pass the result in.
    """
    conn = get_conn()
    async with _get_write_lock():
        block = _WriteBlock()
        token = _write_block.set(block)
        try:
            await conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except BaseException:
                await conn.rollback()
                raise
            else:
                await conn.commit()
        finally:
            # Both, and in this order. Clearing `active` is what reaches tasks
            # that copied this context (reset() cannot — it only touches ours);
            # reset() is what keeps this context itself clean.
            block.active = False
            _write_block.reset(token)


_MIGRATION_RE = re.compile(r"^\d+_.+\.sql$")


async def run_migrations() -> list[str]:
    """Apply migrations/*.sql in numeric filename order; each applied at most once.

    Tracks applied files in a `schema_migrations` table. Returns the list of
    filenames applied in this call.

    Deliberately exempt from the write lock: this runs once at startup, in a
    single task, before anything is serving. There is nothing to race. The
    exemption is stated here so a later reader finds a decision rather than an
    oversight — it commits directly on the connection, around the lock.
    """
    conn = get_conn()
    await conn.execute(
        """CREATE TABLE IF NOT EXISTS schema_migrations (
               filename TEXT PRIMARY KEY,
               applied_at TEXT NOT NULL
           )"""
    )
    await conn.commit()

    applied = {
        r["filename"]
        for r in await fetch_all("SELECT filename FROM schema_migrations")
    }
    newly_applied: list[str] = []
    for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
        if not _MIGRATION_RE.match(path.name):
            continue
        if path.name in applied:
            continue
        await conn.executescript(path.read_text(encoding="utf-8"))
        await conn.execute(
            "INSERT INTO schema_migrations (filename, applied_at) VALUES (?, ?)",
            (path.name, utc_now()),
        )
        await conn.commit()
        newly_applied.append(path.name)
    return newly_applied
