"""Bot reply modes per channel membership, and direct messages with a bot."""

import json
from contextlib import ExitStack

import pytest

from app import db, events
from app.config import reset_settings_cache
from app.routers import auth

from .test_ws import (
    BOT2_KEY,
    BOT_KEY,
    add_member,
    call,
    cookie,
    login as ws_login,
    main_feed_id as ws_main_feed_id,
    make_bot as ws_make_bot,
    make_user as ws_make_user,
    open_bot,
    open_user,
    post_msg,
    wsc,  # noqa: F401 — fixture
)

PASSWORD = "correct horse battery staple"


async def make_user(username: str, *, is_admin: bool = False) -> int:
    cur = await db.execute(
        """INSERT INTO users (username, password_hash, display_name, is_admin)
           VALUES (?, ?, ?, ?)""",
        (username, auth.hash_password(PASSWORD), username.capitalize(), int(is_admin)),
    )
    return cur.lastrowid


async def make_bot(name: str, api_key: str = "k1", *, dm_open: bool = False) -> int:
    cur = await db.execute(
        "INSERT INTO bots (name, api_key_hash, dm_open) VALUES (?, ?, ?)",
        (name, auth.hash_api_key(api_key), int(dm_open)),
    )
    return cur.lastrowid


async def login(client, username: str) -> None:
    client.cookies.clear()
    r = await client.post("/auth/login", json={"username": username, "password": PASSWORD})
    assert r.status_code == 200, r.text


async def main_feed_id() -> int:
    row = await db.fetch_one("SELECT id FROM channels WHERE type = 'main_feed'")
    return row["id"]


async def bot_row(channel_id: int, bot_id: int):
    return await db.fetch_one(
        """SELECT * FROM channel_members
           WHERE channel_id = ? AND member_type = 'bot' AND member_id = ?""",
        (channel_id, bot_id),
    )


@pytest.fixture
def mention_only(monkeypatch):
    def apply(*channel_ids: int) -> None:
        monkeypatch.setenv("MENTION_ONLY_CHANNEL_IDS", json.dumps(list(channel_ids)))
        reset_settings_cache()

    return apply


@pytest.fixture
def bus():
    seen: list[dict] = []
    events.subscribe(seen.append)
    yield seen
    events.unsubscribe(seen.append)


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

async def test_a_new_membership_row_replies_on_mention_and_a_new_bot_takes_no_dms(app):
    applied = {r["filename"] for r in await db.fetch_all("SELECT filename FROM schema_migrations")}
    assert "018_bot_reply_modes.sql" in applied
    cur = await db.execute(
        "INSERT INTO bots (name, api_key_hash) VALUES ('plain', 'x')"
    )
    bot_id = cur.lastrowid
    main = await main_feed_id()
    add = "INSERT INTO channel_members (channel_id, member_type, member_id) VALUES (?, 'bot', ?)"
    await db.execute(add, (main, bot_id))
    assert (await bot_row(main, bot_id))["reply_mode"] == "mention"
    assert (await db.fetch_one("SELECT dm_open FROM bots WHERE id = ?", (bot_id,)))["dm_open"] == 0
    with pytest.raises(Exception):
        await db.execute(
            "UPDATE channel_members SET reply_mode = 'sometimes' WHERE member_id = ?", (bot_id,)
        )


# ---------------------------------------------------------------------------
# Adding and changing a mode
# ---------------------------------------------------------------------------

async def test_a_bot_added_for_every_message_shows_that_mode_in_the_member_list(client):
    await make_user("alice")
    always = await make_bot("otto", "k1")
    plain = await make_bot("claw", "k2")
    await login(client, "alice")
    main = await main_feed_id()

    r = await client.post(f"/channels/{main}/bots", json={"bot_id": always, "reply_mode": "always"})
    assert r.json() == {"ok": True, "added": True}
    r = await client.post(f"/channels/{main}/bots", json={"bot_id": plain})
    assert r.json() == {"ok": True, "added": True}

    members = (await client.get(f"/channels/{main}/members")).json()
    modes = {(m["type"], m["id"]): m["reply_mode"] for m in members}
    assert modes[("bot", always)] == "always"
    assert modes[("bot", plain)] == "mention"
    assert all(mode is None for (kind, _), mode in modes.items() if kind == "user")

    bad = await client.post(f"/channels/{main}/bots", json={"bot_id": plain, "reply_mode": "loud"})
    assert bad.status_code == 422


