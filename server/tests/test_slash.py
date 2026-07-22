"""WP-L2 tests: slash-command framework + /backlog.

Covers: pass-through of plain text and unknown /commands, /backlog filing
(verbatim) + server-rendered ack, /backlog listing, the reply being a real
persisted message authored by the seeded 'system' bot, and the GET /backlog
SDK read surface (users and bots).

Plus the BUILD-LOOP red-team regressions:
  BL-D5 — filing from a DM is refused (a DM-filed item would be reprinted
          verbatim, with its author, by the next public `/backlog` listing).
  BL-D6 — backlog text cap, bounded in-channel listing, paginated GET /backlog,
          per-actor slash dispatch rate limit."""

import pytest

from app import db, events
from app.routers import auth, slash

PASSWORD = "correct horse battery staple"
BOT_KEY = "bot-key-1"


@pytest.fixture(autouse=True)
def reset_rate_limit():
    """The dispatch limiter is in-process module state; isolate every test."""
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


# ---------------------------------------------------------------------------
# BL-D5 — visibility scoping: no filing from DMs
# ---------------------------------------------------------------------------

async def make_dm(client, other_user_id: int) -> int:
    r = await client.post("/dms", json={"user_id": other_user_id})
    assert r.status_code == 200, r.text
    return r.json()["id"]


async def text_channel(client, name: str) -> int:
    r = await client.post("/channels", json={"name": name})
    assert r.status_code == 200, r.text
    return r.json()["id"]


SENSITIVE = "rebuild the payroll importer before bob's review on friday"


async def test_backlog_refuses_filing_from_dm(client):
    # Regression (BL-D5): merely-sensitive text — nothing that trips the
    # privacy NL detector, so the HIGH fix does not catch it — filed from a DM
    # would land in the global, public, bot-readable backlog table.
    await make_user("alice")
    bob = await make_user("bob")
    await login(client, "alice")
    dm = await make_dm(client, bob)

    await post(client, dm, f"/backlog {SENSITIVE}")

    assert await db.fetch_all("SELECT * FROM backlog") == []  # refused, not filed
    refusal = (await channel_messages(client, dm))[-1]["content"]
    assert "Can't file from a DM" in refusal
    # The refusal must never echo the content it just refused to publish.
    for word in ("payroll", "importer", "bob's review", SENSITIVE):
        assert word not in refusal


async def test_dm_filed_item_never_reaches_public_listing_or_api(client):
    # The full exfil path the finding describes: file in a DM, then list in
    # public. Nothing filed => nothing to leak, through chat OR GET /backlog.
    await make_user("alice")
    bob = await make_user("bob")
    await login(client, "alice")
    dm = await make_dm(client, bob)
    main = await main_feed_id()

    await post(client, dm, f"/backlog {SENSITIVE}")
    await post(client, main, "/backlog")  # public listing in #main

    listing = (await channel_messages(client, main))[-1]["content"]
    assert "payroll" not in listing
    assert "empty" in listing.lower()
    assert (await client.get("/backlog")).json() == []

    # And the DM message itself stays where it was said — bob's DM, not #main.
    main_contents = [m["content"] for m in await channel_messages(client, main)]
    assert not any(SENSITIVE in c for c in main_contents)


async def test_backlog_listing_still_works_in_a_dm(client):
    # Not over-refusing: reading the (public by construction) backlog inside a
    # DM leaks nothing — only FILING from a DM is refused.
    await make_user("alice")
    bob = await make_user("bob")
    await login(client, "alice")
    main = await main_feed_id()
    await post(client, main, "/backlog add a gif picker")

    dm = await make_dm(client, bob)
    await post(client, dm, "/backlog")
    listing = (await channel_messages(client, dm))[-1]["content"]
    assert "#1 [open] add a gif picker — alice" in listing


async def test_backlog_files_from_a_named_text_channel(client):
    # Not over-refusing: named text channels are house-public (every user is an
    # implicit member), so filing there is the intended path.
    await make_user("alice")
    await login(client, "alice")
    ch = await text_channel(client, "custodian")

    await post(client, ch, "/backlog dark mode toggle")
    rows = await db.fetch_all("SELECT * FROM backlog")
    assert [r["text"] for r in rows] == ["dark mode toggle"]


