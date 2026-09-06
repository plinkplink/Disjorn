"""APPS stage 1 — the registry, the sessions, the walls, the stage stream.

SPECS/2026-08-30-apps-tab-v1.md (confirmed by plink, seq 2211; stage 1 opened
at Round 12).

Four claims carry the rest and are worth naming before the code:

1. **A build chat is a real channel, walled by the real wall.** The tests do
   not check that this router refuses a stranger; they check that the ORDINARY
   message routes do, because that is the wall that would actually be reached.
2. **The summon is server-attested, not name-matched.** In an `app_build`
   channel the member bot gets the `context` block for every USER message and
   for no bot message — the resident integration is that fact and nothing else.
3. **The stage stream reaches the owner and nobody else.** Not the builder, not
   another user. "Nobody else" is asserted with a sentinel: trigger a visible
   event afterwards and require it to be that connection's NEXT frame.
4. **Nothing prints a model string this codebase knows.** The builder card's
   model is read from the seat's own file at request time; a missing file is
   answered with null, never with a guess.

The WS claims use test_ws.py's sync TestClient + portal idiom, for the same
reason it does: one event loop shared by REST calls and sockets, so after a
REST post returns, all fan-out frames are already buffered.
"""

import json
import re
import sqlite3
from contextlib import ExitStack
from datetime import datetime, time as dt_time, timedelta, timezone

import pytest
from starlette.testclient import TestClient

from app import db, events
from app.config import reset_settings_cache
from app.routers import auth

PASSWORD = "correct horse battery staple"
PASSWORD_HASH = auth.hash_password(PASSWORD)  # hashed once — argon2 is slow
BUILDER_KEY = "apps-builder-key"
BROKER_KEY = "apps-broker-key"

MODEL_PIN = "claude-test-model-from-the-seat"


# ---------------------------------------------------------------------------
# Fixtures + helpers
# ---------------------------------------------------------------------------

@pytest.fixture
def seat_toml(tmp_path):
    """A builder seat's config file, the way a real seat declares its model."""
    path = tmp_path / "seat.toml"
    path.write_text(f'# a seat\nmodel = "{MODEL_PIN}"\n\n[other]\nmodel = "not-it"\n')
    return path


@pytest.fixture
def settings_env(monkeypatch):
    """Set APPS_* settings mid-test.

    get_settings() is read per request, so resetting its cache after the app
    fixture has booted is enough — every handler picks the new values up on the
    next call. The boot-critical values (COOKIE_SECURE, HOUSE_ORIGINS) stay
    pinned by conftest's env, so a reset never un-configures the app.
    """

    def apply(**values) -> None:
        for key, value in values.items():
            monkeypatch.setenv(
                key, value if isinstance(value, str) else json.dumps(value)
            )
        reset_settings_cache()

    yield apply
    reset_settings_cache()


async def make_user(username: str, display_name: str | None = None,
                    *, admin: bool = False) -> int:
    cur = await db.execute(
        "INSERT INTO users (username, password_hash, display_name, is_admin) "
        "VALUES (?, ?, ?, ?)",
        (username, PASSWORD_HASH, display_name or username.capitalize(),
         1 if admin else 0),
    )
    return cur.lastrowid


async def make_bot(name: str, api_key: str = BUILDER_KEY) -> int:
    cur = await db.execute(
        "INSERT INTO bots (name, api_key_hash) VALUES (?, ?)",
        (name, auth.hash_api_key(api_key)),
    )
    return cur.lastrowid


async def login(client, username: str) -> None:
    r = await client.post(
        "/auth/login", json={"username": username, "password": PASSWORD}
    )
    assert r.status_code == 200, r.text


def as_bot(client, api_key: str) -> dict:
    """Bot auth on the shared client.

    The cookie jar has to go first: get_actor prefers a session cookie over the
    API key, so a leftover login would silently turn a bot request into that
    user's request — and the test would then be asserting nothing.
    """
    client.cookies.clear()
    return {"X-Api-Key": api_key}


async def start_session(client, builder_bot_id: int, app_id: str | None = None):
    body: dict = {"builder_bot_id": builder_bot_id}
    if app_id is not None:
        body["app_id"] = app_id
    return await client.post("/apps/sessions", json=body)


def next_utc_midnight() -> str:
    tomorrow = datetime.now(timezone.utc).date() + timedelta(days=1)
    return (
        datetime.combine(tomorrow, dt_time.min, tzinfo=timezone.utc)
        .strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3]
        + "Z"
    )


