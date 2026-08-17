"""Per-channel membership as an enforced wall (SPECS/2026-08-08-per-channel-membership).

What is being tested, in the spec's own terms:

- A private channel's CONTENT is unreadable to non-members: history, read
  state, member list, search, WS fan-out and push all refuse.
- Its EXISTENCE is not hidden: GET /channels lists it honestly, with
  `member: false` and no snippet.
- The wall is the same for bots (no carve in either direction) and there is no
  admin god-view.
- Existing channels are grandfathered public — nothing changes for anyone on
  the day this lands.
- Owner-only invite/kick (RULED by plink, 2026-08-12).

Search gets its own section: it is the likeliest leak (it reaches across every
channel at once) and the spec asks for it by name.
"""

import asyncio
import sqlite3
from contextlib import ExitStack

import pytest
from starlette.testclient import TestClient

from app import db, events
from app.routers import auth, channels, notifications
from app.services import push

PASSWORD = "correct horse battery staple"
PASSWORD_HASH = auth.hash_password(PASSWORD)  # hash once — argon2 is slow
BOT_KEY = "private-bot-key-1"
BOT2_KEY = "private-bot-key-2"


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


async def make_bot(name: str, api_key: str = BOT_KEY) -> int:
    cur = await db.execute(
        "INSERT INTO bots (name, api_key_hash) VALUES (?, ?)",
        (name, auth.hash_api_key(api_key)),
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


def key(api_key: str = BOT_KEY) -> dict[str, str]:
    return {"X-Api-Key": api_key}


async def main_feed_id() -> int:
    row = await db.fetch_one("SELECT id FROM channels WHERE type = 'main_feed'")
    assert row is not None
    return row["id"]


async def make_channel(client, token: str, name: str, visibility: str = "public") -> int:
    r = await client.post(
        "/channels", json={"name": name, "visibility": visibility}, headers=cookie(token)
    )
    assert r.status_code == 200, r.text
    assert r.json()["visibility"] == visibility
    return r.json()["id"]


async def post_msg(client, headers: dict[str, str], channel_id: int, content: str) -> dict:
    r = await client.post(
        f"/channels/{channel_id}/messages", json={"content": content}, headers=headers
    )
    assert r.status_code == 200, r.text
    return r.json()


async def member_row(channel_id: int, member_type: str, member_id: int):
    return await db.fetch_one(
        """SELECT * FROM channel_members
           WHERE channel_id = ? AND member_type = ? AND member_id = ?""",
        (channel_id, member_type, member_id),
    )


async def channel_row(channel_id: int):
    return await db.fetch_one("SELECT * FROM channels WHERE id = ?", (channel_id,))


def list_item(items: list[dict], channel_id: int) -> dict:
    return next(i for i in items if i["id"] == channel_id)


# ---------------------------------------------------------------------------
# Migration 008: grandfathering + owner backfill
# ---------------------------------------------------------------------------

def test_migration_grandfathers_public_and_backfills_owner(tmp_path):
    """Applying 008 to a pre-008 database leaves every channel readable exactly
    as it was, and gives orphan text channels an owner (the first admin)."""
    conn = sqlite3.connect(tmp_path / "pre008.db")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    for path in sorted(db.MIGRATIONS_DIR.glob("*.sql")):
        if path.name.startswith("008_"):
            break
        conn.executescript(path.read_text(encoding="utf-8"))

    for username, is_admin in (("plink", 1), ("gable", 1), ("bob", 0)):
        conn.execute(
            """INSERT INTO users (username, password_hash, display_name, is_admin)
               VALUES (?, 'x', ?, ?)""",
            (username, username, is_admin),
        )
    conn.execute("INSERT INTO channels (type, name) VALUES ('main_feed', 'main')")
    conn.execute("INSERT INTO channels (type, name) VALUES ('text', 'custodian')")
    conn.execute("INSERT INTO channels (type, name) VALUES ('dm_1to1', NULL)")
    conn.commit()

    migration = db.MIGRATIONS_DIR / "008_channel_visibility.sql"
    conn.executescript(migration.read_text(encoding="utf-8"))
    conn.commit()

    rows = {r["type"]: dict(r) for r in conn.execute("SELECT * FROM channels")}
    # Grandfathered: everything that existed is public.
    assert {r["visibility"] for r in rows.values()} == {"public"}
    # Text channels get the first admin as owner; main_feed/DMs have none.
    first_admin = conn.execute(
        "SELECT id FROM users WHERE username = 'plink'"
    ).fetchone()["id"]
    assert rows["text"]["created_by"] == first_admin
    assert rows["main_feed"]["created_by"] is None
    assert rows["dm_1to1"]["created_by"] is None

    # The CHECK constraint is live, not decorative.
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO channels (type, name, visibility) VALUES ('text', 'x', 'sorta')"
        )
    conn.close()


