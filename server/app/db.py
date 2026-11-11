"""Shared aiosqlite connection; writes are serialized by a process-wide lock."""

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

# Without it, one request's autocommit commits another request's open transaction().
_write_lock: asyncio.Lock | None = None


class _WriteBlock:
    """Mutable: child tasks copy the ContextVar, and a bool would stay True there."""

    __slots__ = ("active",)

    def __init__(self) -> None:
        self.active = True


_write_block: contextvars.ContextVar[_WriteBlock | None] = contextvars.ContextVar(
    "db_write_block", default=None
)


def _get_write_lock() -> asyncio.Lock:
    """Return the write lock; raises if not connected."""
    if _write_lock is None:
        raise RuntimeError("Database not connected — call db.connect() first (app lifespan does this)")
    return _write_lock


def utc_now() -> str:
    """Current UTC time as an ISO-8601 string."""
    return (
        datetime.datetime.now(datetime.timezone.utc)
        .strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3]
        + "Z"
    )


async def connect(db_path: str | Path | None = None) -> aiosqlite.Connection:
    """Open or return the shared connection."""
    global _conn, _write_lock
    if _conn is not None:
        return _conn
    path = Path(db_path) if db_path is not None else get_settings().db_path
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = await aiosqlite.connect(path)
    conn.row_factory = aiosqlite.Row
    await conn.execute("PRAGMA journal_mode=WAL")
    await conn.execute("PRAGMA foreign_keys=ON")
    await conn.execute("PRAGMA busy_timeout=5000")
    _conn = conn
    _write_lock = asyncio.Lock()
    return conn


async def close() -> None:
    """Close the shared connection and drop the write lock (it binds to its first loop)."""
    global _conn, _write_lock
    if _conn is not None:
        await _conn.close()
        _conn = None
    _write_lock = None


def get_conn() -> aiosqlite.Connection:
    """Return the open connection; raises if connect() was never called."""
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
    """Run a statement, committing unless commit=False.

    commit=False takes no lock (asyncio.Lock is not reentrant), so outside
    transaction() it bypasses the lock. commit=True inside transaction() joins
    that block: committing there would publish it early, and re-locking deadlocks.
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
    """BEGIN IMMEDIATE transaction: commit on success, roll back on error.

    Holds the write lock across every await in the block, so do slow work first.
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
            # Clear `active` for copied contexts; reset() cleans this one.
            block.active = False
            _write_block.reset(token)


_MIGRATION_RE = re.compile(r"^\d+_.+\.sql$")


async def run_migrations() -> list[str]:
    """Apply pending migrations/*.sql in filename order.

    Runs at startup, before serving, unlocked.
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
