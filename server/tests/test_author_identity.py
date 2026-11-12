"""Who filed a backlog row or wrote a plan-room comment is stored as columns:
the authenticated actor, and the resident seat when a relay wrote for one."""

import sqlite3

import pytest

from app import db
from app.config import reset_settings_cache
from app.routers import auth, slash

from .test_planroom import index  # noqa: F401

PASSWORD = "correct horse battery staple"
MIGRATION = "027_author_identity.sql"
CARD = "/planroom/cards/2026-08-20-plan-room"


@pytest.fixture(autouse=True)
def fresh_state():
    slash.init()
    reset_settings_cache()
    yield
    slash.init()
    reset_settings_cache()


async def make_user(username: str, display_name: str = "") -> int:
    cur = await db.execute(
        "INSERT INTO users (username, password_hash, display_name, is_admin) "
        "VALUES (?, ?, ?, 1)",
        (username, auth.hash_password(PASSWORD), display_name or username))
    return cur.lastrowid


async def make_bot(name: str, member: bool = False) -> int:
    cur = await db.execute("INSERT INTO bots (name, api_key_hash) VALUES (?, ?)",
                           (name, auth.hash_api_key(f"{name}-key")))
    if member:
        await db.execute(
            "INSERT INTO channel_members (channel_id, member_type, member_id) "
            "VALUES (?, 'bot', ?)", (await channel(), cur.lastrowid))
    return cur.lastrowid


def key(name: str) -> dict:
    return {"X-Api-Key": f"{name}-key"}


async def login(client, username: str) -> None:
    r = await client.post("/auth/login",
                          json={"username": username, "password": PASSWORD})
    assert r.status_code == 200, r.text


async def channel(name: str = "") -> int:
    if not name:
        return (await db.fetch_one(
            "SELECT id FROM channels WHERE type = 'main_feed'"))["id"]
    cur = await db.execute(
        "INSERT INTO channels (type, name, created_at) VALUES ('text', ?, ?)",
        (name, db.utc_now()))
    return cur.lastrowid


async def say(client, content: str, **kw) -> None:
    r = await client.post(f"/channels/{await channel()}/messages",
                          json={"content": content}, **kw)
    assert r.status_code == 200, r.text


async def filer(item_id: int = 1) -> tuple:
    row = await db.fetch_one("SELECT * FROM backlog WHERE id = ?", (item_id,))
    return row["author"], row["author_type"], row["author_id"], row["author_seat"]


async def comment(client, author=None, **kw) -> dict:
    body = {"text": "reading it now"} | ({"author": author} if author else {})
    r = await client.post(f"{CARD}/comment", json=body, **kw)
    assert r.status_code == 200, r.text
    return r.json()["comment"]


async def test_a_person_filing_is_stored_as_that_user(client):
    alice = await make_user("alice")
    await login(client, "alice")
    await say(client, "/backlog add a gif picker")
    assert await filer() == ("alice", "user", alice, None)
    item = (await client.get("/backlog")).json()[0]
    assert (item["author_type"], item["author_id"], item["author_seat"]) == (
        "user", alice, None)


async def test_a_bot_filing_is_stored_as_that_bot(client):
    claw = await make_bot("claw", member=True)
    await say(client, "/backlog add a gif picker", headers=key("claw"))
    assert await filer() == ("claw", "bot", claw, None)


async def test_a_relayed_filing_stores_the_relay_and_the_seat(client):
    broker = await make_bot("broker")
    await channel("custodian")
    r = await client.post("/backlog", headers=key("broker"),
                          json={"text": "dark mode", "on_behalf_of": "res-gable"})
    assert r.status_code == 200, r.text
    assert await filer() == ("res-gable", "bot", broker, "res-gable")
    assert (r.json()["author_type"], r.json()["author_id"],
            r.json()["author_seat"]) == ("bot", broker, "res-gable")


async def test_a_same_named_bot_and_user_are_different_filers(client):
    user = await make_user("twin")
    bot = await make_bot("twin", member=True)
    await login(client, "twin")
    await say(client, "/backlog one")
    client.cookies.clear()
    await say(client, "/backlog two", headers=key("twin"))
    assert await filer(1) == ("twin", "user", user, None)
    assert await filer(2) == ("twin", "bot", bot, None)


async def test_a_rename_does_not_change_who_filed_or_who_commented(
        client, index):  # noqa: F811
    alice = await make_user("alice", "Alice")
    await login(client, "alice")
    await say(client, "/backlog add a gif picker")
    first = await comment(client)
    r = await client.patch("/me", json={"display_name": "Somebody Else"})
    assert r.status_code == 200, r.text
    assert await filer() == ("alice", "user", alice, None)
    (kept,) = (await client.get(CARD)).json()["comments"]
    assert kept == first
    assert (kept["author_type"], kept["author_id"], kept["author_label"],
            kept["author_seat"]) == ("user", alice, "Alice", None)


