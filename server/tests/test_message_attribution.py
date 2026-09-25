"""Message attribution (SPECS/2026-09-23-footer-as-attribution.md): a
bot-only JSON field beside the body, capped like the other metadata, closed
to unknown keys, and present on every payload a client or bot reads."""

import sqlite3

from app import db
from tests.test_messages import (
    add_member,
    as_bot,
    capture_events,
    login,
    main_feed_id,
    make_bot,
    make_user,
)
from app.routers.messages import MAX_METADATA_CHARS

MIGRATION = "017_message_attribution.sql"
ATTRIBUTION = {"model": "claude-fable-5-1", "verified": True, "summoner": "plink"}


async def _bot_in_main(client) -> tuple[int, dict[str, str]]:
    bot_id = await make_bot("gable")
    main = await main_feed_id()
    await add_member(main, "bot", bot_id)
    return main, as_bot(client)


async def test_bot_attribution_round_trips_through_list_and_bus(client):
    main, hdrs = await _bot_in_main(client)
    captured = capture_events()

    r = await client.post(
        f"/channels/{main}/messages",
        json={"content": "the reply, and nothing after it",
              "attribution": ATTRIBUTION},
        headers=hdrs,
    )
    assert r.status_code == 200, r.text
    msg = r.json()
    assert msg["attribution"] == ATTRIBUTION
    assert msg["content"] == "the reply, and nothing after it"

    assert [e["message"]["attribution"] for e in captured] == [ATTRIBUTION]

    scrollback = (await client.get(f"/channels/{main}/messages",
                                   headers=hdrs)).json()
    assert [m["attribution"] for m in scrollback] == [ATTRIBUTION]
    backfill = (await client.get(f"/channels/{main}/messages",
                                 params={"from_seq": 1}, headers=hdrs)).json()
    assert [m["attribution"] for m in backfill] == [ATTRIBUTION]

    unverified = {"model": "claude-fable-5-1", "verified": False,
                  "summoner": None}
    r = await client.post(f"/channels/{main}/messages",
                          json={"content": "x", "attribution": unverified},
                          headers=hdrs)
    assert r.json()["attribution"] == unverified


async def test_a_bot_post_without_attribution_reads_empty(client):
    main, hdrs = await _bot_in_main(client)
    r = await client.post(f"/channels/{main}/messages", json={"content": "x"},
                          headers=hdrs)
    assert r.json()["attribution"] == {}


async def test_user_attribution_is_stored_as_empty(client):
    await make_user("alice")
    await login(client, "alice")
    main = await main_feed_id()
    r = await client.post(f"/channels/{main}/messages",
                          json={"content": "hi", "attribution": ATTRIBUTION})
    assert r.status_code == 200
    assert r.json()["attribution"] == {}
    row = await db.fetch_one("SELECT attribution FROM messages WHERE id = ?",
                             (r.json()["id"],))
    assert row["attribution"] == "{}"


async def test_oversized_attribution_is_422(client):
    main, hdrs = await _bot_in_main(client)
    r = await client.post(
        f"/channels/{main}/messages",
        json={"content": "hi",
              "attribution": {"model": "m" * (MAX_METADATA_CHARS + 10),
                              "verified": True, "summoner": "plink"}},
        headers=hdrs,
    )
    assert r.status_code == 422
    assert await db.fetch_all("SELECT * FROM messages") == []


async def test_unknown_attribution_key_is_422(client):
    main, hdrs = await _bot_in_main(client)
    r = await client.post(
        f"/channels/{main}/messages",
        json={"content": "hi", "attribution": {**ATTRIBUTION, "note": "x"}},
        headers=hdrs,
    )
    assert r.status_code == 422
    r = await client.post(
        f"/channels/{main}/messages",
        json={"content": "hi", "attribution": {**ATTRIBUTION, "verified": "yes"}},
        headers=hdrs,
    )
    assert r.status_code == 422
    assert await db.fetch_all("SELECT * FROM messages") == []


async def test_migration_017_gives_existing_rows_an_empty_attribution(
        tmp_path, monkeypatch):
    from app.config import reset_settings_cache
    from app.routers.messages import message_payload

    monkeypatch.setenv("DB_PATH", str(tmp_path / "old.db"))
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    reset_settings_cache()
    await db.close()
    conn = await db.connect()
    try:
        await conn.execute("""CREATE TABLE schema_migrations (
            filename TEXT PRIMARY KEY, applied_at TEXT NOT NULL)""")
        for path in sorted(db.MIGRATIONS_DIR.glob("*.sql")):
            if path.name == MIGRATION:
                continue
            await conn.executescript(path.read_text(encoding="utf-8"))
            await conn.execute(
                "INSERT INTO schema_migrations (filename, applied_at) VALUES (?, ?)",
                (path.name, db.utc_now()))
        await conn.commit()

        await db.execute("INSERT INTO channels (type, name) VALUES ('text', 'c')")
        await db.execute(
            "INSERT INTO messages (channel_id, seq, author_type, author_id, content)"
            " VALUES (1, 1, 'bot', 2, 'old body\n\n— gable · m · summoned by plink')")
        with_column = [r["name"] for r in await db.fetch_all(
            "PRAGMA table_info(messages)")]
        assert "attribution" not in with_column

        assert await db.run_migrations() == [MIGRATION]

        row = await db.fetch_one("SELECT * FROM messages WHERE id = 1")
        assert row["attribution"] == "{}"
        assert row["content"].endswith("summoned by plink")
        assert (await message_payload(row))["attribution"] == {}
        (check,) = (await db.fetch_one("PRAGMA integrity_check")).values()
        assert check == "ok"
        # The column refuses NULL, so no reader has to handle one.
        try:
            await db.execute("UPDATE messages SET attribution = NULL WHERE id = 1")
        except sqlite3.IntegrityError:
            pass
        else:
            raise AssertionError("attribution accepted NULL")
    finally:
        await db.close()
        reset_settings_cache()