async def test_only_a_bot_manager_may_change_a_bots_reply_mode(client):
    await make_user("alice")
    await make_user("bob")
    bot_id = await make_bot("otto")
    await login(client, "alice")
    room = (await client.post("/channels", json={"name": "den", "visibility": "private"})).json()["id"]
    bob_id = (await db.fetch_one("SELECT id FROM users WHERE username = 'bob'"))["id"]
    await client.post(f"/channels/{room}/invite", json={"user_id": bob_id})
    await client.post(f"/channels/{room}/bots", json={"bot_id": bot_id})

    await login(client, "bob")
    r = await client.patch(f"/channels/{room}/bots/{bot_id}", json={"reply_mode": "always"})
    assert r.status_code == 403
    assert (await bot_row(room, bot_id))["reply_mode"] == "mention"

    await login(client, "alice")
    r = await client.patch(f"/channels/{room}/bots/{bot_id}", json={"reply_mode": "always"})
    assert r.json() == {"ok": True, "reply_mode": "always", "changed": True}
    assert (await bot_row(room, bot_id))["reply_mode"] == "always"

    stranger = await make_bot("claw", "k2")
    r = await client.patch(f"/channels/{room}/bots/{stranger}", json={"reply_mode": "always"})
    assert r.status_code == 404

    client.cookies.clear()
    r = await client.patch(f"/channels/{room}/bots/{bot_id}", json={"reply_mode": "mention"})
    assert r.status_code == 401


async def test_every_message_is_refused_in_a_mention_only_room(client, mention_only):
    await make_user("alice")
    bot_id = await make_bot("otto")
    await login(client, "alice")
    main = await main_feed_id()
    shed = (await client.post("/channels", json={"name": "shed"})).json()["id"]
    mention_only(main)

    r = await client.post(f"/channels/{main}/bots", json={"bot_id": bot_id, "reply_mode": "always"})
    assert r.status_code == 400
    assert r.json()["detail"] == "This room is mention-only"
    assert await bot_row(main, bot_id) is None

    assert (await client.post(f"/channels/{main}/bots", json={"bot_id": bot_id})).status_code == 200
    r = await client.patch(f"/channels/{main}/bots/{bot_id}", json={"reply_mode": "always"})
    assert r.status_code == 400
    assert r.json()["detail"] == "This room is mention-only"

    rows = {c["id"]: c for c in (await client.get("/channels")).json()}
    assert rows[main]["mention_only"] is True
    assert rows[shed]["mention_only"] is False

    mention_only(main, shed + 1)
    yard = (await client.post("/channels", json={"name": "yard"})).json()
    assert yard["id"] == shed + 1 and yard["mention_only"] is True


async def test_custodian_is_mention_only_by_name_with_no_ids_configured(client, monkeypatch):
    await make_user("alice")
    bot_id = await make_bot("otto")
    await login(client, "alice")
    custodian = (await client.post("/channels", json={"name": "custodian"})).json()
    assert custodian["mention_only"] is True
    r = await client.post(
        f"/channels/{custodian['id']}/bots", json={"bot_id": bot_id, "reply_mode": "always"}
    )
    assert r.status_code == 400 and r.json()["detail"] == "This room is mention-only"

    monkeypatch.setenv("MENTION_ONLY_CHANNEL_NAMES", json.dumps(["den"]))
    reset_settings_cache()
    den = (await client.post("/channels", json={"name": "den"})).json()["id"]
    rows = {c["id"]: c for c in (await client.get("/channels")).json()}
    assert rows[den]["mention_only"] is True
    assert rows[custodian["id"]]["mention_only"] is False
    r = await client.post(f"/channels/{den}/bots", json={"bot_id": bot_id, "reply_mode": "always"})
    assert r.status_code == 400
    r = await client.post(
        f"/channels/{custodian['id']}/bots", json={"bot_id": bot_id, "reply_mode": "always"}
    )
    assert r.status_code == 200


