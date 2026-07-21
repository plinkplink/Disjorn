"""WP-L2 tests: slash-command framework + /backlog.

Covers: pass-through of plain text and unknown /commands, /backlog filing
(verbatim) + server-rendered ack, /backlog listing, the reply being a real
persisted message authored by the seeded 'system' bot, and the GET /backlog
SDK read surface (users and bots)."""

from app import db, events
from app.routers import auth

PASSWORD = "correct horse battery staple"
BOT_KEY = "bot-key-1"


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
    r = await client.post("/auth/login", json={"username": username, "password": PASSWORD})
    assert r.status_code == 200


async def main_feed_id() -> int:
    row = await db.fetch_one("SELECT id FROM channels WHERE type = 'main_feed'")
    assert row is not None
    return row["id"]


async def post(client, channel_id: int, content: str) -> dict:
    r = await client.post(f"/channels/{channel_id}/messages", json={"content": content})
    assert r.status_code == 200, r.text
    return r.json()


async def channel_messages(client, channel_id: int) -> list[dict]:
    """Ascending, current-state (system replies included)."""
    r = await client.get(f"/channels/{channel_id}/messages", params={"from_seq": 0})
    assert r.status_code == 200, r.text
    return r.json()


# ---------------------------------------------------------------------------
# Pass-through: plain text & unknown commands are never handled
# ---------------------------------------------------------------------------

async def test_plain_text_no_dispatch(client):
    await make_user("alice")
    await login(client, "alice")
    ch = await main_feed_id()

    await post(client, ch, "hello world")
    msgs = await channel_messages(client, ch)
    assert [m["content"] for m in msgs] == ["hello world"]  # no extra reply


async def test_unknown_command_passes_through(client):
    await make_user("alice")
    await login(client, "alice")
    ch = await main_feed_id()

    payload = await post(client, ch, "/shrug ¯\\_(ツ)_/¯")
    msgs = await channel_messages(client, ch)
    # The /shrug stays as the user's plain-text message; nothing else posted.
    assert [m["content"] for m in msgs] == ["/shrug ¯\\_(ツ)_/¯"]
    assert msgs[0]["id"] == payload["id"]
    assert msgs[0]["author"]["type"] == "user"


# ---------------------------------------------------------------------------
# /backlog listing
# ---------------------------------------------------------------------------

async def test_backlog_list_empty(client):
    await make_user("alice")
    await login(client, "alice")
    ch = await main_feed_id()

    user_msg = await post(client, ch, "/backlog")
    msgs = await channel_messages(client, ch)
    assert len(msgs) == 2  # user's /backlog + system reply
    reply = msgs[1]
    assert reply["author"]["type"] == "bot"
    assert reply["author"]["name"] == "system"
    assert "empty" in reply["content"].lower()
    assert reply["seq"] == user_msg["seq"] + 1


async def test_backlog_list_shows_items(client):
    await make_user("alice")
    await login(client, "alice")
    ch = await main_feed_id()

    await post(client, ch, "/backlog add a GIF picker")
    await post(client, ch, "/backlog dark mode toggle")
    await post(client, ch, "/backlog")

    msgs = await channel_messages(client, ch)
    listing = msgs[-1]["content"]
    assert listing.startswith("Backlog (2 items, 2 open):")
    assert "#1 [open] add a GIF picker — alice" in listing
    assert "#2 [open] dark mode toggle — alice" in listing


# ---------------------------------------------------------------------------
# /backlog filing
# ---------------------------------------------------------------------------

async def test_backlog_file_creates_item_and_acks(client):
    await make_user("alice")
    await login(client, "alice")
    ch = await main_feed_id()

    events_seen: list[dict] = []
    events.subscribe(events_seen.append)

    await post(client, ch, "/backlog please add threads")

    rows = await db.fetch_all("SELECT * FROM backlog")
    assert len(rows) == 1
    assert rows[0]["text"] == "please add threads"
    assert rows[0]["author"] == "alice"
    assert rows[0]["status"] == "open"
    assert rows[0]["spec_ref"] is None

    msgs = await channel_messages(client, ch)
    ack = msgs[-1]
    assert ack["author"]["name"] == "system"
    assert "Filed backlog #1" in ack["content"]

    # The system reply is published on the bus like any other message.
    create_types = [e for e in events_seen if e["type"] == "message_create"]
    assert any(e["message"]["content"] == ack["content"] for e in create_types)