async def test_a_relayed_comment_stores_the_seat_beside_the_relay(
        client, index):  # noqa: F811
    broker = await make_bot("broker")
    got = await comment(client, "res-gable", headers=key("broker"))
    assert (got["author_type"], got["author_id"], got["author_seat"],
            got["author_label"]) == ("bot", broker, "res-gable",
                                     "res-gable (via broker)")
    (read,) = (await client.get(CARD, headers=key("broker"))).json()["comments"]
    assert read == got


@pytest.mark.parametrize("bot,author", [
    ("Gable", "res-claudette"),
    ("broker", None),
    ("broker", "keyboard, on a person's word"),
    ("broker", "res-" + "a" * 61),
    ("broker", "res-gable\n"),
])
async def test_only_a_relay_naming_a_seat_stores_one(client, index, bot, author):  # noqa: F811
    await make_bot(bot)
    got = await comment(client, author, headers=key(bot))
    assert got["author_seat"] is None
    assert got["author_label"] == (f"{author} (via {bot})" if author else bot)


async def test_the_plan_room_relay_list_is_config(client, index, monkeypatch):  # noqa: F811
    monkeypatch.setenv("PLANROOM_RELAY_BOT_NAMES", '["relay"]')
    reset_settings_cache()
    await make_bot("broker")
    await make_bot("relay")
    assert (await comment(client, "res-gable", headers=key("broker")))["author_seat"] is None
    assert (await comment(client, "res-gable", headers=key("relay")))["author_seat"] == "res-gable"


async def test_migration_027_maps_only_unambiguous_labels(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "old.db"))
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    reset_settings_cache()
    await db.close()
    conn = await db.connect()
    try:
        await conn.execute("""CREATE TABLE schema_migrations (
            filename TEXT PRIMARY KEY, applied_at TEXT NOT NULL)""")
        for path in sorted(db.MIGRATIONS_DIR.glob("*.sql")):
            if path.name >= MIGRATION:
                continue
            await conn.executescript(path.read_text(encoding="utf-8"))
            await conn.execute(
                "INSERT INTO schema_migrations (filename, applied_at) VALUES (?, ?)",
                (path.name, db.utc_now()))
        await conn.commit()

        plink = await make_user("plink")
        await make_user("twin")
        await make_user("res-twin")
        builder, broker, gable = [await make_bot(n) for n in ("Builder", "broker", "Gable")]
        await make_bot("twin")
        labels = ["plink", "Builder", "twin", "plink (keyboard)", "Plink",
                  "res-gable", "res-twin", "res-Gable", "res-gable (via broker)"]
        for label in labels:
            await db.execute("INSERT INTO backlog (text, author) VALUES ('t', ?)", (label,))
        comments = [("bot", broker, "res-claudette (via broker)"),
                    ("bot", gable, "res-claudette (via Gable)"),
                    ("bot", gable, "res-claudette (via broker)"),
                    ("bot", broker, "broker"),
                    ("bot", broker, "keyboard, on a word (via broker)"),
                    ("bot", broker, "res-a (via broker) (via broker)"),
                    ("user", plink, "plink")]
        for row in comments:
            await db.execute(
                "INSERT INTO card_comments (slug, author_type, author_id, "
                "author_label, text) VALUES ('backlog-1', ?, ?, ?, 't')", row)

        assert MIGRATION in await db.run_migrations()

        rows = await db.fetch_all("SELECT * FROM backlog ORDER BY id")
        assert [(r["author"], r["author_type"], r["author_id"], r["author_seat"])
                for r in rows] == [
            ("plink", "user", plink, None),
            ("Builder", "bot", builder, None),
            ("twin", None, None, None),
            ("plink (keyboard)", None, None, None),
            ("Plink", None, None, None),
            ("res-gable", None, None, "res-gable"),
            ("res-twin", None, None, None),
            ("res-Gable", None, None, None),
            ("res-gable (via broker)", None, None, None),
        ]
        rows = await db.fetch_all("SELECT * FROM card_comments ORDER BY id")
        assert [r["author_seat"] for r in rows] == [
            "res-claudette", None, None, None, None, None, None]
        assert [(r["author_type"], r["author_id"], r["author_label"])
                for r in rows] == comments

        (check,) = (await db.fetch_one("PRAGMA integrity_check")).values()
        assert check == "ok"
        for bad in ("UPDATE backlog SET author_type = 'seat' WHERE id = 1",
                    "UPDATE backlog SET author_seat = 'plink' WHERE id = 1",
                    "UPDATE card_comments SET author_seat = 'plink' WHERE id = 1"):
            with pytest.raises(sqlite3.IntegrityError):
                await db.execute(bad)
    finally:
        await db.close()
        reset_settings_cache()