async def test_a_real_mode_change_publishes_member_update_and_a_repeat_does_not(client, bus):
    await make_user("alice")
    bot_id = await make_bot("otto")
    await login(client, "alice")
    main = await main_feed_id()
    await client.post(f"/channels/{main}/bots", json={"bot_id": bot_id})
    bus.clear()

    await client.patch(f"/channels/{main}/bots/{bot_id}", json={"reply_mode": "always"})
    r = await client.patch(f"/channels/{main}/bots/{bot_id}", json={"reply_mode": "always"})
    assert r.json()["changed"] is False

    updates = [e for e in bus if e["type"] == "member_update"]
    assert len(updates) == 1
    assert updates[0]["channel_id"] == main
    assert updates[0]["member_type"] == "bot"
    assert updates[0]["member_id"] == bot_id
    assert updates[0]["reply_mode"] == "always"


# ---------------------------------------------------------------------------
# DMs with a bot
# ---------------------------------------------------------------------------

async def test_a_bot_takes_dms_only_once_an_admin_opens_them(client):
    await make_user("root", is_admin=True)
    await make_user("alice")
    bot_id = await make_bot("otto")
    await login(client, "alice")

    assert (await client.post("/dms", json={"bot_id": bot_id})).status_code == 404
    assert (await client.post("/dms", json={"bot_id": 999})).status_code == 404
    assert (await client.patch(f"/bots/{bot_id}", json={"dm_open": True})).status_code == 403

    await login(client, "root")
    r = await client.patch(f"/bots/{bot_id}", json={"dm_open": True})
    assert r.status_code == 200 and r.json()["dm_open"] is True
    listed = {b["id"]: b for b in (await client.get("/bots")).json()}
    assert listed[bot_id]["dm_open"] is True

    await login(client, "alice")
    first = (await client.post("/dms", json={"bot_id": bot_id})).json()
    assert first["created"] is True
    assert first["name"] == "otto"
    assert first["dm_bot_id"] == bot_id and first["dm_user_id"] is None
    again = (await client.post("/dms", json={"bot_id": bot_id})).json()
    assert again["id"] == first["id"] and again["created"] is False

    members = await db.fetch_all(
        "SELECT member_type, member_id, reply_mode FROM channel_members WHERE channel_id = ?",
        (first["id"],),
    )
    alice_id = (await db.fetch_one("SELECT id FROM users WHERE username = 'alice'"))["id"]
    assert {(m["member_type"], m["member_id"]) for m in members} == {
        ("user", alice_id), ("bot", bot_id),
    }
    assert (await bot_row(first["id"], bot_id))["reply_mode"] == "always"

    row = next(c for c in (await client.get("/channels")).json() if c["id"] == first["id"])
    assert row["name"] == "otto"
    assert row["dm_bot_id"] == bot_id and row["dm_user_id"] is None
    assert row["dm_bot_avatar_url"] is None


async def test_a_bot_dm_is_invisible_to_everyone_but_its_person(client):
    await make_user("root", is_admin=True)
    await make_user("alice")
    bot_id = await make_bot("otto", dm_open=True)
    await login(client, "alice")
    dm = (await client.post("/dms", json={"bot_id": bot_id})).json()["id"]

    await login(client, "root")
    assert dm not in {c["id"] for c in (await client.get("/channels")).json()}
    assert (await client.get(f"/channels/{dm}/members")).status_code == 403
    assert (await client.get(f"/channels/{dm}/messages")).status_code == 403