# ---------------------------------------------------------------------------
# Grandfathering on the live app: nothing changes for public channels
# ---------------------------------------------------------------------------

async def test_public_is_the_default_and_behaves_exactly_as_before(client):
    await make_user("alice")
    uid_b = await make_user("bob")
    ta = await login(client, "alice")
    tb = await login(client, "bob")

    # No `visibility` in the request body at all -> public.
    r = await client.post("/channels", json={"name": "custodian"}, headers=cookie(ta))
    assert r.status_code == 200
    cid = r.json()["id"]
    assert r.json()["visibility"] == "public" and r.json()["member"] is True
    assert (await channel_row(cid))["visibility"] == "public"

    main = await main_feed_id()
    assert (await channel_row(main))["visibility"] == "public"

    # Bob, who did not create it, is a full member: implicit, no row.
    assert await member_row(cid, "user", uid_b) is None
    assert await channels.is_member(cid, "user", uid_b) is True
    await post_msg(client, cookie(tb), cid, "hello from a non-creator")
    r = await client.get(f"/channels/{cid}/messages", headers=cookie(tb))
    assert r.status_code == 200 and len(r.json()) == 1
    items = (await client.get("/channels", headers=cookie(tb))).json()
    assert list_item(items, cid)["member"] is True
    assert list_item(items, cid)["last_message"]["snippet"] == "hello from a non-creator"


# ---------------------------------------------------------------------------
# The wall: reads by a non-member
# ---------------------------------------------------------------------------

async def test_private_channel_starts_with_only_its_creator(client):
    uid_a = await make_user("alice")
    uid_b = await make_user("bob")
    ta = await login(client, "alice")
    cid = await make_channel(client, ta, "backroom", "private")

    row = await channel_row(cid)
    assert row["visibility"] == "private" and row["created_by"] == uid_a
    assert await member_row(cid, "user", uid_a) is not None
    assert await member_row(cid, "user", uid_b) is None
    assert await channels.is_member(cid, "user", uid_a) is True
    assert await channels.is_member(cid, "user", uid_b) is False
    assert cid in await channels.user_channel_ids(uid_a)
    assert cid not in await channels.user_channel_ids(uid_b)


async def test_non_member_is_refused_on_every_read_path(client):
    await make_user("alice")
    uid_b = await make_user("bob")
    ta = await login(client, "alice")
    tb = await login(client, "bob")
    cid = await make_channel(client, ta, "backroom", "private")
    await post_msg(client, cookie(ta), cid, "the quiet part")

    # History (both scrollback and backfill), read state, members, posting.
    assert (
        await client.get(f"/channels/{cid}/messages", headers=cookie(tb))
    ).status_code == 403
    assert (
        await client.get(f"/channels/{cid}/messages?from_seq=0", headers=cookie(tb))
    ).status_code == 403
    assert (
        await client.put(f"/channels/{cid}/read", json={"seq": 1}, headers=cookie(tb))
    ).status_code == 403
    assert (
        await client.get(f"/channels/{cid}/members", headers=cookie(tb))
    ).status_code == 403
    assert (
        await client.post(
            f"/channels/{cid}/messages", json={"content": "hi"}, headers=cookie(tb)
        )
    ).status_code == 403

    # Crucially: no membership row was manufactured by any of those attempts.
    assert await member_row(cid, "user", uid_b) is None


