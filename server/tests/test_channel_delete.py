"""DELETE /channels/{id} — a text channel and everything in it.

What is being pinned down here:

- WHO may delete: the owner (`created_by`), or an admin who never was a member
  (RULED by plink, 2026-08-17 — deletion destroys content rather than handing
  out read access, so it is not the god-view that invite/kick still refuse).
  Anyone else gets 403.
- WHAT can be deleted: named `text` channels only. main_feed is the house's one
  permanent room and a DM belongs to two people, so both answer 400 truthfully
  instead of pretending.
- That the delete is HARD and complete: the channel's messages, membership rows
  and attachment rows go with it via ON DELETE CASCADE, and — the easy thing to
  get wrong — its content leaves the FTS index, so a former member's search
  cannot still turn it up.
- That the `channel_delete` frame reaches everyone who could see the channel a
  moment earlier. This one needs the recipient list computed BEFORE the row is
  deleted: afterwards `is_member` answers False for everybody, so a post-delete
  fan-out would reach nobody at all.
"""

import asyncio
from contextlib import ExitStack

import pytest
from starlette.testclient import TestClient

from app import db, events
from app.routers import auth

PASSWORD = "correct horse battery staple"
PASSWORD_HASH = auth.hash_password(PASSWORD)  # hash once — argon2 is slow
BOT_KEY = "delete-bot-key-1"


# ---------------------------------------------------------------------------
# Helpers (async / httpx client)
# ---------------------------------------------------------------------------

async def make_user(username: str, is_admin: bool = False) -> int:
    cur = await db.execute(
        """INSERT INTO users (username, password_hash, display_name, is_admin)
           VALUES (?, ?, ?, ?)""",
        (username, PASSWORD_HASH, username.capitalize(), int(is_admin)),
    )
    return cur.lastrowid


async def login(client, username: str) -> str:
    r = await client.post(
        "/auth/login", json={"username": username, "password": PASSWORD}
    )
    assert r.status_code == 200, r.text
    token = r.cookies.get(auth.COOKIE_NAME)
    client.cookies.clear()
    assert token
    return token


def cookie(token: str) -> dict[str, str]:
    return {"cookie": f"{auth.COOKIE_NAME}={token}"}


async def make_channel(client, token: str, name: str, visibility: str = "public") -> int:
    r = await client.post(
        "/channels", json={"name": name, "visibility": visibility}, headers=cookie(token)
    )
    assert r.status_code == 200, r.text
    return r.json()["id"]


async def post_msg(client, token: str, channel_id: int, content: str) -> dict:
    r = await client.post(
        f"/channels/{channel_id}/messages",
        json={"content": content},
        headers=cookie(token),
    )
    assert r.status_code == 200, r.text
    return r.json()


async def main_feed_id() -> int:
    row = await db.fetch_one("SELECT id FROM channels WHERE type = 'main_feed'")
    assert row is not None
    return row["id"]


async def count(sql: str, params=()) -> int:
    row = await db.fetch_one(f"SELECT COUNT(*) AS n FROM ({sql})", params)
    assert row is not None
    return row["n"]


# ---------------------------------------------------------------------------
# Access rules
# ---------------------------------------------------------------------------