async def test_a_dm_request_names_exactly_one_of_a_person_or_a_bot(client):
    await make_user("alice")
    bob = await make_user("bob")
    bot_id = await make_bot("otto", dm_open=True)
    await login(client, "alice")
    both = await client.post("/dms", json={"user_id": bob, "bot_id": bot_id})
    assert both.status_code == 422
    assert "exactly one" in both.json()["detail"]
    assert (await client.post("/dms", json={})).status_code == 422


async def test_person_and_bot_dm_lookups_never_find_each_others_rooms(client):
    await make_user("alice")
    bob = await make_user("bob")
    bot_id = await make_bot("otto", dm_open=True)
    await login(client, "alice")

    people = (await client.post("/dms", json={"user_id": bob})).json()
    r = await client.post(f"/channels/{people['id']}/bots", json={"bot_id": bot_id, "reply_mode": "always"})
    assert r.status_code == 200

    bot_dm = (await client.post("/dms", json={"bot_id": bot_id})).json()
    assert bot_dm["created"] is True and bot_dm["id"] != people["id"]
    again = (await client.post("/dms", json={"user_id": bob})).json()
    assert again["id"] == people["id"] and again["dm_bot_id"] is None

    rows = {c["id"]: c for c in (await client.get("/channels")).json()}
    assert rows[people["id"]]["dm_user_id"] == bob
    assert rows[people["id"]]["dm_bot_id"] is None
    assert rows[bot_dm["id"]]["dm_bot_id"] == bot_id

    await login(client, "bob")
    bob_dm = (await client.post("/dms", json={"bot_id": bot_id})).json()
    assert bob_dm["id"] not in (people["id"], bot_dm["id"])


async def test_a_bot_dm_keeps_its_bot_and_its_mode(client):
    await make_user("alice")
    bot_id = await make_bot("otto", dm_open=True)
    other = await make_bot("claw", "k2")
    await login(client, "alice")
    dm = (await client.post("/dms", json={"bot_id": bot_id})).json()["id"]

    r = await client.patch(f"/channels/{dm}/bots/{bot_id}", json={"reply_mode": "mention"})
    assert r.status_code == 403 and r.json()["detail"] == "A bot DM always replies"
    assert (await client.delete(f"/channels/{dm}/bots/{bot_id}")).status_code == 403
    assert (await client.post(f"/channels/{dm}/bots", json={"bot_id": other})).status_code == 403
    assert (await bot_row(dm, bot_id))["reply_mode"] == "always"


async def test_leaving_a_bot_dm_deletes_the_room_and_tells_its_members(client, bus):
    alice = await make_user("alice")
    bot_id = await make_bot("otto", dm_open=True)
    await login(client, "alice")
    dm = (await client.post("/dms", json={"bot_id": bot_id})).json()["id"]
    r = await client.post(f"/channels/{dm}/messages", json={"content": "here is the code"})
    assert r.status_code == 200, r.text
    bus.clear()

    r = await client.post(f"/channels/{dm}/leave")
    assert r.json() == {"ok": True, "left": True}
    assert await db.fetch_one("SELECT 1 FROM channels WHERE id = ?", (dm,)) is None
    assert await db.fetch_one("SELECT 1 FROM channel_members WHERE channel_id = ?", (dm,)) is None
    assert await db.fetch_one("SELECT 1 FROM messages WHERE channel_id = ?", (dm,)) is None
    [deleted] = [e for e in bus if e["type"] == "channel_delete"]
    assert deleted["channel_id"] == dm and deleted["by_user_id"] == alice
    assert deleted["recipients"] == [["bot", bot_id], ["user", alice]]

    assert (await client.post(f"/channels/{dm}/leave")).status_code == 404
    fresh = (await client.post("/dms", json={"bot_id": bot_id})).json()
    assert fresh["created"] is True and fresh["id"] != dm


