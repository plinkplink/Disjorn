"""WP3 tests: channel listing/unread math, DM idempotency, read state, members, bot membership."""

from app import db
from app.routers import auth, channels

PASSWORD = "correct horse battery staple"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def make_user(username: str, display_name: str | None = None) -> int:
    cur = await db.execute(
        "INSERT INTO users (username, password_hash, display_name) VALUES (?, ?, ?)",
        (username, auth.hash_password(PASSWORD), display_name or username.capitalize()),
    )
    return cur.lastrowid


async def make_bot(name: str = "claw", api_key: str = "bot-key-1") -> int:
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


async def insert_message(
    channel_id: int,
    seq: int,
    content: str,
    author_id: int = 1,
    author_type: str = "user",
    deleted: bool = False,
) -> int:
    cur = await db.execute(
        """INSERT INTO messages (channel_id, seq, author_type, author_id, content, deleted_at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (channel_id, seq, author_type, author_id, content, db.utc_now() if deleted else None),
    )
    return cur.lastrowid


async def member_row(channel_id: int, member_type: str, member_id: int):
    return await db.fetch_one(
        """SELECT * FROM channel_members
           WHERE channel_id = ? AND member_type = ? AND member_id = ?""",
        (channel_id, member_type, member_id),
    )


# ---------------------------------------------------------------------------
# GET /channels — listing + unread math
# ---------------------------------------------------------------------------

async def test_list_requires_auth(client):
    assert (await client.get("/channels")).status_code == 401


async def test_list_main_feed_always_present_with_unread_math(client):
    uid = await make_user("alice")
    await login(client, "alice")
    main = await main_feed_id()

    r = await client.get("/channels")
    assert r.status_code == 200
    [item] = r.json()
    assert item["id"] == main and item["type"] == "main_feed"
    assert item["unread"] == 0 and item["last_message"] is None

    for seq in range(1, 6):
        await insert_message(main, seq, f"msg {seq}", author_id=uid)

    [item] = (await client.get("/channels")).json()
    assert item["unread"] == 5  # no read state yet -> floor is 0
    assert item["last_message"]["seq"] == 5
    assert item["last_message"]["snippet"] == "msg 5"

    await client.put(f"/channels/{main}/read", json={"seq": 3})
    [item] = (await client.get("/channels")).json()
    assert item["unread"] == 2

    # last_read above max seq -> unread floored at 0, never negative
    await client.put(f"/channels/{main}/read", json={"seq": 99})
    [item] = (await client.get("/channels")).json()
    assert item["unread"] == 0


async def test_list_dm_shows_other_participant_and_skips_deleted_snippet(client):
    uid_a = await make_user("alice")
    uid_b = await make_user("bob", display_name="Bobby")
    await login(client, "alice")
    dm_id = (await client.post("/dms", json={"user_id": uid_b})).json()["id"]

    await insert_message(dm_id, 1, "hello bob", author_id=uid_a)
    await insert_message(dm_id, 2, "oops deleted", author_id=uid_a, deleted=True)

    items = (await client.get("/channels")).json()
    assert [i["type"] for i in items] == ["main_feed", "dm_1to1"]
    dm = items[1]
    assert dm["name"] == "Bobby" and dm["dm_user_id"] == uid_b
    assert dm["unread"] == 2  # max(seq)=2 (deleted still counts) minus last_read_seq=0
    # deleted message still counts toward max seq but not the snippet
    assert dm["last_message"]["snippet"] == "hello bob"

    # Bob sees Alice's name on the same channel
    await login(client, "bob")
    items = (await client.get("/channels")).json()
    dm = next(i for i in items if i["type"] == "dm_1to1")
    assert dm["name"] == "Alice" and dm["dm_user_id"] == uid_a


async def test_list_unread_counts_deleted_toward_max_seq(client):
    await make_user("alice")
    uid_b = await make_user("bob")
    await login(client, "alice")
    dm_id = (await client.post("/dms", json={"user_id": uid_b})).json()["id"]
    await insert_message(dm_id, 1, "one", author_id=uid_b)
    await insert_message(dm_id, 2, "two", author_id=uid_b, deleted=True)
    dm = next(i for i in (await client.get("/channels")).json() if i["id"] == dm_id)
    assert dm["unread"] == 2  # max(seq)=2 minus last_read_seq=0


async def test_list_excludes_other_peoples_dms(client):
    await make_user("alice")
    uid_b = await make_user("bob")
    await make_user("carol")
    await login(client, "alice")
    await client.post("/dms", json={"user_id": uid_b})

    await login(client, "carol")
    items = (await client.get("/channels")).json()
    assert [i["type"] for i in items] == ["main_feed"]


# ---------------------------------------------------------------------------
# POST /dms — idempotent get-or-create
# ---------------------------------------------------------------------------

async def test_dm_idempotent_both_directions(client):
    uid_a = await make_user("alice")
    uid_b = await make_user("bob")

    await login(client, "alice")
    r1 = await client.post("/dms", json={"user_id": uid_b})
    assert r1.status_code == 200
    assert r1.json()["created"] is True
    dm_id = r1.json()["id"]

    r2 = await client.post("/dms", json={"user_id": uid_b})
    assert r2.json() == {**r1.json(), "created": False}

    # Reverse direction resolves to the same channel
    await login(client, "bob")
    r3 = await client.post("/dms", json={"user_id": uid_a})
    assert r3.json()["id"] == dm_id and r3.json()["created"] is False
    assert r3.json()["name"] == "Alice" and r3.json()["dm_user_id"] == uid_a

    # Exactly one dm channel, exactly 2 user member rows
    chans = await db.fetch_all("SELECT * FROM channels WHERE type = 'dm_1to1'")
    assert len(chans) == 1
    members = await db.fetch_all(
        "SELECT * FROM channel_members WHERE channel_id = ? ORDER BY member_id", (dm_id,)
    )
    assert [(m["member_type"], m["member_id"]) for m in members] == [
        ("user", uid_a),
        ("user", uid_b),
    ]


async def test_dm_self_and_unknown_target(client):
    uid = await make_user("alice")
    await login(client, "alice")
    assert (await client.post("/dms", json={"user_id": uid})).status_code == 400
    assert (await client.post("/dms", json={"user_id": 999})).status_code == 404


# ---------------------------------------------------------------------------
# PUT /channels/{id}/read — monotonic + lazy main_feed membership
# ---------------------------------------------------------------------------

async def test_read_state_lazy_row_and_monotonic(client):
    uid = await make_user("alice")
    await login(client, "alice")
    main = await main_feed_id()

    # Implicit membership: no row until first mark-read
    assert await member_row(main, "user", uid) is None

    r = await client.put(f"/channels/{main}/read", json={"seq": 5})
    assert r.status_code == 200
    assert r.json() == {"channel_id": main, "last_read_seq": 5}
    assert (await member_row(main, "user", uid))["last_read_seq"] == 5

    # Never lowered
    r = await client.put(f"/channels/{main}/read", json={"seq": 3})
    assert r.json()["last_read_seq"] == 5
    assert (await member_row(main, "user", uid))["last_read_seq"] == 5

    # Raised fine
    r = await client.put(f"/channels/{main}/read", json={"seq": 7})
    assert r.json()["last_read_seq"] == 7

    # Negative rejected by validation
    assert (await client.put(f"/channels/{main}/read", json={"seq": -1})).status_code == 422


async def test_read_state_rejects_non_member_dm(client):
    await make_user("alice")
    uid_b = await make_user("bob")
    await make_user("carol")
    await login(client, "alice")
    dm_id = (await client.post("/dms", json={"user_id": uid_b})).json()["id"]

    await login(client, "carol")
    uid_c = (await client.get("/me")).json()["id"]
    assert (await client.put(f"/channels/{dm_id}/read", json={"seq": 1})).status_code == 403
    # And crucially: no membership row was manufactured by the attempt
    assert await member_row(dm_id, "user", uid_c) is None
    assert (await client.put("/channels/999/read", json={"seq": 1})).status_code == 404


# ---------------------------------------------------------------------------
# GET /channels/{id}/members — access control
# ---------------------------------------------------------------------------

async def test_members_main_feed_lists_all_users_and_explicit_bots(client):
    uid_a = await make_user("alice")
    uid_b = await make_user("bob", display_name="Bobby")
    bot_id = await make_bot("claw", api_key="k1")
    main = await main_feed_id()
    await db.execute(
        "INSERT INTO channel_members (channel_id, member_type, member_id) VALUES (?, 'bot', ?)",
        (main, bot_id),
    )
    await login(client, "alice")
    r = await client.get(f"/channels/{main}/members")
    assert r.status_code == 200
    got = {(m["type"], m["id"], m["name"]) for m in r.json()}
    assert got == {
        ("user", uid_a, "Alice"),
        ("user", uid_b, "Bobby"),
        ("bot", bot_id, "claw"),
    }
    user_entry = next(m for m in r.json() if m["type"] == "user" and m["id"] == uid_a)
    assert user_entry["status"] in ("online", "idle", "dnd", "offline")

    # Bot actor may list main_feed members too (it's main_feed)
    client.cookies.clear()
    r = await client.get(f"/channels/{main}/members", headers={"X-Api-Key": "k1"})
    assert r.status_code == 200


async def test_members_dm_access_control(client):
    uid_a = await make_user("alice")
    uid_b = await make_user("bob")
    await make_user("carol")
    await login(client, "alice")
    dm_id = (await client.post("/dms", json={"user_id": uid_b})).json()["id"]

    r = await client.get(f"/channels/{dm_id}/members")
    assert r.status_code == 200
    assert {(m["type"], m["id"]) for m in r.json()} == {("user", uid_a), ("user", uid_b)}

    # Non-member user: forbidden
    await login(client, "carol")
    assert (await client.get(f"/channels/{dm_id}/members")).status_code == 403
    # Unknown channel: 404
    assert (await client.get("/channels/999/members")).status_code == 404
    # No auth at all: 401
    client.cookies.clear()
    assert (await client.get(f"/channels/{dm_id}/members")).status_code == 401


async def test_members_bot_needs_explicit_dm_membership(client):
    await make_user("alice")
    uid_b = await make_user("bob")
    bot_id = await make_bot("claw", api_key="k1")
    await login(client, "alice")
    dm_id = (await client.post("/dms", json={"user_id": uid_b})).json()["id"]

    # Act as the bot: drop the user cookie so get_actor resolves the API key
    headers = {"X-Api-Key": "k1"}
    client.cookies.clear()
    assert (await client.get(f"/channels/{dm_id}/members", headers=headers)).status_code == 403

    await login(client, "alice")
    r = await client.post(f"/channels/{dm_id}/bots", json={"bot_id": bot_id})
    assert r.status_code == 200 and r.json()["added"] is True

    client.cookies.clear()
    r = await client.get(f"/channels/{dm_id}/members", headers=headers)
    assert r.status_code == 200
    assert ("bot", bot_id, "claw") in {(m["type"], m["id"], m["name"]) for m in r.json()}


# ---------------------------------------------------------------------------
# Bot add/remove
# ---------------------------------------------------------------------------

async def test_bot_add_remove_dm(client):
    await make_user("alice")
    uid_b = await make_user("bob")
    bot_id = await make_bot("claw")
    await login(client, "alice")
    dm_id = (await client.post("/dms", json={"user_id": uid_b})).json()["id"]

    # Add: idempotent
    r = await client.post(f"/channels/{dm_id}/bots", json={"bot_id": bot_id})
    assert r.json() == {"ok": True, "added": True}
    r = await client.post(f"/channels/{dm_id}/bots", json={"bot_id": bot_id})
    assert r.json() == {"ok": True, "added": False}
    assert await member_row(dm_id, "bot", bot_id) is not None

    # Remove: idempotent
    r = await client.delete(f"/channels/{dm_id}/bots/{bot_id}")
    assert r.json() == {"ok": True, "removed": True}
    assert await member_row(dm_id, "bot", bot_id) is None
    r = await client.delete(f"/channels/{dm_id}/bots/{bot_id}")
    assert r.json() == {"ok": True, "removed": False}

    # Unknown bot / channel
    assert (
        await client.post(f"/channels/{dm_id}/bots", json={"bot_id": 999})
    ).status_code == 404
    assert (
        await client.post("/channels/999/bots", json={"bot_id": bot_id})
    ).status_code == 404

    # Requires user auth (bots can't manage membership)
    client.cookies.clear()
    assert (
        await client.post(f"/channels/{dm_id}/bots", json={"bot_id": bot_id})
    ).status_code == 401


async def test_bot_add_remove_dm_requires_membership(client):
    """Only a DM's participants may grant/revoke a bot's access to it —
    otherwise any user could point a bot at someone else's DM stream."""
    await make_user("alice")
    uid_b = await make_user("bob")
    await make_user("carol")
    bot_id = await make_bot("claw")
    await login(client, "alice")
    dm_id = (await client.post("/dms", json={"user_id": uid_b})).json()["id"]

    # Carol is not in the alice<->bob DM: cannot add or remove the bot there.
    await login(client, "carol")
    r = await client.post(f"/channels/{dm_id}/bots", json={"bot_id": bot_id})
    assert r.status_code == 403
    assert await member_row(dm_id, "bot", bot_id) is None

    await login(client, "alice")
    assert (
        await client.post(f"/channels/{dm_id}/bots", json={"bot_id": bot_id})
    ).status_code == 200

    await login(client, "carol")
    assert (
        await client.delete(f"/channels/{dm_id}/bots/{bot_id}")
    ).status_code == 403
    assert await member_row(dm_id, "bot", bot_id) is not None

    # main_feed stays flat access: any user may manage bots there.
    main = await main_feed_id()
    r = await client.post(f"/channels/{main}/bots", json={"bot_id": bot_id})
    assert r.status_code == 200


# ---------------------------------------------------------------------------
# Exported helpers: is_member / user_channel_ids / bot_channel_ids
# ---------------------------------------------------------------------------

async def test_helper_is_member_semantics(client):
    uid_a = await make_user("alice")
    uid_b = await make_user("bob")
    bot_id = await make_bot("claw")
    main = await main_feed_id()
    await login(client, "alice")
    dm_id = (await client.post("/dms", json={"user_id": uid_b})).json()["id"]

    # Users: main_feed implicit (no row), DMs explicit
    assert await member_row(main, "user", uid_a) is None
    assert await channels.is_member(main, "user", uid_a) is True
    assert await channels.is_member(dm_id, "user", uid_a) is True
    assert await channels.is_member(dm_id, "user", uid_b) is True

    # Bots: nothing implicit — not main_feed, and not the DM until added
    assert await channels.is_member(main, "bot", bot_id) is False
    assert await channels.is_member(dm_id, "bot", bot_id) is False
    await client.post(f"/channels/{dm_id}/bots", json={"bot_id": bot_id})
    assert await channels.is_member(dm_id, "bot", bot_id) is True

    # Unknown channel
    assert await channels.is_member(999, "user", uid_a) is False


async def test_helper_channel_id_lists(client):
    uid_a = await make_user("alice")
    uid_b = await make_user("bob")
    await make_user("carol")
    bot_id = await make_bot("claw")
    main = await main_feed_id()
    await login(client, "alice")
    dm_id = (await client.post("/dms", json={"user_id": uid_b})).json()["id"]

    # Users get main_feed implicitly + their DMs; carol only main_feed
    assert sorted(await channels.user_channel_ids(uid_a)) == sorted([main, dm_id])
    carol = await db.fetch_one("SELECT id FROM users WHERE username = 'carol'")
    assert await channels.user_channel_ids(carol["id"]) == [main]

    # Lazy main_feed read-state row must not duplicate main in the list
    await client.put(f"/channels/{main}/read", json={"seq": 1})
    assert sorted(await channels.user_channel_ids(uid_a)) == sorted([main, dm_id])

    # Bots: explicit only — empty until added anywhere
    assert await channels.bot_channel_ids(bot_id) == []
    await db.execute(
        "INSERT INTO channel_members (channel_id, member_type, member_id) VALUES (?, 'bot', ?)",
        (main, bot_id),
    )
    assert await channels.bot_channel_ids(bot_id) == [main]
    await client.post(f"/channels/{dm_id}/bots", json={"bot_id": bot_id})
    assert sorted(await channels.bot_channel_ids(bot_id)) == sorted([main, dm_id])