async def test_channel_list_is_honest_about_existence_and_silent_about_content(client):
    await make_user("alice")
    await make_user("bob")
    ta = await login(client, "alice")
    tb = await login(client, "bob")
    cid = await make_channel(client, ta, "backroom", "private")
    await post_msg(client, cookie(ta), cid, "a snippet bob must not see")

    # Bob sees the channel exists — the list is not a lie — but gets no content.
    items = (await client.get("/channels", headers=cookie(tb))).json()
    row = list_item(items, cid)
    assert row["name"] == "backroom" and row["visibility"] == "private"
    assert row["member"] is False
    assert row["unread"] == 0 and row["last_message"] is None

    # Alice, a member, sees the same row with its content.
    row = list_item((await client.get("/channels", headers=cookie(ta))).json(), cid)
    assert row["member"] is True and row["unread"] == 1
    assert row["last_message"]["snippet"] == "a snippet bob must not see"


async def test_members_listing_of_a_private_channel_is_explicit_only(client):
    uid_a = await make_user("alice")
    uid_b = await make_user("bob")
    await make_user("carol")
    ta = await login(client, "alice")
    cid = await make_channel(client, ta, "backroom", "private")

    got = {
        (m["type"], m["id"])
        for m in (await client.get(f"/channels/{cid}/members", headers=cookie(ta))).json()
    }
    assert got == {("user", uid_a)}  # not "every user in the house"

    await client.post(
        f"/channels/{cid}/invite", json={"user_id": uid_b}, headers=cookie(ta)
    )
    got = {
        (m["type"], m["id"])
        for m in (await client.get(f"/channels/{cid}/members", headers=cookie(ta))).json()
    }
    assert got == {("user", uid_a), ("user", uid_b)}


async def test_no_silent_admin_god_view(client):
    """plink owns the box and can read the DB; the APP ships no quiet
    admin read button, or "private" would be a lie to everyone else."""
    await make_user("alice")
    await make_user("plink", is_admin=True)
    ta = await login(client, "alice")
    tp = await login(client, "plink")
    cid = await make_channel(client, ta, "backroom", "private")
    await post_msg(client, cookie(ta), cid, "administratively interesting")

    assert (
        await client.get(f"/channels/{cid}/messages", headers=cookie(tp))
    ).status_code == 403
    assert (
        await client.get(f"/channels/{cid}/members", headers=cookie(tp))
    ).status_code == 403
    r = await client.get("/search", params={"q": "administratively"}, headers=cookie(tp))
    assert r.status_code == 200 and r.json() == []
    # And no back-door verb: an admin who is not the owner cannot invite
    # themselves in either.
    admin_id = (await db.fetch_one("SELECT id FROM users WHERE username='plink'"))["id"]
    r = await client.post(
        f"/channels/{cid}/invite", json={"user_id": admin_id}, headers=cookie(tp)
    )
    assert r.status_code == 403
    assert await member_row(cid, "user", admin_id) is None


# ---------------------------------------------------------------------------
# Search — the likeliest leak, per the spec
# ---------------------------------------------------------------------------

async def test_search_never_returns_private_content_to_a_non_member(client):
    await make_user("alice")
    uid_b = await make_user("bob")
    ta = await login(client, "alice")
    tb = await login(client, "bob")
    main = await main_feed_id()
    cid = await make_channel(client, ta, "backroom", "private")

    await post_msg(client, cookie(ta), main, "pineapple in the main feed")
    await post_msg(client, cookie(ta), cid, "pineapple in the back room")

    # Non-member: the public hit only. The private message does not exist for him.
    r = await client.get("/search", params={"q": "pineapple"}, headers=cookie(tb))
    assert r.status_code == 200
    assert [h["channel"]["id"] for h in r.json()] == [main]

    # Member: both.
    r = await client.get("/search", params={"q": "pineapple"}, headers=cookie(ta))
    assert sorted(h["channel"]["id"] for h in r.json()) == sorted([main, cid])

    # Invite flips it on for Bob...
    r = await client.post(
        f"/channels/{cid}/invite", json={"user_id": uid_b}, headers=cookie(ta)
    )
    assert r.status_code == 200
    r = await client.get("/search", params={"q": "pineapple"}, headers=cookie(tb))
    assert sorted(h["channel"]["id"] for h in r.json()) == sorted([main, cid])

    # ...and a kick flips it straight back off, including for history he saw.
    r = await client.post(
        f"/channels/{cid}/kick", json={"user_id": uid_b}, headers=cookie(ta)
    )
    assert r.status_code == 200
    r = await client.get("/search", params={"q": "pineapple"}, headers=cookie(tb))
    assert [h["channel"]["id"] for h in r.json()] == [main]