async def test_closing_a_bots_dms_leaves_its_existing_dms_open(client):
    await make_user("root", is_admin=True)
    await make_user("alice")
    bot_id = await make_bot("otto", dm_open=True)
    await login(client, "alice")
    dm = (await client.post("/dms", json={"bot_id": bot_id})).json()["id"]
    await login(client, "root")
    await client.patch(f"/bots/{bot_id}", json={"dm_open": False})

    await login(client, "alice")
    assert (await client.post("/dms", json={"bot_id": bot_id})).status_code == 404
    assert dm in {c["id"] for c in (await client.get("/channels")).json()}
    r = await client.post(f"/channels/{dm}/messages", json={"content": "still here"})
    assert r.status_code == 200


async def test_a_person_to_person_dm_still_cannot_be_left(client):
    await make_user("alice")
    bob = await make_user("bob")
    await login(client, "alice")
    dm = (await client.post("/dms", json={"user_id": bob})).json()["id"]
    assert (await client.post(f"/channels/{dm}/leave")).status_code == 400


# ---------------------------------------------------------------------------
# Fan-out
# ---------------------------------------------------------------------------

def set_mode(wsc, channel_id, bot_id, mode):
    call(
        wsc,
        db.execute,
        "UPDATE channel_members SET reply_mode = ? WHERE channel_id = ? AND member_type = 'bot' AND member_id = ?",
        (mode, channel_id, bot_id),
    )


def test_an_always_bot_is_addressed_by_every_person_but_never_by_a_bot(wsc):
    a = ws_make_user(wsc, "alice")
    ta = ws_login(wsc, "alice")
    always = ws_make_bot(wsc, "claudette", BOT_KEY)
    named = ws_make_bot(wsc, "otto", BOT2_KEY)
    main = ws_main_feed_id(wsc)
    add_member(wsc, main, "bot", always)
    add_member(wsc, main, "bot", named)
    set_mode(wsc, main, always, "always")

    with ExitStack() as stack:
        open_user(stack, wsc, ta, a)
        wal = open_bot(stack, wsc, always, BOT_KEY)
        wot = open_bot(stack, wsc, named, BOT2_KEY)

        post_msg(wsc, ta, main, "good morning")
        assert wal.receive_json()["context"]["addressed_by"] == "always"
        assert "context" not in wot.receive_json()

        post_msg(wsc, ta, main, "otto, thoughts?")
        assert wal.receive_json()["context"]["addressed_by"] == "always"
        assert wot.receive_json()["context"]["addressed_by"] == "mention"

        post_msg(wsc, ta, main, "claudette, thoughts?")
        assert wal.receive_json()["context"]["addressed_by"] == "mention"
        assert "context" not in wot.receive_json()

        r = wsc.post(
            f"/channels/{main}/messages", json={"content": "beep"}, headers={"X-Api-Key": BOT2_KEY}
        )
        assert r.status_code == 200, r.text
        bot_said = wal.receive_json()
        assert bot_said["message"]["author_type"] == "bot"
        assert "context" not in bot_said


def test_member_update_reaches_the_rooms_people_and_the_bot(wsc):
    a = ws_make_user(wsc, "alice")
    ta = ws_login(wsc, "alice")
    bot_id = ws_make_bot(wsc, "claudette", BOT_KEY)
    main = ws_main_feed_id(wsc)
    add_member(wsc, main, "bot", bot_id)

    with ExitStack() as stack:
        wa = open_user(stack, wsc, ta, a)
        wbot = open_bot(stack, wsc, bot_id, BOT_KEY)
        r = wsc.patch(
            f"/channels/{main}/bots/{bot_id}", json={"reply_mode": "always"}, headers=cookie(ta)
        )
        assert r.status_code == 200, r.text
        for ws in (wa, wbot):
            frame = ws.receive_json()
            assert frame["type"] == "member_update"
            assert frame["member_id"] == bot_id and frame["reply_mode"] == "always"
            assert frame["by_user_id"] == a


