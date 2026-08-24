"""Plan Room Phase II slice A — the /backlog lifecycle verbs.

SPECS/2026-08-23-plan-room-phase2-slice-a.md (confirmed by plink, #custodian
seq 1625).

Four claims are on trial:

1. **The verbs write the row, with a signature.** `reject`/`duplicate`/`built`/
   `spec'd` set the status and record who and when as TYPED columns, not prose.
2. **Bots are refused at the guard, server-side.** Not hidden from — refused.
3. **A reserved word never silently files.** `/backlog reject the login flow`
   changes nothing and files nothing, and says which word was reserved. The
   failure this prevents is the lossy one: a person believing #5 is rejected
   while the table holds a new item reading "reject 5 please".
4. **`duplicate` is writable and re-writable.** The row-3 case: a row marked
   `rejected` for want of a better word can be corrected.

Plus the migration round-trip: rows filed under the 006 schema survive the
010 table rebuild with their ids, and with NULL attribution rather than an
invented one.
"""

import pytest

from app import db
from app.routers import auth, slash
from app.services import backlog as backlog_service

PASSWORD = "correct horse battery staple"
BOT_KEY = "bot-key-triage"
SPEC_SLUG = "2026-08-23-db-write-lock"


@pytest.fixture(autouse=True)
def reset_rate_limit():
    slash.init()
    yield
    slash.init()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def make_user(username: str) -> int:
    cur = await db.execute(
        "INSERT INTO users (username, password_hash, display_name) VALUES (?, ?, ?)",
        (username, auth.hash_password(PASSWORD), username.capitalize()),
    )
    return cur.lastrowid


async def make_bot(name: str = "claw", api_key: str = BOT_KEY) -> int:
    cur = await db.execute(
        "INSERT INTO bots (name, api_key_hash) VALUES (?, ?)",
        (name, auth.hash_api_key(api_key)),
    )
    return cur.lastrowid


async def login(client, username: str) -> None:
    r = await client.post("/auth/login",
                          json={"username": username, "password": PASSWORD})
    assert r.status_code == 200


async def main_feed_id() -> int:
    row = await db.fetch_one("SELECT id FROM channels WHERE type = 'main_feed'")
    assert row is not None
    return row["id"]


async def post(client, channel_id: int, content: str, **kw) -> dict:
    r = await client.post(f"/channels/{channel_id}/messages",
                          json={"content": content}, **kw)
    assert r.status_code == 200, r.text
    return r.json()


async def last_reply(client, channel_id: int, **kw) -> str:
    r = await client.get(f"/channels/{channel_id}/messages",
                         params={"from_seq": 0}, **kw)
    assert r.status_code == 200, r.text
    return r.json()[-1]["content"]


async def row(item_id: int = 1) -> dict:
    got = await db.fetch_one("SELECT * FROM backlog WHERE id = ?", (item_id,))
    assert got is not None
    return got


@pytest.fixture
async def filed(client):
    """One signed-in human ('alice') and one open backlog item, #1."""
    await make_user("alice")
    await login(client, "alice")
    ch = await main_feed_id()
    await post(client, ch, "/backlog add a gif picker")
    assert (await row())["status"] == "open"
    return ch


# ---------------------------------------------------------------------------
# The verbs write the row
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("verb,status", [
    ("reject", "rejected"),
    ("duplicate", "duplicate"),
    ("built", "built"),
])
async def test_verb_sets_status(client, filed, verb, status):
    alice = (await db.fetch_one("SELECT id FROM users WHERE username = 'alice'"))["id"]

    await post(client, filed, f"/backlog {verb} 1")

    got = await row()
    assert got["status"] == status
    # Attribution is DATA, not prose: the same shape messages.author_* has.
    assert got["status_by_type"] == "user"
    assert got["status_by_id"] == alice
    assert got["status_at"] is not None
    # `author` stays what it always was — the filer, frozen at filing time.
    assert got["author"] == "alice"

    reply = await last_reply(client, filed)
    assert f"Backlog #1 → {status}" in reply
    assert "by alice" in reply
    # The command edits the artifact and says the machinery will follow — it
    # never claims to have moved a card it cannot see.
    assert "derivation tick" in reply