async def test_search_scopes_bots_by_the_same_wall(client):
    await make_user("alice")
    ta = await login(client, "alice")
    bot_id = await make_bot("claudette")
    main = await main_feed_id()
    await db.execute(
        "INSERT INTO channel_members (channel_id, member_type, member_id) VALUES (?, 'bot', ?)",
        (main, bot_id),
    )
    cid = await make_channel(client, ta, "backroom", "private")
    await post_msg(client, cookie(ta), main, "pineapple in the main feed")
    await post_msg(client, cookie(ta), cid, "pineapple in the back room")

    # A bot that isn't a member sees exactly what a human non-member sees.
    r = await client.get("/search", params={"q": "pineapple"}, headers=key())
    assert r.status_code == 200
    assert [h["channel"]["id"] for h in r.json()] == [main]
    assert await channels.bot_channel_ids(bot_id) == [main]

    # The owner adds it; now it searches the private channel like any member.
    r = await client.post(
        f"/channels/{cid}/bots", json={"bot_id": bot_id}, headers=cookie(ta)
    )
    assert r.status_code == 200
    r = await client.get("/search", params={"q": "pineapple"}, headers=key())
    assert sorted(h["channel"]["id"] for h in r.json()) == sorted([main, cid])


async def test_channel_acl_and_message_flags_are_independent_walls(client):
    """Orthogonality: a secret message in a PUBLIC channel is still hidden from
    bots, and a plain message in a PRIVATE channel is still visible to a
    member bot. Neither wall is implemented in terms of the other."""
    await make_user("alice")
    ta = await login(client, "alice")
    bot_id = await make_bot("claudette")
    main = await main_feed_id()
    await db.execute(
        "INSERT INTO channel_members (channel_id, member_type, member_id) VALUES (?, 'bot', ?)",
        (main, bot_id),
    )
    cid = await make_channel(client, ta, "backroom", "private")
    await client.post(f"/channels/{cid}/bots", json={"bot_id": bot_id}, headers=cookie(ta))

    await post_msg(client, cookie(ta), main, "don't tell anyone: rhubarb")
    await post_msg(client, cookie(ta), cid, "plain rhubarb, private room")

    # Bot: member of both channels; the message-level flag still hides one.
    r = await client.get("/search", params={"q": "rhubarb"}, headers=key())
    assert [h["channel"]["id"] for h in r.json()] == [cid]

    # Human member of both: sees both.
    r = await client.get("/search", params={"q": "rhubarb"}, headers=cookie(ta))
    assert sorted(h["channel"]["id"] for h in r.json()) == sorted([main, cid])


# ---------------------------------------------------------------------------
# Verbs: invite / join / leave / kick
# ---------------------------------------------------------------------------