async def test_backlog_dm_refusal_is_not_a_privacy_refusal(client):
    """The two refusals stay distinguishable (and both stay non-echoing)."""
    await make_user("alice")
    bob = await make_user("bob")
    await login(client, "alice")
    dm = await make_dm(client, bob)

    await post(client, dm, "/backlog off the record: rebuild the importer")
    refusal = (await channel_messages(client, dm))[-1]["content"]
    # Privacy flag wins — it is the wall, checked first.
    assert "marked private" in refusal
    assert "importer" not in refusal
    assert await db.fetch_all("SELECT * FROM backlog") == []


async def test_editing_a_dm_message_into_a_command_does_not_file(client):
    """Adversarial: the edit path must not be a second door into dispatch.

    Post something innocuous in a DM, then PATCH it into `/backlog <secret>`.
    edit_message does not run slash dispatch, so nothing is filed — this test
    exists so that stays true if someone later wires dispatch into edits."""
    await make_user("alice")
    bob = await make_user("bob")
    await login(client, "alice")
    dm = await make_dm(client, bob)

    msg = await post(client, dm, "hi bob")
    r = await client.patch(f"/messages/{msg['id']}", json={"content": f"/backlog {SENSITIVE}"})
    assert r.status_code == 200

    assert await db.fetch_all("SELECT * FROM backlog") == []
    assert (await client.get("/backlog")).json() == []


async def test_bot_in_a_dm_cannot_file_from_it(client):
    """Bots can be explicit DM members; the DM refusal is not human-only."""
    bot_id = await make_bot()
    await make_user("alice")
    bob = await make_user("bob")
    await login(client, "alice")
    dm = await make_dm(client, bob)
    await db.execute(
        "INSERT INTO channel_members (channel_id, member_type, member_id) VALUES (?, 'bot', ?)",
        (dm, bot_id),
    )

    client.cookies.clear()
    r = await client.post(
        f"/channels/{dm}/messages",
        json={"content": f"/backlog {SENSITIVE}"},
        headers={"X-Api-Key": BOT_KEY},
    )
    assert r.status_code == 200
    assert await db.fetch_all("SELECT * FROM backlog") == []


async def test_unknown_channel_type_fails_closed():
    """A channel type nobody has taught the handler about counts as private."""
    from app.routers.auth import Actor

    actor = Actor(type="user", id=1)
    for channel_type in (None, "dm_1to1", "group_dm_from_the_future"):
        ctx = slash.Ctx(1, "x", actor, {}, channel_type)
        assert ctx.is_private_channel is True
    for channel_type in ("main_feed", "text"):
        ctx = slash.Ctx(1, "x", actor, {}, channel_type)
        assert ctx.is_private_channel is False


# ---------------------------------------------------------------------------
# BL-D6 — length caps, bounded listing, pagination, rate limit
# ---------------------------------------------------------------------------

async def test_backlog_refuses_oversize_text(client):
    # Regression (BL-D6): /backlog stores its argument verbatim, so without a
    # cap a single command writes an unbounded row (and an unbounded listing).
    await make_user("alice")
    await login(client, "alice")
    ch = await main_feed_id()

    huge = "z" * (slash.MAX_BACKLOG_CHARS + 1)
    await post(client, ch, f"/backlog {huge}")

    assert await db.fetch_all("SELECT * FROM backlog") == []
    refusal = (await channel_messages(client, ch))[-1]["content"]
    assert "Can't file that" in refusal and "capped at" in refusal
    assert "zzzz" not in refusal  # refusals never echo the argument


async def test_backlog_accepts_text_at_the_cap(client):
    await make_user("alice")
    await login(client, "alice")
    ch = await main_feed_id()

    at_cap = "y" * slash.MAX_BACKLOG_CHARS
    await post(client, ch, f"/backlog {at_cap}")
    rows = await db.fetch_all("SELECT text FROM backlog")
    assert len(rows) == 1 and rows[0]["text"] == at_cap


async def seed_backlog(n: int) -> None:
    for i in range(1, n + 1):
        await db.execute(
            "INSERT INTO backlog (text, author, created_at) VALUES (?, ?, ?)",
            (f"item {i}", "alice", db.utc_now()),
        )


