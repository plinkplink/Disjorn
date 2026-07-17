"""WP1 smoke tests: app boots, migrations apply, /healthz, main_feed seed, FTS."""

from app import db
from app.main import seed_main_feed

EXPECTED_TABLES = {
    "users",
    "sessions",
    "channels",
    "channel_members",
    "messages",
    "attachments",
    "bots",
    "push_subscriptions",
    "messages_fts",
    "schema_migrations",
}


async def test_healthz(client):
    resp = await client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}


async def test_migrations_created_all_tables(app):
    rows = await db.fetch_all(
        "SELECT name FROM sqlite_master WHERE type IN ('table') AND name NOT LIKE 'sqlite_%'"
    )
    names = {r["name"] for r in rows}
    assert EXPECTED_TABLES <= names, f"missing tables: {EXPECTED_TABLES - names}"
    # WAL mode is on
    row = await db.fetch_one("PRAGMA journal_mode")
    assert row["journal_mode"] == "wal"


async def test_main_feed_seeded_and_idempotent(app):
    rows = await db.fetch_all("SELECT * FROM channels WHERE type = 'main_feed'")
    assert len(rows) == 1
    assert rows[0]["name"] == "main"

    # Re-running the seed (as a lifespan restart would) must not duplicate it.
    await seed_main_feed()
    await db.run_migrations()  # already-applied migrations are skipped
    rows = await db.fetch_all("SELECT * FROM channels WHERE type = 'main_feed'")
    assert len(rows) == 1


async def test_fts_triggers_maintain_index(app):
    chan = await db.fetch_one("SELECT id FROM channels WHERE type = 'main_feed'")
    await db.execute(
        """INSERT INTO messages (channel_id, seq, author_type, author_id, content)
           VALUES (?, 1, 'user', 1, 'hello searchable world')""",
        (chan["id"],),
    )
    hits = await db.fetch_all(
        "SELECT rowid FROM messages_fts WHERE messages_fts MATCH 'searchable'"
    )
    assert len(hits) == 1

    # Edit is reflected
    await db.execute(
        "UPDATE messages SET content = 'goodbye planet' WHERE id = ?", (hits[0]["rowid"],)
    )
    assert await db.fetch_all(
        "SELECT rowid FROM messages_fts WHERE messages_fts MATCH 'searchable'"
    ) == []
    assert len(
        await db.fetch_all("SELECT rowid FROM messages_fts WHERE messages_fts MATCH 'planet'")
    ) == 1

    # Hard delete removes from index (soft delete filtering is query-side, WP4)
    await db.execute("DELETE FROM messages WHERE id = ?", (hits[0]["rowid"],))
    assert await db.fetch_all(
        "SELECT rowid FROM messages_fts WHERE messages_fts MATCH 'planet'"
    ) == []


async def test_seq_unique_per_channel(app):
    chan = await db.fetch_one("SELECT id FROM channels WHERE type = 'main_feed'")
    await db.execute(
        "INSERT INTO messages (channel_id, seq, author_type, author_id, content) "
        "VALUES (?, 10, 'user', 1, 'a')",
        (chan["id"],),
    )
    import sqlite3

    import pytest

    with pytest.raises(sqlite3.IntegrityError):
        await db.execute(
            "INSERT INTO messages (channel_id, seq, author_type, author_id, content) "
            "VALUES (?, 10, 'user', 2, 'b')",
            (chan["id"],),
        )