async def test_invite_is_owner_only_and_grants_read_access(client):
    await make_user("alice")
    uid_b = await make_user("bob")
    uid_c = await make_user("carol")
    ta = await login(client, "alice")
    tb = await login(client, "bob")
    cid = await make_channel(client, ta, "backroom", "private")
    await post_msg(client, cookie(ta), cid, "history bob will get to read")

    captured: list[dict] = []
    events.subscribe(captured.append)

    r = await client.post(
        f"/channels/{cid}/invite", json={"user_id": uid_b}, headers=cookie(ta)
    )
    assert r.status_code == 200 and r.json() == {"ok": True, "added": True}
    assert [e["type"] for e in captured] == ["member_add"]
    assert captured[0]["member_type"] == "user" and captured[0]["member_id"] == uid_b
    assert captured[0]["channel"]["visibility"] == "private"

    # Idempotent, and no second event.
    r = await client.post(
        f"/channels/{cid}/invite", json={"user_id": uid_b}, headers=cookie(ta)
    )
    assert r.json() == {"ok": True, "added": False}
    assert len(captured) == 1

    # Bob can now read the channel's whole history, not just what follows.
    r = await client.get(f"/channels/{cid}/messages", headers=cookie(tb))
    assert r.status_code == 200
    assert [m["content"] for m in r.json()] == ["history bob will get to read"]

    # RULED 2026-08-12: a member who is not the owner cannot invite anyone.
    r = await client.post(
        f"/channels/{cid}/invite", json={"user_id": uid_c}, headers=cookie(tb)
    )
    assert r.status_code == 403
    assert await member_row(cid, "user", uid_c) is None

    # Unknown user -> 404; public channel -> 400 (nothing to invite anyone to).
    assert (
        await client.post(
            f"/channels/{cid}/invite", json={"user_id": 999}, headers=cookie(ta)
        )
    ).status_code == 404
    pub = await make_channel(client, ta, "commons", "public")
    assert (
        await client.post(
            f"/channels/{pub}/invite", json={"user_id": uid_b}, headers=cookie(ta)
        )
    ).status_code == 400


async def test_join_is_refused_for_private_and_a_no_op_for_public(client):
    await make_user("alice")
    uid_b = await make_user("bob")
    ta = await login(client, "alice")
    tb = await login(client, "bob")
    priv = await make_channel(client, ta, "backroom", "private")
    pub = await make_channel(client, ta, "commons", "public")
    main = await main_feed_id()

    # Private: invite-only, so asking is not a way in.
    r = await client.post(f"/channels/{priv}/join", headers=cookie(tb))
    assert r.status_code == 403 and "invite-only" in r.json()["detail"]
    assert await member_row(priv, "user", uid_b) is None

    # Public (and main_feed): everyone is already a member — a truthful no-op.
    for cid in (pub, main):
        r = await client.post(f"/channels/{cid}/join", headers=cookie(tb))
        assert r.status_code == 200 and r.json() == {"ok": True, "joined": False}

    # The owner "joining" their own private channel: already in.
    r = await client.post(f"/channels/{priv}/join", headers=cookie(ta))
    assert r.json() == {"ok": True, "joined": False}


async def test_anyone_can_leave_and_leaving_ends_access(client):
    await make_user("alice")
    uid_b = await make_user("bob")
    ta = await login(client, "alice")
    tb = await login(client, "bob")
    cid = await make_channel(client, ta, "backroom", "private")
    await client.post(
        f"/channels/{cid}/invite", json={"user_id": uid_b}, headers=cookie(ta)
    )
    await post_msg(client, cookie(ta), cid, "spoken while bob was here")
    await client.put(f"/channels/{cid}/read", json={"seq": 1}, headers=cookie(tb))

    captured: list[dict] = []
    events.subscribe(captured.append)

    r = await client.post(f"/channels/{cid}/leave", headers=cookie(tb))
    assert r.status_code == 200 and r.json() == {"ok": True, "left": True}
    assert [e["type"] for e in captured] == ["member_remove"]
    assert captured[0]["member_id"] == uid_b

    assert await member_row(cid, "user", uid_b) is None  # read state goes too
    assert (
        await client.get(f"/channels/{cid}/messages", headers=cookie(tb))
    ).status_code == 403
    r = await client.get("/search", params={"q": "spoken"}, headers=cookie(tb))
    assert r.json() == []

    # Idempotent; no second event.
    r = await client.post(f"/channels/{cid}/leave", headers=cookie(tb))
    assert r.json() == {"ok": True, "left": False}
    assert len(captured) == 1

    # The owner may leave too — "anyone can leave".
    r = await client.post(f"/channels/{cid}/leave", headers=cookie(ta))
    assert r.json() == {"ok": True, "left": True}
    assert (
        await client.get(f"/channels/{cid}/messages", headers=cookie(ta))
    ).status_code == 403

    # Public channels have nothing to leave: membership there is implicit.
    pub = await make_channel(client, ta, "commons", "public")
    assert (await client.post(f"/channels/{pub}/leave", headers=cookie(tb))).status_code == 400
    main = await main_feed_id()
    assert (await client.post(f"/channels/{main}/leave", headers=cookie(tb))).status_code == 400