def test_a_mention_only_room_never_addresses_an_always_row(wsc, monkeypatch):
    a = ws_make_user(wsc, "alice")
    ta = ws_login(wsc, "alice")
    bot_id = ws_make_bot(wsc, "claudette", BOT_KEY)
    main = ws_main_feed_id(wsc)
    add_member(wsc, main, "bot", bot_id)
    set_mode(wsc, main, bot_id, "always")
    monkeypatch.setenv("MENTION_ONLY_CHANNEL_IDS", json.dumps([main]))
    reset_settings_cache()

    with ExitStack() as stack:
        open_user(stack, wsc, ta, a)
        wbot = open_bot(stack, wsc, bot_id, BOT_KEY)
        post_msg(wsc, ta, main, "good morning")
        assert "context" not in wbot.receive_json()
        post_msg(wsc, ta, main, "claudette?")
        assert wbot.receive_json()["context"]["addressed_by"] == "mention"


def test_an_always_row_in_custodian_is_never_addressed_without_its_name(wsc):
    a = ws_make_user(wsc, "alice")
    ta = ws_login(wsc, "alice")
    bot_id = ws_make_bot(wsc, "claudette", BOT_KEY)
    r = wsc.post("/channels", json={"name": "custodian"}, headers=cookie(ta))
    custodian = r.json()["id"]
    add_member(wsc, custodian, "bot", bot_id)
    set_mode(wsc, custodian, bot_id, "always")

    with ExitStack() as stack:
        open_user(stack, wsc, ta, a)
        wbot = open_bot(stack, wsc, bot_id, BOT_KEY)
        post_msg(wsc, ta, custodian, "good morning")
        assert "context" not in wbot.receive_json()


def test_a_secret_in_an_always_room_reaches_no_bot(wsc):
    a = ws_make_user(wsc, "alice")
    ta = ws_login(wsc, "alice")
    bot_id = ws_make_bot(wsc, "claudette", BOT_KEY)
    main = ws_main_feed_id(wsc)
    add_member(wsc, main, "bot", bot_id)
    set_mode(wsc, main, bot_id, "always")

    with ExitStack() as stack:
        open_user(stack, wsc, ta, a)
        wbot = open_bot(stack, wsc, bot_id, BOT_KEY)
        secret = post_msg(wsc, ta, main, "don't tell anyone, claudette")
        assert secret["privacy_flags"] == {"secret": True}
        post_msg(wsc, ta, main, "off the record: the plan")
        post_msg(wsc, ta, main, "all clear")
        frame = wbot.receive_json()
        assert frame["message"]["content"] == "all clear"
        assert frame["context"]["addressed_by"] == "always"


def test_every_context_block_says_how_the_bot_was_addressed(wsc):
    a = ws_make_user(wsc, "alice")
    ta = ws_login(wsc, "alice")
    builder = ws_make_bot(wsc, "gable", BOT_KEY)
    main = ws_main_feed_id(wsc)
    add_member(wsc, main, "bot", builder)
    build = call(
        wsc, db.execute,
        "INSERT INTO channels (type, name, visibility, created_by) VALUES ('app_build', 'App', 'private', ?)",
        (a,),
    ).lastrowid
    add_member(wsc, build, "user", a)
    add_member(wsc, build, "bot", builder)
    call(
        wsc, db.execute,
        "INSERT INTO apps (id, owner_user_id, name, builder_bot_id) VALUES ('abcdefghijkl', ?, 'App', ?)",
        (a, builder),
    )
    call(
        wsc, db.execute,
        """INSERT INTO app_sessions (app_id, user_id, builder_bot_id, channel_id, locked_until)
           VALUES ('abcdefghijkl', ?, ?, ?, '2999-01-01T00:00:00.000Z')""",
        (a, builder, build),
    )

    with ExitStack() as stack:
        open_user(stack, wsc, ta, a)
        wbot = open_bot(stack, wsc, builder, BOT_KEY)
        post_msg(wsc, ta, main, "gable, a word")
        assert wbot.receive_json()["context"]["addressed_by"] == "mention"
        set_mode(wsc, main, builder, "always")
        post_msg(wsc, ta, main, "a word for the room")
        assert wbot.receive_json()["context"]["addressed_by"] == "always"
        post_msg(wsc, ta, build, "make me a to-do list")
        assert wbot.receive_json()["context"]["addressed_by"] == "always"