async def test_specd_sets_spec_ref_in_the_same_write(client, filed):
    await post(client, filed, f"/backlog spec'd 1 {SPEC_SLUG}")

    got = await row()
    assert got["status"] == "spec'd"
    assert got["spec_ref"] == SPEC_SLUG      # the slug, and only the slug
    assert got["status_by_type"] == "user"
    assert SPEC_SLUG in await last_reply(client, filed)


async def test_specd_without_a_slug_writes_nothing(client, filed):
    """A status implying a pointer must not ship without the pointer."""
    await post(client, filed, "/backlog spec'd 1")

    got = await row()
    assert got["status"] == "open" and got["spec_ref"] is None
    reply = await last_reply(client, filed)
    assert "needs the spec slug" in reply
    assert "/backlog spec'd <id>" in reply


async def test_specd_refuses_a_path(client, filed):
    """`spec_ref` is a slug. A path is wrong the day SPECS/ is reorganised."""
    await post(client, filed, f"/backlog spec'd 1 SPECS/{SPEC_SLUG}.md")

    got = await row()
    assert got["status"] == "open" and got["spec_ref"] is None
    assert "is a path, not a slug" in await last_reply(client, filed)


async def test_specd_refuses_a_non_slug(client, filed):
    await post(client, filed, "/backlog spec'd 1 the-write-lock-one")
    assert (await row())["status"] == "open"
    assert "is not a spec slug" in await last_reply(client, filed)


async def test_specd_slug_with_spaces_is_refused(client, filed):
    await post(client, filed, f"/backlog spec'd 1 {SPEC_SLUG} and also this")
    assert (await row())["status"] == "open"
    assert "one word" in await last_reply(client, filed)


async def test_plain_verb_refuses_a_trailing_slug(client, filed):
    """Only `spec'd` takes a second argument; the others say so."""
    await post(client, filed, f"/backlog reject 1 {SPEC_SLUG}")
    assert (await row())["status"] == "open"
    assert "takes only an item id" in await last_reply(client, filed)


async def test_curly_apostrophe_still_reaches_the_verb(client, filed):
    """A phone keyboard writes `spec’d`. It must not file an item instead."""
    await post(client, filed, f"/backlog spec’d 1 {SPEC_SLUG}")

    got = await row()
    assert got["status"] == "spec'd" and got["spec_ref"] == SPEC_SLUG
    assert len(await db.fetch_all("SELECT id FROM backlog")) == 1  # nothing filed


async def test_unknown_id_is_refused_not_created(client, filed):
    await post(client, filed, "/backlog reject 99")
    assert [r["id"] for r in await db.fetch_all("SELECT id FROM backlog")] == [1]
    assert "no backlog #99" in await last_reply(client, filed)


# ---------------------------------------------------------------------------
# The guard: bots are refused server-side
# ---------------------------------------------------------------------------

async def test_bot_is_refused_at_the_guard(client, filed):
    """A resident may file and read; ruling on a request is a human act."""
    bot_id = await make_bot()
    await db.execute(
        "INSERT INTO channel_members (channel_id, member_type, member_id) "
        "VALUES (?, 'bot', ?)", (filed, bot_id))

    client.cookies.clear()  # force API-key auth
    key = {"X-Api-Key": BOT_KEY}
    await post(client, filed, "/backlog reject 1", headers=key)

    got = await row()
    assert got["status"] == "open"          # refused, not applied
    assert got["status_by_type"] is None    # and nothing attributed
    # Refused out loud, in the channel, where the bot's caller can read it.
    assert "Only a signed-in person" in await last_reply(client, filed, headers=key)


async def test_bot_refusal_lives_in_the_write_path_not_the_router(client, filed):
    """The gate is inside set_status, so no caller can route around it."""
    with pytest.raises(backlog_service.BacklogStatusError) as exc:
        await backlog_service.set_status(
            1, "rejected", actor_type="bot", actor_id=1)
    assert exc.value.status_code == 403
    assert (await row())["status"] == "open"