async def test_kick_is_owner_only_and_never_the_owner(client):
    uid_a = await make_user("alice")
    uid_b = await make_user("bob")
    uid_c = await make_user("carol")
    ta = await login(client, "alice")
    tb = await login(client, "bob")
    cid = await make_channel(client, ta, "backroom", "private")
    for uid in (uid_b, uid_c):
        r = await client.post(
            f"/channels/{cid}/invite", json={"user_id": uid}, headers=cookie(ta)
        )
        assert r.status_code == 200

    # A member who is not the owner cannot kick.
    r = await client.post(
        f"/channels/{cid}/kick", json={"user_id": uid_c}, headers=cookie(tb)
    )
    assert r.status_code == 403
    assert await member_row(cid, "user", uid_c) is not None

    # The owner can, and the kicked user's access ends with the row.
    captured: list[dict] = []
    events.subscribe(captured.append)
    r = await client.post(
        f"/channels/{cid}/kick", json={"user_id": uid_b}, headers=cookie(ta)
    )
    assert r.status_code == 200 and r.json() == {"ok": True, "removed": True}
    assert [e["type"] for e in captured] == ["member_remove"]
    assert await member_row(cid, "user", uid_b) is None
    assert (
        await client.get(f"/channels/{cid}/messages", headers=cookie(tb))
    ).status_code == 403

    # Idempotent.
    r = await client.post(
        f"/channels/{cid}/kick", json={"user_id": uid_b}, headers=cookie(ta)
    )
    assert r.json() == {"ok": True, "removed": False}

    # The owner cannot be kicked (their exit is /leave).
    r = await client.post(
        f"/channels/{cid}/kick", json={"user_id": uid_a}, headers=cookie(ta)
    )
    assert r.status_code == 400
    assert await member_row(cid, "user", uid_a) is not None


async def test_membership_verbs_require_a_user_and_a_real_channel(client):
    await make_user("alice")
    uid_b = await make_user("bob")
    ta = await login(client, "alice")
    cid = await make_channel(client, ta, "backroom", "private")

    # Unknown channel -> 404 on every verb.
    for path, body in (
        ("/channels/999/invite", {"user_id": uid_b}),
        ("/channels/999/kick", {"user_id": uid_b}),
        ("/channels/999/join", None),
        ("/channels/999/leave", None),
    ):
        r = await client.post(path, json=body, headers=cookie(ta))
        assert r.status_code == 404, path

    # Bot auth is not user auth: membership is a human decision, so even a
    # valid API key is a 401 on these verbs.
    await make_bot("claw")
    for path, body in (
        (f"/channels/{cid}/invite", {"user_id": uid_b}),
        (f"/channels/{cid}/kick", {"user_id": uid_b}),
        (f"/channels/{cid}/join", None),
        (f"/channels/{cid}/leave", None),
    ):
        r = await client.post(path, json=body, headers=key())
        assert r.status_code == 401, path


# ---------------------------------------------------------------------------
# Bots: the same wall, no carve, in either direction
# ---------------------------------------------------------------------------

async def test_bot_needs_explicit_membership_in_a_private_channel(client):
    await make_user("alice")
    ta = await login(client, "alice")
    bot_id = await make_bot("claudette")
    cid = await make_channel(client, ta, "backroom", "private")
    await post_msg(client, cookie(ta), cid, "resident-visible only once invited")

    # Not a member: reads refuse exactly as a human non-member's do.
    assert (await client.get(f"/channels/{cid}/messages", headers=key())).status_code == 403
    assert (await client.get(f"/channels/{cid}/members", headers=key())).status_code == 403
    assert (
        await client.post(
            f"/channels/{cid}/messages", json={"content": "hi"}, headers=key()
        )
    ).status_code == 403
    assert await channels.is_member(cid, "bot", bot_id) is False

    # The owner lets it in; now it reads like any member.
    r = await client.post(
        f"/channels/{cid}/bots", json={"bot_id": bot_id}, headers=cookie(ta)
    )
    assert r.status_code == 200 and r.json() == {"ok": True, "added": True}
    r = await client.get(f"/channels/{cid}/messages", headers=key())
    assert r.status_code == 200
    assert [m["content"] for m in r.json()] == ["resident-visible only once invited"]


