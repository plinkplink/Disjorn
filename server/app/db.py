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

Timestamps are UTC ISO-8601 strings; use `utc_now()`.
"""

import datetime
import re
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator, Sequence

import aiosqlite

from .config import get_settings

MIGRATIONS_DIR = Path(__file__).parent / "migrations"

_conn: aiosqlite.Connection | None = None


def utc_now() -> str:
    """Current UTC time as an ISO-8601 string, e.g. '2026-07-17T12:34:56.789Z'."""
    return (
        datetime.datetime.now(datetime.timezone.utc)
        .strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3]
        + "Z"
    )


async def connect(db_path: str | Path | None = None) -> aiosqlite.Connection:
    """Open (or return) the shared connection. WAL mode + foreign keys enforced."""
    global _conn
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
    return conn


async def close() -> None:
    """Close the shared connection (no-op if not open)."""
    global _conn
    if _conn is not None:
        await _conn.close()
        _conn = None


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
    """Run a statement. Commits by default; pass commit=False inside transaction()."""
    conn = get_conn()
    cur = await conn.execute(sql, params)
    if commit:
        await conn.commit()
    return cur


@asynccontextmanager
async def transaction() -> AsyncIterator[aiosqlite.Connection]:
    """BEGIN IMMEDIATE transaction; commits on success, rolls back on error.

    Use `execute(..., commit=False)` (or conn.execute) inside the block.
    Needed e.g. for per-channel seq allocation (WP4).
    """
    conn = get_conn()
    await conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        await conn.rollback()
        raise
    else:
        await conn.commit()


_MIGRATION_RE = re.compile(r"^\d+_.+\.sql$")


async def run_migrations() -> list[str]:
    """Apply migrations/*.sql in numeric filename order; each applied at most once.

    Tracks applied files in a `schema_migrations` table. Returns the list of
    filenames applied in this call.
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