async def test_service_refuses_a_status_with_no_verb(client, filed):
    """`open` has no verb, so this path cannot un-triage a row."""
    with pytest.raises(backlog_service.BacklogStatusError):
        await backlog_service.set_status(1, "open", actor_type="user", actor_id=1)
    assert (await row())["status"] == "open"


# ---------------------------------------------------------------------------
# Reserved words: no silent wrong turn
# ---------------------------------------------------------------------------

async def test_reserved_word_never_silently_files(client, filed):
    await post(client, filed, "/backlog reject the login flow, it is awful")

    # Nothing filed, nothing changed, and the reply says which word did it.
    assert [r["id"] for r in await db.fetch_all("SELECT id FROM backlog")] == [1]
    assert (await row())["status"] == "open"
    reply = await last_reply(client, filed)
    assert "reserved" in reply and "`reject`" in reply
    assert "reword it" in reply


async def test_typo_after_the_verb_does_not_become_a_filed_item(client, filed):
    """`/backlog reject 5 please` must not file "reject 5 please"."""
    await post(client, filed, "/backlog reject 5 please")
    texts = [r["text"] for r in await db.fetch_all("SELECT text FROM backlog")]
    assert texts == ["add a gif picker"]
    assert (await row())["status"] == "open"


async def test_verb_lookalikes_still_file_normally(client, filed):
    """Not over-reserving: only the exact first word is a verb."""
    await post(client, filed, "/backlog rejection emails should be nicer")
    await post(client, filed, "/backlog built-in emoji picker")  # 'built-in' != 'built'
    texts = [r["text"] for r in await db.fetch_all("SELECT text FROM backlog ORDER BY id")]
    assert texts == ["add a gif picker",
                     "rejection emails should be nicer",
                     "built-in emoji picker"]


# ---------------------------------------------------------------------------
# `duplicate` exists so a wrong answer can be corrected (the row-3 case)
# ---------------------------------------------------------------------------

async def test_rejected_row_can_be_corrected_to_duplicate(client, filed):
    """Backlog row 3 took `rejected` on 08-23 for want of `duplicate`.

    A triage table you cannot correct accumulates wrong answers, so any of the
    four may replace any other — with the corrector's name on it."""
    await post(client, filed, "/backlog reject 1")
    assert (await row())["status"] == "rejected"
    first_at = (await row())["status_at"]

    await post(client, filed, "/backlog duplicate 1")
    got = await row()
    assert got["status"] == "duplicate"
    assert got["status_at"] >= first_at


async def test_later_verb_keeps_the_spec_ref_it_was_given(client, filed):
    """Rejecting a spec'd row must not blank the only pointer back to the spec."""
    await post(client, filed, f"/backlog spec'd 1 {SPEC_SLUG}")
    await post(client, filed, "/backlog reject 1")

    got = await row()
    assert got["status"] == "rejected"
    assert got["spec_ref"] == SPEC_SLUG


async def test_duplicate_is_allowed_by_the_check_constraint(client, filed):
    """The CHECK widened; migration 010's whole reason for being a rebuild."""
    await db.execute("UPDATE backlog SET status = 'duplicate' WHERE id = 1")
    assert (await row())["status"] == "duplicate"


async def test_check_constraint_still_refuses_nonsense(client, filed):
    import sqlite3
    with pytest.raises(sqlite3.IntegrityError):
        await db.execute("UPDATE backlog SET status = 'vibes' WHERE id = 1")


async def test_status_by_type_is_constrained(client, filed):
    import sqlite3
    with pytest.raises(sqlite3.IntegrityError):
        await db.execute(
            "UPDATE backlog SET status_by_type = 'ghost' WHERE id = 1")


# ---------------------------------------------------------------------------
# The read surfaces carry the new fields
# ---------------------------------------------------------------------------