async def test_only_the_owner_may_hand_a_bot_the_keys_to_a_private_channel(client):
    await make_user("alice")
    uid_b = await make_user("bob")
    ta = await login(client, "alice")
    tb = await login(client, "bob")
    bot_id = await make_bot("claudette")
    cid = await make_channel(client, ta, "backroom", "private")
    await client.post(
        f"/channels/{cid}/invite", json={"user_id": uid_b}, headers=cookie(ta)
    )

    # Bob is a member but not the owner: adding a bot is an invite by another
    # name, so it is the owner's call.
    r = await client.post(
        f"/channels/{cid}/bots", json={"bot_id": bot_id}, headers=cookie(tb)
    )
    assert r.status_code == 403
    assert await member_row(cid, "bot", bot_id) is None

    assert (
        await client.post(
            f"/channels/{cid}/bots", json={"bot_id": bot_id}, headers=cookie(ta)
        )
    ).status_code == 200
    # Nor may a non-owner member revoke it.
    assert (
        await client.delete(f"/channels/{cid}/bots/{bot_id}", headers=cookie(tb))
    ).status_code == 403
    assert (
        await client.delete(f"/channels/{cid}/bots/{bot_id}", headers=cookie(ta))
    ).status_code == 200

    # Public text channels keep their flat access: any user manages bots there.
    pub = await make_channel(client, ta, "commons", "public")
    r = await client.post(
        f"/channels/{pub}/bots", json={"bot_id": bot_id}, headers=cookie(tb)
    )
    assert r.status_code == 200


# ---------------------------------------------------------------------------
# Push notifications carry a content snippet — non-members are not candidates
# ---------------------------------------------------------------------------

@pytest.fixture
def sent(monkeypatch):
    """Monkeypatch services.push.send_push with a sync recorder."""
    calls: list[dict] = []

    def fake_send_push(row, payload):
        calls.append({"user_id": row["user_id"], "payload": payload})

    monkeypatch.setattr(push, "send_push", fake_send_push)
    return calls


async def test_push_never_reaches_a_non_member_of_a_private_channel(client, sent):
    uid_a = await make_user("alice")
    uid_b = await make_user("bob")
    ta = await login(client, "alice")
    for uid, endpoint in ((uid_a, "https://p.example/a"), (uid_b, "https://p.example/b")):
        await db.execute(
            """INSERT INTO push_subscriptions (user_id, endpoint, keys_json, created_at)
               VALUES (?, ?, '{}', ?)""",
            (uid, endpoint, db.utc_now()),
        )
    cid = await make_channel(client, ta, "backroom", "private")

    # A mention would normally push — but the snippet is content, and bob is
    # not a member.
    await post_msg(client, cookie(ta), cid, "bob should never see this snippet")
    await notifications.wait_pending()
    assert {c["user_id"] for c in sent} == set()

    # Invited, the same mention reaches him.
    await client.post(
        f"/channels/{cid}/invite", json={"user_id": uid_b}, headers=cookie(ta)
    )
    await post_msg(client, cookie(ta), cid, "bob, now you are in")
    await notifications.wait_pending()
    assert {c["user_id"] for c in sent} == {uid_b}


# ---------------------------------------------------------------------------
# WS fan-out (sync TestClient, mirroring tests/test_ws.py's harness)
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