async def test_backlog_listing_is_bounded(client):
    # An unbounded listing is the one way to manufacture a giant message:
    # server-authored replies bypass MessageCreate's max_length.
    await make_user("alice")
    await login(client, "alice")
    ch = await main_feed_id()
    await seed_backlog(slash._LIST_MAX_ITEMS + 12)

    await post(client, ch, "/backlog")
    listing = (await channel_messages(client, ch))[-1]["content"]

    assert listing.startswith(f"Backlog ({slash._LIST_MAX_ITEMS + 12} items,")
    assert f"showing the {slash._LIST_MAX_ITEMS} most recent" in listing
    assert "item 1 —" not in listing          # oldest dropped
    assert f"item {slash._LIST_MAX_ITEMS + 12} " in listing  # newest kept
    assert len(listing) < 4000


async def test_get_backlog_pagination(client):
    await make_user("alice")
    await login(client, "alice")
    await seed_backlog(120)

    # Default page size.
    first = (await client.get("/backlog")).json()
    assert len(first) == slash.BACKLOG_PAGE_DEFAULT
    assert first[0]["id"] == 1

    # Cursor: from_id is an inclusive lower bound (mirrors from_seq).
    second = (await client.get("/backlog", params={"from_id": first[-1]["id"] + 1})).json()
    assert second[0]["id"] == slash.BACKLOG_PAGE_DEFAULT + 1

    # Explicit limit, and the hard max is enforced by validation (not clamped).
    assert len((await client.get("/backlog", params={"limit": 5})).json()) == 5
    over = await client.get("/backlog", params={"limit": slash.BACKLOG_PAGE_MAX + 1})
    assert over.status_code == 422
    assert (await client.get("/backlog", params={"limit": 0})).status_code == 422
    assert (await client.get("/backlog", params={"from_id": -1})).status_code == 422


async def test_slash_dispatch_is_rate_limited(client):
    # Regression (BL-D6): dispatch had no throttle, so a loop could fill the
    # backlog table and the channel without bound.
    await make_user("alice")
    await login(client, "alice")
    ch = await main_feed_id()

    for i in range(slash.SLASH_RATE_MAX):
        await post(client, ch, f"/backlog request {i}")
    assert len(await db.fetch_all("SELECT * FROM backlog")) == slash.SLASH_RATE_MAX

    # Over the limit: the message still persists as ordinary chat, but the
    # command does not run — nothing filed.
    await post(client, ch, "/backlog one too many")
    await post(client, ch, "/backlog and another")
    rows = await db.fetch_all("SELECT text FROM backlog")
    assert len(rows) == slash.SLASH_RATE_MAX
    assert not any("too many" in r["text"] for r in rows)

    # Exactly one "slow down" notice, then silence (a refusal is itself a
    # persisted message — it must not be a free amplifier).
    contents = [m["content"] for m in await channel_messages(client, ch)]
    assert sum(1 for c in contents if c.startswith("Slow down")) == 1


async def test_slash_rate_limit_is_per_actor(client):
    await make_user("alice")
    await make_user("bob")
    ch = await main_feed_id()

    await login(client, "alice")
    for i in range(slash.SLASH_RATE_MAX + 2):
        await post(client, ch, f"/backlog alice {i}")
    assert len(await db.fetch_all("SELECT * FROM backlog")) == slash.SLASH_RATE_MAX

    client.cookies.clear()
    await login(client, "bob")
    await post(client, ch, "/backlog bob is not throttled")
    rows = await db.fetch_all("SELECT text FROM backlog")
    assert rows[-1]["text"] == "bob is not throttled"


async def test_rate_limit_does_not_block_plain_chat(client):
    """Only dispatch is limited — ordinary messages are untouched."""
    await make_user("alice")
    await login(client, "alice")
    ch = await main_feed_id()

    for i in range(slash.SLASH_RATE_MAX + 5):
        await post(client, ch, f"/backlog spam {i}")
    r = await client.post(f"/channels/{ch}/messages", json={"content": "hi everyone"})
    assert r.status_code == 200