async def test_owner_deletes_channel_and_everything_in_it(client):
    """The whole point: the row, its members, its messages and its attachment
    rows go together, and the channel stops existing for every read path."""
    uid = await make_user("alice")
    ta = await login(client, "alice")
    uid_b = await make_user("bob")
    tb = await login(client, "bob")

    cid = await make_channel(client, ta, "doomed")
    msg = await post_msg(client, ta, cid, "this goes away with the room")
    # An attachment row hangs off the message (two cascade hops from channels).
    await db.execute(
        """INSERT INTO attachments
               (message_id, file_path, original_filename, mime_type, size_bytes)
           VALUES (?, 'uploads/x.png', 'x.png', 'image/png', 10)""",
        (msg["id"],),
    )
    # Bob reads it, which lazily creates his channel_members row.
    r = await client.put(
        f"/channels/{cid}/read", json={"seq": msg["seq"]}, headers=cookie(tb)
    )
    assert r.status_code == 200, r.text
    assert await count("SELECT 1 FROM channel_members WHERE channel_id = ?", (cid,)) == 1

    r = await client.delete(f"/channels/{cid}", headers=cookie(ta))
    assert r.status_code == 200, r.text
    assert r.json() == {"ok": True}

    assert await db.fetch_one("SELECT 1 FROM channels WHERE id = ?", (cid,)) is None
    assert await count("SELECT 1 FROM messages WHERE channel_id = ?", (cid,)) == 0
    assert await count("SELECT 1 FROM channel_members WHERE channel_id = ?", (cid,)) == 0
    assert await count("SELECT 1 FROM attachments WHERE message_id = ?", (msg["id"],)) == 0

    # Gone from the sidebar, and gone from history — for its members too.
    listing = (await client.get("/channels", headers=cookie(tb))).json()
    assert cid not in [c["id"] for c in listing]
    r = await client.get(f"/channels/{cid}/messages", headers=cookie(tb))
    assert r.status_code == 404, r.text
    # And from the owner's own view (`uid` is referenced so the fixture user is
    # not merely decorative).
    assert uid != uid_b
    listing = (await client.get("/channels", headers=cookie(ta))).json()
    assert cid not in [c["id"] for c in listing]


async def test_admin_may_delete_a_private_channel_they_never_belonged_to(client):
    """RULED 2026-08-17: an admin can clear out a room they were never in.

    They still cannot READ it — the same admin gets 403 on its history right up
    to the moment it stops existing, which is what makes this not a god-view.
    """
    await make_user("alice")
    ta = await login(client, "alice")
    await make_user("root", is_admin=True)
    tadmin = await login(client, "root")

    cid = await make_channel(client, ta, "backroom", "private")
    await post_msg(client, ta, cid, "admin never gets to read this")

    r = await client.get(f"/channels/{cid}/messages", headers=cookie(tadmin))
    assert r.status_code == 403, r.text

    r = await client.delete(f"/channels/{cid}", headers=cookie(tadmin))
    assert r.status_code == 200, r.text
    assert await db.fetch_one("SELECT 1 FROM channels WHERE id = ?", (cid,)) is None


async def test_ordinary_non_owner_cannot_delete(client):
    await make_user("alice")
    ta = await login(client, "alice")
    await make_user("bob")
    tb = await login(client, "bob")

    cid = await make_channel(client, ta, "alices-room")
    r = await client.delete(f"/channels/{cid}", headers=cookie(tb))
    assert r.status_code == 403, r.text
    assert "owner" in r.json()["detail"]
    assert await db.fetch_one("SELECT 1 FROM channels WHERE id = ?", (cid,)) is not None


async def test_main_feed_and_dms_are_not_deletable(client):
    """Both refusals are 400, not 403: the caller's rights are not the problem,
    the target is. An admin gets the same answer."""
    await make_user("alice")
    ta = await login(client, "alice")
    uid_b = await make_user("bob")
    await make_user("root", is_admin=True)
    tadmin = await login(client, "root")

    main = await main_feed_id()
    dm = (await client.post("/dms", json={"user_id": uid_b}, headers=cookie(ta))).json()

    for token in (ta, tadmin):
        for cid in (main, dm["id"]):
            r = await client.delete(f"/channels/{cid}", headers=cookie(token))
            assert r.status_code == 400, r.text
            assert "text channels" in r.json()["detail"]
    assert await db.fetch_one("SELECT 1 FROM channels WHERE id = ?", (main,)) is not None
    assert await db.fetch_one("SELECT 1 FROM channels WHERE id = ?", (dm["id"],)) is not None


async def test_unknown_channel_is_404(client):
    await make_user("alice")
    ta = await login(client, "alice")
    r = await client.delete("/channels/9999", headers=cookie(ta))
    assert r.status_code == 404, r.text