async def channel_members(channel_id: int) -> set[tuple[str, int]]:
    rows = await db.fetch_all(
        "SELECT member_type, member_id FROM channel_members WHERE channel_id = ?",
        (channel_id,),
    )
    return {(r["member_type"], r["member_id"]) for r in rows}


# ---------------------------------------------------------------------------
# 1. Migration
# ---------------------------------------------------------------------------

async def test_migration_widens_the_channel_check_and_adds_the_registry(app):
    """`app_build` is a channel type now, the five tables exist, and the CHECK
    is still a CHECK — a widened constraint that accepts anything would pass a
    test that only tried the new value."""
    cur = await db.execute(
        "INSERT INTO channels (type, name, visibility) "
        "VALUES ('app_build', 'Untitled app', 'private')"
    )
    row = await db.fetch_one("SELECT * FROM channels WHERE id = ?", (cur.lastrowid,))
    assert row["type"] == "app_build"
    assert row["visibility"] == "private"

    with pytest.raises(sqlite3.IntegrityError):
        await db.execute("INSERT INTO channels (type) VALUES ('app_serve')")

    names = {
        r["name"]
        for r in await db.fetch_all(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
    }
    assert {
        "apps", "app_shares", "user_apps", "app_sessions", "app_stage_events"
    } <= names

    # The rebuild kept the partial unique index text channels rely on.
    await db.execute("INSERT INTO channels (type, name) VALUES ('text', 'shed')")
    with pytest.raises(sqlite3.IntegrityError):
        await db.execute("INSERT INTO channels (type, name) VALUES ('text', 'shed')")

    # Five stages and no sixth.
    with pytest.raises(sqlite3.IntegrityError):
        await db.execute(
            "INSERT INTO app_stage_events (session_id, stage) VALUES (1, 'nearly')"
        )


# ---------------------------------------------------------------------------
# 2. Session create
# ---------------------------------------------------------------------------

async def test_session_create_builds_app_channel_members_and_one_opener(
    client, app, settings_env, seat_toml
):
    uid = await make_user("alice", "Alice")
    builder = await make_bot("gable")
    settings_env(APPS_BUILDERS=[{"bot_id": builder, "model_source": str(seat_toml)}])
    await login(client, "alice")

    r = await start_session(client, builder)
    assert r.status_code == 200, r.text
    body = r.json()

    # The app: born private, draft, named, owned, and never sequentially id'd.
    app_out = body["app"]
    assert re.fullmatch(r"[a-z2-7]{12}", app_out["id"])
    assert app_out["name"] == "Untitled app"
    assert app_out["description"] == ""
    assert app_out["visibility"] == "private"
    assert app_out["status"] == "draft"
    assert app_out["owner_user_id"] == uid
    assert app_out["builder_bot_id"] == builder
    assert app_out["parent_app_id"] is None
    assert app_out["on_menu"] is True
    assert app_out["open_session"]["id"] == body["id"]
    assert app_out["open_session"]["channel_id"] == body["channel_id"]
    assert app_out["open_session"]["stage"] is None

    # The session envelope.
    assert body["builder"] == {
        "bot_id": builder,
        "name": "gable",
        "avatar_url": None,
        "model": MODEL_PIN,
        "builds_total": 1,
        "builds_live": 0,
    }
    assert body["stages"] == []
    assert body["stage"] is None
    assert body["ended_at"] is None
    assert body["locked_until"] > body["started_at"]
    assert body["quota"] == {
        "cap": 3, "used": 1, "left": 2, "resets_at": next_utc_midnight()
    }

    # The channel: type, name, privacy, and EXACTLY two members.
    channel = await db.fetch_one(
        "SELECT * FROM channels WHERE id = ?", (body["channel_id"],)
    )
    assert channel["type"] == "app_build"
    assert channel["name"] == "Untitled app"
    assert channel["visibility"] == "private"
    assert channel["created_by"] == uid
    assert await channel_members(body["channel_id"]) == {("user", uid), ("bot", builder)}

    # Exactly one message, authored by the seeded `system` bot (D8) — not by
    # the builder, whose first reply is its own personalized open.
    messages = await db.fetch_all(
        "SELECT * FROM messages WHERE channel_id = ?", (body["channel_id"],)
    )
    assert len(messages) == 1
    system = await db.fetch_one("SELECT id FROM bots WHERE name = 'system'")
    assert messages[0]["author_type"] == "bot"
    assert messages[0]["author_id"] == system["id"]
    content = messages[0]["content"]
    assert "«Untitled app»" in content
    assert "Alice" in content and "gable" in content
    assert "2 of 3 builds left today after this one." in content


async def test_session_create_refuses_an_unconfigured_builder(
    client, app, settings_env
):
    await make_user("alice")
    stranger = await make_bot("otto")
    settings_env(APPS_BUILDERS=[])
    await login(client, "alice")

    r = await start_session(client, stranger)
    assert r.status_code == 400
    # Flat sentence, and it never repeats what the caller sent.
    assert isinstance(r.json()["detail"], str)
    assert str(stranger) not in r.json()["detail"]
    assert await db.fetch_all("SELECT * FROM apps") == []


# ---------------------------------------------------------------------------
# 3. Quota
# ---------------------------------------------------------------------------

async def test_quota_caps_sessions_per_user_per_utc_day(
    client, app, settings_env, seat_toml
):
    await make_user("alice", "Alice")
    await make_user("bob", "Bob")
    builder = await make_bot("gable")
    settings_env(
        APPS_DAILY_SESSION_CAP="2",
        APPS_BUILDERS=[{"bot_id": builder, "model_source": str(seat_toml)}],
    )

    await login(client, "alice")
    assert (await start_session(client, builder)).status_code == 200
    assert (await start_session(client, builder)).status_code == 200

    r = await start_session(client, builder)
    assert r.status_code == 429
    assert r.json()["detail"] == (
        "You have used all 2 app builds for today; "
        "the meter resets at midnight UTC."
    )

    meter = (await client.get("/apps/quota")).json()
    assert meter == {"cap": 2, "used": 2, "left": 0, "resets_at": next_utc_midnight()}

    # The cap is per user, not per house.
    await login(client, "bob")
    assert (await client.get("/apps/quota")).json()["used"] == 0
    assert (await start_session(client, builder)).status_code == 200

    # Yesterday's sessions do not count against today's meter (D5 counts from
    # started_at, so this is the whole reset mechanism).
    await login(client, "alice")
    await db.execute(
        "UPDATE app_sessions SET started_at = '2020-01-01T00:00:00.000Z' "
        "WHERE user_id = (SELECT id FROM users WHERE username = 'alice')"
    )
    assert (await client.get("/apps/quota")).json() == {
        "cap": 2, "used": 0, "left": 2, "resets_at": next_utc_midnight()
    }


# ---------------------------------------------------------------------------
# 4. The session lock
# ---------------------------------------------------------------------------

async def test_lock_blocks_a_second_session_heartbeats_and_lapses(
    client, app, settings_env, seat_toml
):
    await make_user("alice", "Alice")
    builder = await make_bot("gable")
    settings_env(
        APPS_DAILY_SESSION_CAP="50",
        APPS_SESSION_LOCK_TTL="900",
        APPS_BUILDERS=[{"bot_id": builder, "model_source": str(seat_toml)}],
    )
    await login(client, "alice")

    first = (await start_session(client, builder)).json()
    app_id = first["app"]["id"]

    # A live lock refuses a second session on the SAME app...
    blocked = await start_session(client, builder, app_id=app_id)
    assert blocked.status_code == 409
    assert isinstance(blocked.json()["detail"], str)

    # ...and does not refuse a brand new app.
    assert (await start_session(client, builder)).status_code == 200

    # Heartbeat pushes locked_until out. Deterministic without sleeping: raise
    # the TTL, beat once, and the new deadline must be past the old one.
    settings_env(APPS_SESSION_LOCK_TTL="7200")
    beat = await client.post(f"/apps/sessions/{first['id']}/heartbeat")
    assert beat.status_code == 200
    assert beat.json()["locked_until"] > first["locked_until"]
    row = await db.fetch_one(
        "SELECT locked_until FROM app_sessions WHERE id = ?", (first["id"],)
    )
    assert row["locked_until"] == beat.json()["locked_until"]

    # An explicit end clears the lock, and is idempotent.
    ended = await client.post(f"/apps/sessions/{first['id']}/end")
    assert ended.status_code == 200
    again = await client.post(f"/apps/sessions/{first['id']}/end")
    assert again.json()["ended_at"] == ended.json()["ended_at"]

    assert (await start_session(client, builder, app_id=app_id)).status_code == 200


async def test_a_lapsed_lock_reads_as_clear_and_as_ended(
    client, app, settings_env, seat_toml
):
    """No sweeper: a lock in the past is over the instant anyone looks. If
    correctness needed a timer to have run, an outage would strand every app."""
    await make_user("alice", "Alice")
    builder = await make_bot("gable")
    settings_env(
        APPS_DAILY_SESSION_CAP="50",
        APPS_BUILDERS=[{"bot_id": builder, "model_source": str(seat_toml)}],
    )
    await login(client, "alice")

    session = (await start_session(client, builder)).json()
    app_id = session["app"]["id"]
    await db.execute(
        "UPDATE app_sessions SET locked_until = '2000-01-01T00:00:00.000Z' WHERE id = ?",
        (session["id"],),
    )

    # Clear: a new session on the app is allowed.
    assert (await start_session(client, builder, app_id=app_id)).status_code == 200

    # Ended: the lapsed session refuses writes with 410, and no reader reports
    # it as the app's open session.
    beat = await client.post(f"/apps/sessions/{session['id']}/heartbeat")
    assert beat.status_code == 410
    assert beat.json()["detail"] == "This build session has ended"

    lapsed = (await client.get(f"/apps/sessions/{session['id']}")).json()
    assert lapsed["app"]["open_session"]["id"] != session["id"]


# ---------------------------------------------------------------------------
# 5. The membership wall (through the ordinary message routes)
# ---------------------------------------------------------------------------

async def test_a_third_user_cannot_read_or_post_in_a_build_chat(
    client, app, settings_env, seat_toml
):
    await make_user("alice", "Alice")
    await make_user("carol", "Carol")
    builder = await make_bot("gable", BUILDER_KEY)
    settings_env(APPS_BUILDERS=[{"bot_id": builder, "model_source": str(seat_toml)}])

    await login(client, "alice")
    session = (await start_session(client, builder)).json()
    channel_id = session["channel_id"]

    # Carol is in the house, and that buys her nothing here.
    await login(client, "carol")
    assert (await client.get(f"/channels/{channel_id}/messages")).status_code == 403
    assert (
        await client.post(f"/channels/{channel_id}/messages", json={"content": "hi"})
    ).status_code == 403
    assert (await client.get(f"/channels/{channel_id}/members")).status_code == 403
    # Nor may she reach the session itself.
    assert (await client.get(f"/apps/sessions/{session['id']}")).status_code == 403

    # The builder bot, an explicit member, may do all of it.
    headers = as_bot(client, BUILDER_KEY)
    assert (
        await client.get(f"/channels/{channel_id}/messages", headers=headers)
    ).status_code == 200
    assert (
        await client.post(
            f"/channels/{channel_id}/messages",
            json={"content": "on it"},
            headers=headers,
        )
    ).status_code == 200


# ---------------------------------------------------------------------------
# 8. Menu + discover
# ---------------------------------------------------------------------------

async def test_menu_and_discover_follow_visibility(
    client, app, settings_env, seat_toml
):
    alice = await make_user("alice", "Alice")
    await make_user("bob", "Bob")
    builder = await make_bot("gable")
    settings_env(
        APPS_DAILY_SESSION_CAP="50",
        APPS_BUILDERS=[{"bot_id": builder, "model_source": str(seat_toml)}],
    )

    await login(client, "alice")
    public_app = (await start_session(client, builder)).json()["app"]["id"]
    private_app = (await start_session(client, builder)).json()["app"]["id"]
    # Visibility has no stage-1 write path (the share dialog is stage 3), so
    # the fixture sets it the way stage 3 will.
    await db.execute("UPDATE apps SET visibility = 'public' WHERE id = ?", (public_app,))

    await login(client, "bob")
    discover = (await client.get("/apps/discover")).json()["apps"]
    assert [a["id"] for a in discover] == [public_app]
    assert discover[0]["on_menu"] is False
    assert (await client.get("/apps")).json()["apps"] == []

    # A private app is refused at the endpoint, not merely hidden in the list.
    refused = await client.post(f"/apps/{private_app}/menu")
    assert refused.status_code == 403
    assert refused.json()["detail"] == "This app has not been shared with you"

    added = await client.post(f"/apps/{public_app}/menu")
    assert added.status_code == 200
    assert added.json()["on_menu"] is True
    assert [a["id"] for a in (await client.get("/apps")).json()["apps"]] == [public_app]
    # Once it is on the menu the chooser stops offering it.
    assert (await client.get("/apps/discover")).json()["apps"] == []

    assert (await client.delete(f"/apps/{public_app}/menu")).status_code == 200
    assert (await client.get("/apps")).json()["apps"] == []

    # Sharing without publishing: bob sees it only after an app_shares row.
    await db.execute(
        "INSERT INTO app_shares (app_id, user_id) VALUES (?, "
        "(SELECT id FROM users WHERE username = 'bob'))",
        (private_app,),
    )
    # Both are offerable now: the private one because it was shared, the public
    # one because it came back off the menu a moment ago. Newest first.
    assert [a["id"] for a in (await client.get("/apps/discover")).json()["apps"]] == [
        private_app,
        public_app,
    ]

    # The owner's own app is on their menu by construction, so removing it is a
    # refusal rather than a no-op that reports success.
    await login(client, "alice")
    mine = (await client.get("/apps")).json()
    assert {a["id"] for a in mine["apps"]} == {public_app, private_app}
    assert all(a["on_menu"] for a in mine["apps"])
    assert mine["quota"]["used"] == 2
    removed = await client.delete(f"/apps/{public_app}/menu")
    assert removed.status_code == 400
    assert removed.json()["detail"] == "You own this app, so it stays in your apps list"
    assert alice == mine["apps"][0]["owner_user_id"]


async def test_patch_renames_within_bounds_and_notifies_the_owner(
    client, app, settings_env, seat_toml
):
    await make_user("alice", "Alice")
    builder = await make_bot("gable")
    settings_env(APPS_BUILDERS=[{"bot_id": builder, "model_source": str(seat_toml)}])
    await login(client, "alice")
    session = (await start_session(client, builder)).json()
    app_id = session["app"]["id"]

    seen: list[dict] = []
    events.subscribe(lambda e: seen.append(e) if e["type"] == "app_update" else None)

    r = await client.patch(f"/apps/{app_id}", json={"name": "Book club planner"})
    assert r.status_code == 200
    assert r.json()["name"] == "Book club planner"
    assert r.json()["updated_at"] >= session["app"]["updated_at"]
    assert [e["app"]["name"] for e in seen] == ["Book club planner"]
    assert seen[0]["owner_user_id"] == r.json()["owner_user_id"]

    # Bounds (D10) are refused before anything is written, with a flat sentence
    # that does not echo the submitted string.
    long_name = await client.patch(f"/apps/{app_id}", json={"name": "x" * 61})
    assert long_name.status_code == 422
    assert "x" * 61 not in long_name.text
    assert (
        await client.patch(f"/apps/{app_id}", json={"description": "y" * 301})
    ).status_code == 422
    assert (await client.patch(f"/apps/{app_id}", json={})).status_code == 400
    assert (await client.patch(f"/apps/{app_id}", json={"name": "   "})).status_code == 400

    # Someone else's app is not theirs to rename.
    await make_user("bob", "Bob")
    await login(client, "bob")
    assert (
        await client.patch(f"/apps/{app_id}", json={"name": "mine now"})
    ).status_code == 403
    # An id that could never have been minted is a 404, not a 400 quoting it.
    assert (await client.patch("/apps/../../etc", json={"name": "x"})).status_code == 404


# ---------------------------------------------------------------------------
# 9. Builders — the model is printed from the seat, never from here
# ---------------------------------------------------------------------------

async def test_builders_print_the_model_the_seat_declares(
    client, app, settings_env, seat_toml, tmp_path
):
    await make_user("alice", "Alice")
    gable = await make_bot("gable", BUILDER_KEY)
    claudette = await make_bot("claudette", BROKER_KEY)
    silent = await make_bot("silent")

    env_seat = tmp_path / "seat.env"
    env_seat.write_text("FOO=1\nMODEL=fallback\nCHAT_MODEL=claude-from-an-env-file\n")
    missing = tmp_path / "not-written-yet.toml"
    # The shape Gable's live seat actually has: the pin under a table, named by
    # a dotted model_key. A wrong key is "not declared", never a guess.
    nested = tmp_path / "nested.toml"
    nested.write_text('[server]\nurl = "x"\n\n[container]\nmodel = "claude-nested"\n')
    wrong_key = await make_bot("wrongkey")
    nested_bot = await make_bot("nested")

    settings_env(
        APPS_BUILDERS=[
            {"bot_id": gable, "model_source": str(seat_toml)},
            {"bot_id": claudette, "model_source": str(env_seat)},
            {"bot_id": silent, "model_source": str(missing)},
            {"bot_id": nested_bot, "model_source": str(nested), "model_key": "container.model"},
            {"bot_id": wrong_key, "model_source": str(nested), "model_key": "container.nope"},
            # Configured but not in the bots table: omitted, not rendered as a
            # card nobody can pick.
            {"bot_id": 9999, "model_source": str(seat_toml)},
        ]
    )
    await login(client, "alice")

    cards = (await client.get("/apps/builders")).json()
    assert [c["bot_id"] for c in cards] == [gable, claudette, silent, nested_bot, wrong_key]
    assert cards[0]["model"] == MODEL_PIN
    assert cards[1]["model"] == "claude-from-an-env-file"
    assert cards[2]["model"] is None  # "model not declared by seat"
    assert cards[3]["model"] == "claude-nested"
    assert cards[4]["model"] is None
    assert all(c["builds_total"] == 0 and c["builds_live"] == 0 for c in cards)

    # Build stats come off the registry, and `live` is what counts as live.
    session = (await start_session(client, gable)).json()
    await db.execute(
        "UPDATE apps SET status = 'live' WHERE id = ?", (session["app"]["id"],)
    )
    cards = (await client.get("/apps/builders")).json()
    assert (cards[0]["builds_total"], cards[0]["builds_live"]) == (1, 1)

    # No configured seats is an empty state, not a boot failure.
    settings_env(APPS_BUILDERS=[])
    assert (await client.get("/apps/builders")).json() == []


# ---------------------------------------------------------------------------
# 10. Sidebar contract
# ---------------------------------------------------------------------------

async def test_get_channels_still_lists_the_build_chat_for_its_member(
    client, app, settings_env, seat_toml
):
    """D9: the server keeps returning the row so reconnect resync and unread
    bookkeeping keep working; the CLIENT is what filters it out of the visible
    groups. A bot member must not break the DM-partner logic."""
    await make_user("alice", "Alice")
    await make_user("carol", "Carol")
    builder = await make_bot("gable")
    settings_env(APPS_BUILDERS=[{"bot_id": builder, "model_source": str(seat_toml)}])

    await login(client, "alice")
    session = (await start_session(client, builder)).json()

    rows = (await client.get("/channels")).json()
    row = next(c for c in rows if c["id"] == session["channel_id"])
    assert row["type"] == "app_build"
    assert row["name"] == "Untitled app"     # the app's name, not a DM partner
    assert row["dm_user_id"] is None
    assert row["member"] is True
    assert row["visibility"] == "private"
    assert row["last_message"]["author_type"] == "bot"   # the system opener

    # Not in anyone else's sidebar — not even an admin's bare-row view, which
    # private TEXT channels get and build chats deliberately do not.
    await login(client, "carol")
    assert all(c["id"] != session["channel_id"] for c in (await client.get("/channels")).json())


# ---------------------------------------------------------------------------
# 6 + 7. WebSocket claims (sync TestClient + portal, as in test_ws.py)
# ---------------------------------------------------------------------------

@pytest.fixture
def wsc(tmp_db_path, house_origin):
    """Sync TestClient with lifespan running (portal loop shared by REST + WS)."""
    import asyncio

    events.clear_subscribers()
    asyncio.run(db.close())  # drop any leaked connection to another tmp DB

    from app.main import create_app

    with TestClient(create_app(), headers={"Origin": house_origin}) as client:
        yield client
    events.clear_subscribers()


def call(wsc, coro_fn, *args):
    return wsc.portal.call(coro_fn, *args)


def sync_user(wsc, username, display_name=None):
    cur = call(
        wsc,
        db.execute,
        "INSERT INTO users (username, password_hash, display_name) VALUES (?, ?, ?)",
        (username, PASSWORD_HASH, display_name or username.capitalize()),
    )
    return cur.lastrowid


def sync_bot(wsc, name, api_key=BUILDER_KEY):
    cur = call(
        wsc,
        db.execute,
        "INSERT INTO bots (name, api_key_hash) VALUES (?, ?)",
        (name, auth.hash_api_key(api_key)),
    )
    return cur.lastrowid


def sync_login(wsc, username):
    r = wsc.post("/auth/login", json={"username": username, "password": PASSWORD})
    assert r.status_code == 200, r.text
    token = r.cookies.get(auth.COOKIE_NAME)
    wsc.cookies.clear()
    assert token
    return token


def cookie(token):
    return {"cookie": f"{auth.COOKIE_NAME}={token}"}


def open_user(stack, wsc, token, user_id, *, peers=()):
    ws = stack.enter_context(wsc.websocket_connect("/ws", headers=cookie(token)))
    assert ws.receive_json() == {"type": "ready", "user_id": user_id}
    expected = {"type": "presence", "user_id": user_id, "status": "online"}
    assert ws.receive_json() == expected
    for peer in peers:
        assert peer.receive_json() == expected
    return ws


def open_bot(stack, wsc, bot_id, api_key=BUILDER_KEY):
    ws = stack.enter_context(wsc.websocket_connect("/ws"))
    ws.send_json({"op": "auth", "api_key": api_key})
    assert ws.receive_json() == {"type": "ready", "bot_id": bot_id}
    return ws


def post_msg(wsc, headers, channel_id, content):
    r = wsc.post(
        f"/channels/{channel_id}/messages", json={"content": content}, headers=headers
    )
    assert r.status_code == 200, r.text
    return r.json()


def make_session(wsc, token, builder_bot_id):
    r = wsc.post(
        "/apps/sessions",
        json={"builder_bot_id": builder_bot_id},
        headers=cookie(token),
    )
    assert r.status_code == 200, r.text
    return r.json()


def main_feed_id(wsc):
    row = call(wsc, db.fetch_one, "SELECT id FROM channels WHERE type = 'main_feed'")
    return row["id"]


def test_context_is_attested_in_a_build_chat_not_name_matched(
    wsc, settings_env, seat_toml
):
    """Claim 6. Two members, so "was I addressed" has one answer and the server
    already knows it — the resident never has to see its own name."""
    alice = sync_user(wsc, "alice", "Alice")
    builder = sync_bot(wsc, "gable", BUILDER_KEY)
    settings_env(APPS_BUILDERS=[{"bot_id": builder, "model_source": str(seat_toml)}])
    token = sync_login(wsc, "alice")

    # Session (and its system opener) BEFORE any socket opens, so there is
    # nothing to drain.
    session = make_session(wsc, token, builder)
    build_channel = session["channel_id"]
    app_id = session["app"]["id"]

    # A plain public channel to prove the old rule is untouched.
    r = wsc.post("/channels", json={"name": "shed"}, headers=cookie(token))
    assert r.status_code == 200, r.text
    shed = r.json()["id"]
    call(
        wsc,
        db.execute,
        "INSERT INTO channel_members (channel_id, member_type, member_id) "
        "VALUES (?, 'bot', ?)",
        (shed, builder),
    )

    # User socket first, bot socket second: presence is broadcast to EVERY
    # socket, so a bot connected before a user would have a presence frame
    # queued ahead of the message this test is about.
    with ExitStack() as stack:
        open_user(stack, wsc, token, alice)
        wbot = open_bot(stack, wsc, builder, BUILDER_KEY)

        # A user message with no name in it: context anyway.
        post_msg(wsc, cookie(token), build_channel, "make me a to-do list")
        frame = wbot.receive_json()
        assert frame["type"] == "message_create"
        assert "gable" not in frame["message"]["content"]
        context = frame["context"]
        assert context["channel_state"]["type"] == "app_build"
        assert context["channel_state"]["name"] == "Untitled app"
        assert context["channel_state"]["app"] == {
            "id": app_id,
            "name": "Untitled app",
            "session_id": session["id"],
            "builder_bot_id": builder,
            "stage": None,
        }
        assert context["awake_users"] == [{"id": alice, "name": "Alice", "status": "online"}]

        # A BOT message in the same channel: never context. Otherwise a
        # two-member room talks to itself forever.
        post_msg(wsc, {"X-Api-Key": BUILDER_KEY}, build_channel, "on it")
        echo = wbot.receive_json()
        assert echo["message"]["author_type"] == "bot"
        assert "context" not in echo

        # A user message in a plain text channel with no name in it: unchanged,
        # still no context.
        post_msg(wsc, cookie(token), shed, "anyone seen the drill")
        plain = wbot.receive_json()
        assert plain["channel_id"] == shed
        assert "context" not in plain

        # And the name rule still works where it always did.
        post_msg(wsc, cookie(token), shed, "gable can you look")
        named = wbot.receive_json()
        assert named["context"]["channel_state"]["type"] == "text"
        assert "app" not in named["context"]["channel_state"]


def test_stage_events_reach_the_owner_alone_and_live_flips_the_app(
    wsc, settings_env, seat_toml
):
    """Claim 7. The stage stream is for the person watching the modal. A
    resident reading the harness's report of its own build back as an inbound
    frame is a loop nobody asked for, and another user seeing it at all is a
    leak of what somebody is building."""
    alice = sync_user(wsc, "alice", "Alice")
    carol = sync_user(wsc, "carol", "Carol")
    builder = sync_bot(wsc, "gable", BUILDER_KEY)
    sync_bot(wsc, "broker", BROKER_KEY)
    settings_env(
        APPS_BUILDERS=[{"bot_id": builder, "model_source": str(seat_toml)}],
        APPS_STAGE_PUBLISHER_BOT_NAMES=["broker"],
    )
    ta = sync_login(wsc, "alice")
    tc = sync_login(wsc, "carol")

    session = make_session(wsc, ta, builder)
    main = main_feed_id(wsc)

    # A user with no admin bit cannot publish an attestation about a build.
    refused = wsc.post(
        f"/apps/sessions/{session['id']}/stage",
        json={"stage": "scoped"},
        headers=cookie(ta),
    )
    assert refused.status_code == 403
    assert refused.json()["detail"] == (
        "Only the build harness or an admin can publish a build stage"
    )

    with ExitStack() as stack:
        wa = open_user(stack, wsc, ta, alice)
        wc = open_user(stack, wsc, tc, carol, peers=[wa])
        wbot = open_bot(stack, wsc, builder, BUILDER_KEY)

        published = wsc.post(
            f"/apps/sessions/{session['id']}/stage",
            json={"stage": "files_written", "detail": {"files": ["index.html"]}},
            headers={"X-Api-Key": BROKER_KEY},
        )
        assert published.status_code == 200, published.text

        assert wa.receive_json() == {
            "type": "app_stage",
            "session_id": session["id"],
            "app_id": session["app"]["id"],
            "stage": "files_written",
            "detail": {"files": ["index.html"]},
            "created_at": published.json()["created_at"],
        }

        # "Nobody else" by sentinel: the next frame each of them sees is the
        # ordinary traffic that follows, so no app_stage arrived in between.
        post_msg(wsc, cookie(ta), main, "sentinel")
        assert wa.receive_json()["message"]["content"] == "sentinel"
        assert wc.receive_json()["message"]["content"] == "sentinel"
        post_msg(wsc, cookie(ta), session["channel_id"], "still here")
        assert wa.receive_json()["message"]["content"] == "still here"
        assert wbot.receive_json()["message"]["content"] == "still here"

        # `live` is the app's done state, so it moves the registry too.
        live = wsc.post(
            f"/apps/sessions/{session['id']}/stage",
            json={"stage": "live"},
            headers={"X-Api-Key": BROKER_KEY},
        )
        assert live.status_code == 200
        assert wa.receive_json()["stage"] == "live"

    row = call(
        wsc, db.fetch_one, "SELECT status FROM apps WHERE id = ?", (session["app"]["id"],)
    )
    assert row["status"] == "live"

    # Persisted before published: a modal that reloads sees the same history.
    reloaded = wsc.get(f"/apps/sessions/{session['id']}", headers=cookie(ta)).json()
    assert [s["stage"] for s in reloaded["stages"]] == ["files_written", "live"]
    assert reloaded["stages"][0]["detail"] == {"files": ["index.html"]}
    assert reloaded["stage"] == "live"

    # A finished-by-lock session refuses further stages with 410.
    call(
        wsc,
        db.execute,
        "UPDATE app_sessions SET locked_until = '2000-01-01T00:00:00.000Z' WHERE id = ?",
        (session["id"],),
    )
    stale = wsc.post(
        f"/apps/sessions/{session['id']}/stage",
        json={"stage": "deployed"},
        headers={"X-Api-Key": BROKER_KEY},
    )
    assert stale.status_code == 410