async def test_get_backlog_exposes_status_and_attribution(client, filed):
    alice = (await db.fetch_one("SELECT id FROM users WHERE username = 'alice'"))["id"]
    await post(client, filed, f"/backlog spec'd 1 {SPEC_SLUG}")

    item = (await client.get("/backlog")).json()[0]
    assert item["status"] == "spec'd"
    assert item["spec_ref"] == SPEC_SLUG
    assert item["status_by_type"] == "user"
    assert item["status_by_id"] == alice
    assert item["status_at"] is not None


async def test_get_backlog_is_null_on_an_untriaged_row(client, filed):
    """The truthful answer for a row nobody has ruled on, not an invented one."""
    item = (await client.get("/backlog")).json()[0]
    assert (item["status"], item["status_by_type"], item["status_by_id"],
            item["status_at"]) == ("open", None, None, None)


async def test_chat_listing_shows_the_new_status(client, filed):
    await post(client, filed, "/backlog duplicate 1")
    await post(client, filed, "/backlog")
    listing = await last_reply(client, filed)
    assert "#1 [duplicate] add a gif picker — alice" in listing
    assert "1 item, 0 open" in listing


# ---------------------------------------------------------------------------
# Migration round-trip: the rebuild moves every row, so prove it moved them
# ---------------------------------------------------------------------------

MIGRATION = "010_backlog_status_verbs.sql"


async def test_migration_010_preserves_rows_filed_under_006(tmp_path, monkeypatch):
    """Build a 006-era database, run the rebuild, check every row came across.

    The migration is create-copy-drop-rename — the only way SQLite widens a
    CHECK — so unlike every additive migration in this directory it moves data,
    and "the rows are still there, with their ids" is a claim that has to be
    tested rather than assumed. Ids especially: the Plan Room cards a row as
    slug `backlog-<id>` and the reject button posts to that slug, so a shifted
    id is a button pointed at the wrong request.
    """
    from app.config import reset_settings_cache

    monkeypatch.setenv("DB_PATH", str(tmp_path / "old.db"))
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    reset_settings_cache()
    await db.close()
    conn = await db.connect()
    try:
        # Everything up to and including 009 — the schema as it stood before.
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

        for text, status in [("add a gif picker", "open"),
                             ("dark mode", "spec'd"),
                             ("filed twice by mistake", "rejected")]:
            await db.execute(
                "INSERT INTO backlog (text, author, created_at, status) "
                "VALUES (?, ?, ?, ?)", (text, "alice", db.utc_now(), status))
        await db.execute("UPDATE backlog SET spec_ref = ? WHERE id = 2",
                         (SPEC_SLUG,))
        before = await db.fetch_all("SELECT * FROM backlog ORDER BY id")
        assert [r["id"] for r in before] == [1, 2, 3]

        # 'duplicate' is not yet allowed — this is the constraint being widened.
        import sqlite3
        with pytest.raises(sqlite3.IntegrityError):
            await db.execute("UPDATE backlog SET status = 'duplicate' WHERE id = 3")

        assert await db.run_migrations() == [MIGRATION]

        after = await db.fetch_all("SELECT * FROM backlog ORDER BY id")
        assert [(r["id"], r["text"], r["author"], r["created_at"], r["status"],
                 r["spec_ref"]) for r in after] == \
               [(r["id"], r["text"], r["author"], r["created_at"], r["status"],
                 r["spec_ref"]) for r in before]
        # Backfilled NULL, not invented: nobody recorded who triaged these.
        assert all(r["status_by_type"] is None and r["status_by_id"] is None
                   and r["status_at"] is None for r in after)

        # The point of the rebuild: row 3 can now say what it actually is.
        await db.execute("UPDATE backlog SET status = 'duplicate' WHERE id = 3")
        assert (await row(3))["status"] == "duplicate"

        # AUTOINCREMENT survived the rename — the next filing continues the
        # series rather than reusing an id a card slug already points at.
        cur = await db.execute(
            "INSERT INTO backlog (text, author, created_at) VALUES (?, ?, ?)",
            ("filed after the migration", "alice", db.utc_now()))
        assert cur.lastrowid == 4
    finally:
        await db.close()
        reset_settings_cache()