async def test_delete_requires_auth(client):
    await make_user("alice")
    ta = await login(client, "alice")
    cid = await make_channel(client, ta, "guarded")
    r = await client.delete(f"/channels/{cid}")
    assert r.status_code == 401, r.text


# ---------------------------------------------------------------------------
# Search: a deleted channel's content must not stay findable
# ---------------------------------------------------------------------------

async def test_deleted_channel_content_leaves_the_search_index(client):
    """messages_fts is an external-content FTS5 table whose AFTER DELETE
    trigger fires on the CASCADE from channels — verified here from both ends:
    a former member's search goes quiet, and the index itself holds no orphan
    row for the deleted content (integrity-check would fail if it did)."""
    await make_user("alice")
    ta = await login(client, "alice")
    uid_b = await make_user("bob")
    tb = await login(client, "bob")

    cid = await make_channel(client, ta, "backroom", "private")
    await client.post(
        f"/channels/{cid}/invite", json={"user_id": uid_b}, headers=cookie(ta)
    )
    await post_msg(client, ta, cid, "the zebracorn plan is going ahead")

    # Bob is a member: he finds it.
    r = await client.get("/search", params={"q": "zebracorn"}, headers=cookie(tb))
    assert r.status_code == 200, r.text
    assert [h["message"]["content"] for h in r.json()] == [
        "the zebracorn plan is going ahead"
    ]

    r = await client.delete(f"/channels/{cid}", headers=cookie(ta))
    assert r.status_code == 200, r.text

    r = await client.get("/search", params={"q": "zebracorn"}, headers=cookie(tb))
    assert r.status_code == 200, r.text
    assert r.json() == []

    # Not merely hidden by the JOIN to messages — the index row is gone too.
    assert await db.fetch_all(
        "SELECT rowid FROM messages_fts WHERE messages_fts MATCH 'zebracorn'"
    ) == []
    await db.execute("INSERT INTO messages_fts(messages_fts) VALUES('integrity-check')")


# ---------------------------------------------------------------------------
# WS fan-out (sync TestClient, mirroring test_private_channels.py's harness)
# ---------------------------------------------------------------------------

@pytest.fixture
def wsc(tmp_db_path):
    """Sync TestClient with lifespan running (portal loop shared by REST + WS)."""
    events.clear_subscribers()
    asyncio.run(db.close())  # drop any leaked connection to another tmp DB

    from app.main import create_app

    with TestClient(create_app()) as client:
        yield client
    events.clear_subscribers()


def call(wsc, coro_fn, *args):
    return wsc.portal.call(coro_fn, *args)


def ws_make_user(wsc, username, is_admin=False):
    cur = call(
        wsc,
        db.execute,
        """INSERT INTO users (username, password_hash, display_name, is_admin)
           VALUES (?, ?, ?, ?)""",
        (username, PASSWORD_HASH, username.capitalize(), int(is_admin)),
    )
    return cur.lastrowid


def ws_make_bot(wsc, name, api_key=BOT_KEY):
    cur = call(
        wsc,
        db.execute,
        "INSERT INTO bots (name, api_key_hash) VALUES (?, ?)",
        (name, auth.hash_api_key(api_key)),
    )
    return cur.lastrowid


def ws_login(wsc, username):
    r = wsc.post("/auth/login", json={"username": username, "password": PASSWORD})
    assert r.status_code == 200, r.text
    token = r.cookies.get(auth.COOKIE_NAME)
    wsc.cookies.clear()
    assert token
    return token


def open_user(stack, wsc, token, user_id, *, peers=()):
    ws = stack.enter_context(wsc.websocket_connect("/ws", headers=cookie(token)))
    assert ws.receive_json() == {"type": "ready", "user_id": user_id}
    expected = {"type": "presence", "user_id": user_id, "status": "online"}
    assert ws.receive_json() == expected
    for peer in peers:
        assert peer.receive_json() == expected
    return ws


def open_bot(stack, wsc, bot_id, api_key=BOT_KEY):
    ws = stack.enter_context(wsc.websocket_connect("/ws"))
    ws.send_json({"op": "auth", "api_key": api_key})
    assert ws.receive_json() == {"type": "ready", "bot_id": bot_id}
    return ws