def ws_make_user(wsc, username):
    cur = call(
        wsc,
        db.execute,
        "INSERT INTO users (username, password_hash, display_name) VALUES (?, ?, ?)",
        (username, PASSWORD_HASH, username.capitalize()),
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


def test_ws_private_channel_fanout_is_members_only(wsc):
    """channel_create for a private channel reaches only its members, and so
    does its traffic. Non-members are asserted with the sentinel trick: the
    NEXT frame they receive is later main_feed traffic."""
    a, b = ws_make_user(wsc, "alice"), ws_make_user(wsc, "bob")
    ta, tb = ws_login(wsc, "alice"), ws_login(wsc, "bob")
    outsider_bot = ws_make_bot(wsc, "otto", BOT2_KEY)
    main = call(wsc, db.fetch_one, "SELECT id FROM channels WHERE type = 'main_feed'")["id"]
    call(
        wsc,
        db.execute,
        "INSERT INTO channel_members (channel_id, member_type, member_id) VALUES (?, 'bot', ?)",
        (main, outsider_bot),
    )

    with ExitStack() as stack:
        wa = open_user(stack, wsc, ta, a)
        wb = open_user(stack, wsc, tb, b, peers=[wa])
        wot = open_bot(stack, wsc, outsider_bot, BOT2_KEY)

        r = wsc.post(
            "/channels",
            json={"name": "backroom", "visibility": "private"},
            headers=cookie(ta),
        )
        assert r.status_code == 200, r.text
        cid = r.json()["id"]
        # Only Alice (its sole member) is told it exists over the wire.
        assert wa.receive_json() == {
            "type": "channel_create",
            "channel": {
                "id": cid,
                "type": "text",
                "name": "backroom",
                "visibility": "private",
            },
        }

        # Message traffic in it reaches Alice alone.
        r = wsc.post(
            f"/channels/{cid}/messages",
            json={"content": "just me in here"},
            headers=cookie(ta),
        )
        assert r.status_code == 200, r.text
        frame = wa.receive_json()
        assert frame["type"] == "message_create" and frame["channel_id"] == cid

        # Sentinel: Bob's and otto's NEXT frames are main_feed traffic — none
        # of the private channel's frames ever reached them.
        r = wsc.post(
            f"/channels/{main}/messages", json={"content": "sentinel"}, headers=cookie(ta)
        )
        assert r.status_code == 200, r.text
        for ws in (wb, wot):
            frame = ws.receive_json()
            assert frame["type"] == "message_create" and frame["channel_id"] == main
            assert frame["message"]["content"] == "sentinel"


def test_ws_member_add_and_remove_reach_members_and_the_subject(wsc):
    a, b, c = (
        ws_make_user(wsc, "alice"),
        ws_make_user(wsc, "bob"),
        ws_make_user(wsc, "carol"),
    )
    ta, tb, tc = ws_login(wsc, "alice"), ws_login(wsc, "bob"), ws_login(wsc, "carol")
    main = call(wsc, db.fetch_one, "SELECT id FROM channels WHERE type = 'main_feed'")["id"]

    with ExitStack() as stack:
        wa = open_user(stack, wsc, ta, a)
        wb = open_user(stack, wsc, tb, b, peers=[wa])
        wcar = open_user(stack, wsc, tc, c, peers=[wa, wb])

        r = wsc.post(
            "/channels",
            json={"name": "backroom", "visibility": "private"},
            headers=cookie(ta),
        )
        cid = r.json()["id"]
        assert wa.receive_json()["type"] == "channel_create"

        # Invite: the owner (a member) and Bob (the subject) both hear it.
        r = wsc.post(f"/channels/{cid}/invite", json={"user_id": b}, headers=cookie(ta))
        assert r.status_code == 200, r.text
        expected = {
            "type": "member_add",
            "channel_id": cid,
            "member_type": "user",
            "member_id": b,
            "channel": {
                "id": cid,
                "type": "text",
                "name": "backroom",
                "visibility": "private",
            },
        }
        assert wa.receive_json() == expected
        assert wb.receive_json() == expected

        # Kick: same audience, including the member it just happened to.
        r = wsc.post(f"/channels/{cid}/kick", json={"user_id": b}, headers=cookie(ta))
        assert r.status_code == 200, r.text
        expected = {**expected, "type": "member_remove"}
        assert wa.receive_json() == expected
        assert wb.receive_json() == expected

        # Sentinel: Carol, never a member, heard none of it.
        r = wsc.post(
            f"/channels/{main}/messages", json={"content": "sentinel"}, headers=cookie(ta)
        )
        assert r.status_code == 200, r.text
        frame = wcar.receive_json()
        assert frame["type"] == "message_create" and frame["channel_id"] == main