async def test_backlog_files_text_verbatim(client):
    await make_user("alice")
    await login(client, "alice")
    ch = await main_feed_id()

    # Internal spacing and punctuation must survive unchanged.
    await post(client, ch, "/backlog   Fix   the thing (URGENT!!) — see @bob")

    row = await db.fetch_one("SELECT text FROM backlog WHERE id = 1")
    assert row["text"] == "Fix   the thing (URGENT!!) — see @bob"


async def test_backlog_refuses_nl_flagged_content(client):
    # Regression (adversarial verify, Finding 1): a /backlog whose text trips
    # NL privacy detection is bot-hidden as a message, so it must NOT be copied
    # into the bot-readable backlog. Refuse at intake; nothing is filed.
    await make_user("alice")
    await login(client, "alice")
    ch = await main_feed_id()

    await post(client, ch, "/backlog off the record: the merger price is 50m")

    rows = await db.fetch_all("SELECT * FROM backlog")
    assert rows == []  # refused, not filed
    msgs = await channel_messages(client, ch)
    ack = msgs[-1]
    assert "Can't file" in ack["content"]
    assert "merger" not in ack["content"]  # refusal must not echo the secret


async def test_backlog_refuses_explicitly_flagged_message(client):
    # The other vector: text that doesn't trip NL detection but the message
    # carries explicit privacy_flags. Still bot-hidden, still refused.
    await make_user("alice")
    await login(client, "alice")
    ch = await main_feed_id()

    r = await client.post(
        f"/channels/{ch}/messages",
        json={"content": "/backlog buy the thing", "privacy_flags": {"secret": True}},
    )
    assert r.status_code == 200, r.text

    rows = await db.fetch_all("SELECT * FROM backlog")
    assert rows == []
    assert "Can't file" in (await channel_messages(client, ch))[-1]["content"]


async def test_backlog_files_when_not_flagged(client):
    # The fix must not over-refuse: an ordinary request still files.
    await make_user("alice")
    await login(client, "alice")
    ch = await main_feed_id()

    await post(client, ch, "/backlog add a gif picker to the composer")
    rows = await db.fetch_all("SELECT * FROM backlog")
    assert len(rows) == 1
    assert rows[0]["text"] == "add a gif picker to the composer"


async def test_backlog_bare_slash_no_arg_lists_not_files(client):
    await make_user("alice")
    await login(client, "alice")
    ch = await main_feed_id()

    await post(client, ch, "/backlog    ")  # only whitespace after command
    rows = await db.fetch_all("SELECT * FROM backlog")
    assert rows == []  # nothing filed
    msgs = await channel_messages(client, ch)
    assert "empty" in msgs[-1]["content"].lower()


# ---------------------------------------------------------------------------
# GET /backlog — SDK read surface
# ---------------------------------------------------------------------------

async def test_get_backlog_json(client):
    await make_user("alice")
    await login(client, "alice")
    ch = await main_feed_id()
    await post(client, ch, "/backlog first")
    await post(client, ch, "/backlog second")

    r = await client.get("/backlog")
    assert r.status_code == 200
    items = r.json()
    assert [it["text"] for it in items] == ["first", "second"]
    assert all(it["status"] == "open" and it["spec_ref"] is None for it in items)
    assert all(it["author"] == "alice" for it in items)


async def test_get_backlog_requires_auth(client):
    r = await client.get("/backlog")
    assert r.status_code == 401


async def test_get_backlog_readable_by_bot(client):
    await make_user("alice")
    await make_bot()
    await login(client, "alice")
    ch = await main_feed_id()
    await post(client, ch, "/backlog resident should see this")

    client.cookies.clear()  # force API-key auth
    r = await client.get("/backlog", headers={"X-Api-Key": BOT_KEY})
    assert r.status_code == 200
    assert r.json()[0]["text"] == "resident should see this"


async def test_bot_can_file_backlog(client):
    """Residents can file via chat too; author is the bot's name."""
    await make_bot()
    ch = await main_feed_id()
    # Bots need explicit channel membership to post.
    await db.execute(
        "INSERT INTO channel_members (channel_id, member_type, member_id) VALUES (?, 'bot', ?)",
        (ch, (await db.fetch_one("SELECT id FROM bots WHERE name = 'claw'"))["id"]),
    )
    r = await client.post(
        f"/channels/{ch}/messages",
        json={"content": "/backlog from a bot"},
        headers={"X-Api-Key": BOT_KEY},
    )
    assert r.status_code == 200
    row = await db.fetch_one("SELECT * FROM backlog WHERE id = 1")
    assert row["text"] == "from a bot"
    assert row["author"] == "claw"