def test_ws_public_channel_delete_reaches_everyone(wsc):
    """A public channel's delete is broadcast exactly as its creation was — to
    every connected user and bot, membership rows or not."""
    a, b = ws_make_user(wsc, "alice"), ws_make_user(wsc, "bob")
    ta, tb = ws_login(wsc, "alice"), ws_login(wsc, "bob")
    # A bot that is a member of NOTHING: public channel events still reach it.
    bot_id = ws_make_bot(wsc, "otto")

    with ExitStack() as stack:
        wa = open_user(stack, wsc, ta, a)
        wb = open_user(stack, wsc, tb, b, peers=[wa])
        wbot = open_bot(stack, wsc, bot_id)

        r = wsc.post("/channels", json={"name": "townsquare"}, headers=cookie(ta))
        assert r.status_code == 200, r.text
        cid = r.json()["id"]
        for ws in (wa, wb, wbot):
            assert ws.receive_json()["type"] == "channel_create"

        r = wsc.delete(f"/channels/{cid}", headers=cookie(ta))
        assert r.status_code == 200, r.text
        expected = {
            "type": "channel_delete",
            "channel_id": cid,
            "by_user_id": a,
            "channel": {
                "id": cid,
                "type": "text",
                "name": "townsquare",
                "visibility": "public",
            },
        }
        for ws in (wa, wb, wbot):
            assert ws.receive_json() == expected


def test_ws_private_channel_delete_reaches_its_members_only(wsc):
    """The recipient list is worked out before the row disappears, so members
    (users AND bots) still hear it. Carol, never let in, hears nothing — proved
    with the sentinel trick: her NEXT frame is later main_feed traffic."""
    a, b, c = (
        ws_make_user(wsc, "alice"),
        ws_make_user(wsc, "bob"),
        ws_make_user(wsc, "carol"),
    )
    ta, tb, tc = ws_login(wsc, "alice"), ws_login(wsc, "bob"), ws_login(wsc, "carol")
    bot_id = ws_make_bot(wsc, "claudette")
    main = call(wsc, db.fetch_one, "SELECT id FROM channels WHERE type = 'main_feed'")["id"]

    with ExitStack() as stack:
        wa = open_user(stack, wsc, ta, a)
        wb = open_user(stack, wsc, tb, b, peers=[wa])
        wcar = open_user(stack, wsc, tc, c, peers=[wa, wb])
        wbot = open_bot(stack, wsc, bot_id)

        r = wsc.post(
            "/channels",
            json={"name": "backroom", "visibility": "private"},
            headers=cookie(ta),
        )
        assert r.status_code == 200, r.text
        cid = r.json()["id"]
        assert wa.receive_json()["type"] == "channel_create"

        r = wsc.post(f"/channels/{cid}/invite", json={"user_id": b}, headers=cookie(ta))
        assert r.status_code == 200, r.text
        assert wa.receive_json()["type"] == "member_add"
        assert wb.receive_json()["type"] == "member_add"

        r = wsc.post(f"/channels/{cid}/bots", json={"bot_id": bot_id}, headers=cookie(ta))
        assert r.status_code == 200, r.text
        for ws in (wa, wb, wbot):
            assert ws.receive_json()["type"] == "member_add"

        r = wsc.delete(f"/channels/{cid}", headers=cookie(ta))
        assert r.status_code == 200, r.text
        expected = {
            "type": "channel_delete",
            "channel_id": cid,
            "by_user_id": a,
            "channel": {
                "id": cid,
                "type": "text",
                "name": "backroom",
                "visibility": "private",
            },
        }
        for ws in (wa, wb, wbot):
            assert ws.receive_json() == expected

        # Sentinel: Carol's next frame is main_feed traffic — the delete of a
        # room she could not see never reached her.
        r = wsc.post(
            f"/channels/{main}/messages", json={"content": "sentinel"}, headers=cookie(ta)
        )
        assert r.status_code == 200, r.text
        frame = wcar.receive_json()
        assert frame["type"] == "message_create" and frame["channel_id"] == main
        assert frame["message"]["content"] == "sentinel"


def test_ws_admin_deleting_a_private_channel_hears_their_own_delete(wsc):
    """An admin who was never a member is added to the recipient list: their
    sidebar showed the bare row (RULED 2026-08-17), so it needs the frame to
    take it away. The room's real members hear it too."""
    a = ws_make_user(wsc, "alice")
    admin_id = ws_make_user(wsc, "root", is_admin=True)
    ta, tadmin = ws_login(wsc, "alice"), ws_login(wsc, "root")

    with ExitStack() as stack:
        wa = open_user(stack, wsc, ta, a)
        wadmin = open_user(stack, wsc, tadmin, admin_id, peers=[wa])

        r = wsc.post(
            "/channels",
            json={"name": "backroom", "visibility": "private"},
            headers=cookie(ta),
        )
        cid = r.json()["id"]
        assert wa.receive_json()["type"] == "channel_create"

        r = wsc.delete(f"/channels/{cid}", headers=cookie(tadmin))
        assert r.status_code == 200, r.text
        expected = {
            "type": "channel_delete",
            "channel_id": cid,
            "by_user_id": admin_id,
            "channel": {
                "id": cid,
                "type": "text",
                "name": "backroom",
                "visibility": "private",
            },
        }
        assert wa.receive_json() == expected
        assert wadmin.receive_json() == expected


def test_ws_private_delete_reaches_admins_but_not_ordinary_non_members(wsc):
    """An admin who is NOT a member still gets the frame, because their sidebar
    is the one place a private channel they cannot read is visible (RULED
    2026-08-17): without it they keep a ghost row for a room that no longer
    exists. Carol, an ordinary non-member who never saw the row, gets nothing —
    the frame is not a way to learn that private rooms exist.
    """
    a = ws_make_user(wsc, "alice")
    c = ws_make_user(wsc, "carol")
    admin_id = ws_make_user(wsc, "root", is_admin=True)
    ta, tc, tadmin = (
        ws_login(wsc, "alice"),
        ws_login(wsc, "carol"),
        ws_login(wsc, "root"),
    )
    main = call(wsc, db.fetch_one, "SELECT id FROM channels WHERE type = 'main_feed'")["id"]

    with ExitStack() as stack:
        wa = open_user(stack, wsc, ta, a)
        wcar = open_user(stack, wsc, tc, c, peers=[wa])
        wadmin = open_user(stack, wsc, tadmin, admin_id, peers=[wa, wcar])

        r = wsc.post(
            "/channels",
            json={"name": "backroom", "visibility": "private"},
            headers=cookie(ta),
        )
        assert r.status_code == 200, r.text
        cid = r.json()["id"]
        assert wa.receive_json()["type"] == "channel_create"

        # The admin's sidebar carries the bare row, with no content.
        row = next(
            item
            for item in wsc.get("/channels", headers=cookie(tadmin)).json()
            if item["id"] == cid
        )
        assert row["member"] is False and row["last_message"] is None
        # Carol's does not.
        assert cid not in [
            item["id"] for item in wsc.get("/channels", headers=cookie(tc)).json()
        ]

        # Deleted by its OWNER — the admin is a bystander here, not the actor.
        r = wsc.delete(f"/channels/{cid}", headers=cookie(ta))
        assert r.status_code == 200, r.text
        expected = {
            "type": "channel_delete",
            "channel_id": cid,
            "by_user_id": a,
            "channel": {
                "id": cid,
                "type": "text",
                "name": "backroom",
                "visibility": "private",
            },
        }
        assert wa.receive_json() == expected
        assert wadmin.receive_json() == expected

        # Sentinel: Carol's next frame is main_feed traffic, not the delete.
        r = wsc.post(
            f"/channels/{main}/messages", json={"content": "sentinel"}, headers=cookie(ta)
        )
        assert r.status_code == 200, r.text
        frame = wcar.receive_json()
        assert frame["type"] == "message_create" and frame["channel_id"] == main
