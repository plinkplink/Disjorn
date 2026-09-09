"""APPS — the registry, the sessions, the walls, the stage stream, the turn.

SPECS/2026-08-30-apps-tab-v1.md (confirmed by plink, seq 2211; stage 1 opened
at Round 12) and SPECS/2026-09-06-apps-builder-seat.md §J (confirmed seq 2302;
stage 2 slice (ii) is section 11 at the end of this file).

Four claims carry stage 1 and are worth naming before the code:

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


async def test_the_owner_cannot_change_who_is_in_a_build_chat(
    client, app, settings_env, seat_toml
):
    """Claim 5b (Claudette's review block). The owner is `created_by` on the
    room, and the private-channel owner rule would otherwise let them add any
    bot in the house — where ws.py summons bots without a name match — or evict
    the builder mid-build. Membership is fixed at creation, full stop."""
    await make_user("alice", "Alice")
    carol = await make_user("carol", "Carol")
    builder = await make_bot("gable", BUILDER_KEY)
    other_bot = await make_bot("claudette", BROKER_KEY)
    settings_env(APPS_BUILDERS=[{"bot_id": builder, "model_source": str(seat_toml)}])

    await login(client, "alice")
    session = (await start_session(client, builder)).json()
    channel_id = session["channel_id"]

    refusals = [
        lambda: client.post(f"/channels/{channel_id}/bots", json={"bot_id": other_bot}),
        lambda: client.delete(f"/channels/{channel_id}/bots/{builder}"),
        lambda: client.post(f"/channels/{channel_id}/invite", json={"user_id": carol}),
        lambda: client.post(f"/channels/{channel_id}/kick", json={"user_id": carol}),
        lambda: client.post(f"/channels/{channel_id}/leave"),
    ]
    for call_verb in refusals:
        r = await call_verb()
        assert r.status_code == 403, r.text
        # The build-chat sentence, not the generic "no membership list" one:
        # the refusal has to say what is true about THIS room.
        assert "fixed" in r.json()["detail"]

    # Nothing moved: still exactly the owner and the builder.
    rows = await db.fetch_all(
        "SELECT member_type, member_id FROM channel_members WHERE channel_id = ? "
        "ORDER BY member_type",
        (channel_id,),
    )
    assert [(r["member_type"], r["member_id"]) for r in rows] == [
        ("bot", builder),
        ("user", session["app"]["owner_user_id"]),
    ]


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


def test_only_the_builder_is_summoned_even_if_another_bot_is_in_the_room(
    wsc, settings_env, seat_toml
):
    """Claim 6b (Claudette's review block). channels.py refuses to add a bot to
    a build chat; this is the second wall for the day that one moves. A bot
    present by any other path still receives the room's traffic as a member,
    but the server-attested context — the summons — goes to the session's
    builder_bot_id and nobody else."""
    alice = sync_user(wsc, "alice", "Alice")
    builder = sync_bot(wsc, "gable", BUILDER_KEY)
    stowaway = sync_bot(wsc, "claudette", BROKER_KEY)
    settings_env(APPS_BUILDERS=[{"bot_id": builder, "model_source": str(seat_toml)}])
    token = sync_login(wsc, "alice")

    session = make_session(wsc, token, builder)
    build_channel = session["channel_id"]
    # Smuggle a second bot in below the API, the only way left.
    call(
        wsc,
        db.execute,
        "INSERT INTO channel_members (channel_id, member_type, member_id) "
        "VALUES (?, 'bot', ?)",
        (build_channel, stowaway),
    )

    with ExitStack() as stack:
        open_user(stack, wsc, token, alice)
        wbuilder = open_bot(stack, wsc, builder, BUILDER_KEY)
        wstow = open_bot(stack, wsc, stowaway, BROKER_KEY)

        post_msg(wsc, cookie(token), build_channel, "make me a to-do list")

        summoned = wbuilder.receive_json()
        assert summoned["type"] == "message_create"
        assert summoned["context"]["channel_state"]["app"]["builder_bot_id"] == builder

        seen = wstow.receive_json()
        assert seen["type"] == "message_create"
        assert seen["channel_id"] == build_channel
        assert "context" not in seen


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

        # …and then the §H turn line, which is a MESSAGE and therefore reaches
        # both members of the room rather than the owner alone (stage 2 §H).
        # It carries no `context`: it is the `system` bot's, so it summons
        # nobody, which is what keeps a report of a build from starting one.
        for member in (wa, wbot):
            line = member.receive_json()
            assert line["type"] == "message_create"
            assert line["message"]["content"].startswith("Turn 0 done — 1 file")
            assert "context" not in line

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

    # A lapsed lock does NOT close the door on the harness (D-B3): the lock is
    # the user's chat exclusivity, and a turn that ran has to be reportable
    # even though the modal went away. An ENDED session does.
    call(
        wsc,
        db.execute,
        "UPDATE app_sessions SET locked_until = '2000-01-01T00:00:00.000Z' WHERE id = ?",
        (session["id"],),
    )
    lapsed = wsc.post(
        f"/apps/sessions/{session['id']}/stage",
        json={"stage": "deployed"},
        headers={"X-Api-Key": BROKER_KEY},
    )
    assert lapsed.status_code == 200, lapsed.text
    call(
        wsc,
        db.execute,
        "UPDATE app_sessions SET ended_at = '2000-01-01T00:00:00.000Z' WHERE id = ?",
        (session["id"],),
    )
    stale = wsc.post(
        f"/apps/sessions/{session['id']}/stage",
        json={"stage": "deployed"},
        headers={"X-Api-Key": BROKER_KEY},
    )
    assert stale.status_code == 410


# ---------------------------------------------------------------------------
# 11. Stage 2 — a turn (SPECS/2026-09-06-apps-builder-seat.md §J, slice ii)
# ---------------------------------------------------------------------------
#
# Three claims, and they are the whole slice from this side:
#
# 8.  The harness reads a session through ONE narrow shape and reports turns
#     into the same door. `open` is a field there, not a status code.
# 9.  A turn is counted, not accumulated by event: several events carry one
#     turn number, and only the one that read the runner's result carries its
#     usage.
# 10. The room learns what happened. The §H line is a server-side effect of
#     the event, authored by `system`, and a halt writes one whatever stage it
#     re-posted — a turn that dies at `scoped` must not leave the room silent.


async def build_fixture(client, settings_env, seat_toml, *, admin: bool = False):
    """An open session, its owner logged in, and `broker` as the publisher."""
    await make_user("alice", "Alice", admin=admin)
    builder = await make_bot("gable")
    await make_bot("broker", BROKER_KEY)
    settings_env(
        APPS_BUILDERS=[{"bot_id": builder, "model_source": str(seat_toml)}],
        APPS_STAGE_PUBLISHER_BOT_NAMES=["broker"],
    )
    await login(client, "alice")
    return (await start_session(client, builder)).json()


async def post_stage(client, session_id: int, stage: str, detail: dict | None = None):
    body: dict = {"stage": stage}
    if detail is not None:
        body["detail"] = detail
    return await client.post(
        f"/apps/sessions/{session_id}/stage",
        json=body,
        headers=as_bot(client, BROKER_KEY),
    )


async def channel_lines(channel_id: int) -> list[str]:
    rows = await db.fetch_all(
        "SELECT content FROM messages WHERE channel_id = ? ORDER BY seq", (channel_id,)
    )
    return [r["content"] for r in rows]


async def test_harness_view_is_publisher_gated_and_answers_the_four_checks(
    client, app, settings_env, seat_toml
):
    """Claim 8. It is the broker's whole read of a session: is it there, is it
    open, whose builder is it, and what has it spent."""
    session = await build_fixture(client, settings_env, seat_toml)
    path = f"/apps/sessions/{session['id']}/harness-view"

    # The owner is not a publisher, and being the person the stream is FOR is
    # not a way in.
    refused = await client.get(path)
    assert refused.status_code == 403
    assert refused.json()["detail"] == (
        "Only the build harness or an admin can publish a build stage"
    )

    r = await client.get(path, headers=as_bot(client, BROKER_KEY))
    assert r.status_code == 200, r.text
    view = r.json()
    assert view == {
        "session_id": session["id"],
        "app_id": session["app"]["id"],
        "owner_user_id": session["app"]["owner_user_id"],
        "builder_bot_id": session["builder"]["bot_id"],
        "channel_id": session["channel_id"],
        "stage": None,
        "turns": 0,
        "tokens_used": 0,
        "open": True,
        "lock_lapsed": False,
        "ended_at": None,
        "locked_until": session["locked_until"],
        "stop_requested_at": None,
    }

    # A session nobody minted is 404, not an empty view.
    missing = await client.get(
        "/apps/sessions/9999/harness-view", headers=as_bot(client, BROKER_KEY)
    )
    assert missing.status_code == 404

    # A lapsed lock is reported, not enforced (D-A1): the broker is told, and
    # it still spawns. `open` is what closes the door.
    await db.execute(
        "UPDATE app_sessions SET locked_until = '2000-01-01T00:00:00.000Z' "
        "WHERE id = ?",
        (session["id"],),
    )
    lapsed = (await client.get(path, headers=as_bot(client, BROKER_KEY))).json()
    assert (lapsed["lock_lapsed"], lapsed["open"]) == (True, True)

    ended = db.utc_now()
    await db.execute(
        "UPDATE app_sessions SET ended_at = ? WHERE id = ?", (ended, session["id"])
    )
    closed = (await client.get(path, headers=as_bot(client, BROKER_KEY))).json()
    assert (closed["open"], closed["ended_at"]) == (False, ended)



async def test_turns_are_maxed_and_tokens_add_on_files_written_alone(
    client, app, settings_env, seat_toml
):
    """Claim 9. One turn posts four events; the counter must not count them."""
    session = await build_fixture(client, settings_env, seat_toml)
    sid = session["id"]

    async def view() -> dict:
        return (
            await client.get(
                f"/apps/sessions/{sid}/harness-view",
                headers=as_bot(client, BROKER_KEY),
            )
        ).json()

    for stage, detail in (
        ("scoped", {"turn": 1, "model": "claude-opus-5"}),
        ("scaffolded", {"turn": 1}),
        (
            "files_written",
            {
                "turn": 1,
                "files": ["index.html"],
                "tokens": 40407,
                "model": "claude-opus-5",
                "no_changes": False,
            },
        ),
        ("deployed", {"turn": 1}),
    ):
        assert (await post_stage(client, sid, stage, detail)).status_code == 200

    first = await view()
    assert (first["turns"], first["tokens_used"], first["stage"]) == (
        1, 40407, "deployed"
    )

    # Turn 2 adds; the earlier `scoped` re-post carrying no tokens does not.
    await post_stage(client, sid, "scoped", {"turn": 2, "model": "claude-opus-5"})
    await post_stage(
        client, sid, "files_written",
        {"turn": 2, "files": ["app.js"], "tokens": 1000, "model": "claude-opus-5"},
    )
    second = await view()
    assert (second["turns"], second["tokens_used"]) == (2, 41407)

    # Turn 3 burns tokens and then HALTS — its usage re-posts on `scaffolded`,
    # not `files_written`, and it must STILL meter, or a build that fails
    # repeatedly runs free against the ceiling (Claudette #2352 BLOCK 1).
    await post_stage(client, sid, "scoped", {"turn": 3, "model": "claude-opus-5"})
    await post_stage(client, sid, "scaffolded", {"turn": 3})
    await post_stage(
        client, sid, "scaffolded",
        {"turn": 3, "halted": "timeout", "tokens": 500000, "model": "claude-opus-5"},
    )
    third = await view()
    assert (third["turns"], third["tokens_used"]) == (3, 541407)

    # A stage-1 event with no turn at all is still a valid event.
    assert (await post_stage(client, sid, "deployed")).status_code == 200
    assert (await view())["turns"] == 3


async def test_a_ceiling_refusal_does_not_consume_the_turn_number(
    client, app, settings_env, seat_toml
):
    """Gable #2347: a `spawned: false` event (a ceiling refusal) is posted for
    the room but nothing ran, so it must not advance the turn counter — the
    next real handoff reuses the number."""
    session = await build_fixture(client, settings_env, seat_toml)
    sid = session["id"]

    async def turns() -> int:
        return (
            await client.get(
                f"/apps/sessions/{sid}/harness-view",
                headers=as_bot(client, BROKER_KEY),
            )
        ).json()["turns"]

    # turn 1 ran and was counted.
    await post_stage(
        client, sid, "files_written",
        {"turn": 1, "files": ["a.js"], "tokens": 10, "model": "m"},
    )
    assert await turns() == 1

    # a ceiling refusal, labelled turn 2, spawned false: the room hears it but
    # the counter stays at 1, so the next real handoff is still turn 2.
    resp = await post_stage(
        client, sid, "scoped",
        {"turn": 2, "halted": "ceiling", "spawned": False, "reason": "over"},
    )
    assert resp.status_code == 200
    assert await turns() == 1


async def test_a_ceiling_refusal_does_not_rewind_the_session_stage(
    client, app, settings_env, seat_toml
):
    """Claudette #2352 BLOCK 2: a session sitting at `deployed` gets a ceiling
    refusal posted as `scoped`, and its stage pointer must not go backwards —
    nothing ran, nothing about its progress changed."""
    session = await build_fixture(client, settings_env, seat_toml)
    sid = session["id"]

    async def stage() -> str:
        return (
            await client.get(
                f"/apps/sessions/{sid}/harness-view",
                headers=as_bot(client, BROKER_KEY),
            )
        ).json()["stage"]

    await post_stage(
        client, sid, "files_written",
        {"turn": 1, "files": ["a.js"], "tokens": 10, "model": "m"},
    )
    await post_stage(client, sid, "deployed", {"turn": 1})
    assert await stage() == "deployed"

    await post_stage(
        client, sid, "scoped",
        {"turn": 2, "halted": "ceiling", "spawned": False, "reason": "over"},
    )
    assert await stage() == "deployed"     # not rewound to scoped


async def test_a_halt_names_what_it_wrote(client, app, settings_env, seat_toml):
    """Claudette #2352: a halt that committed a tree still lists its files, so
    the user's next prompt is not written against a repo they weren't told
    changed."""
    session = await build_fixture(client, settings_env, seat_toml)
    sid, channel = session["id"], session["channel_id"]
    await post_stage(
        client, sid, "scaffolded",
        {"turn": 1, "halted": "timeout", "files": ["index.html", "app.js"],
         "tokens": 5, "model": "m"},
    )
    assert (await channel_lines(channel))[-1] == (
        "Turn 1 halted — the build timed out. Wrote index.html, app.js."
    )


async def test_a_halt_that_wrote_a_report_keeps_it(
    client, app, settings_env, seat_toml
):
    """Claudette #2354: the turn where the runner wrote something and then
    failed is the turn its report is most worth reading."""
    session = await build_fixture(client, settings_env, seat_toml)
    sid, channel = session["id"], session["channel_id"]
    await post_stage(
        client, sid, "scaffolded",
        {"turn": 1, "halted": "error", "files": ["a.js"], "tokens": 5,
         "model": "m", "reason": "publish failed",
         "summary": "Wired the form but the deploy step errored."},
    )
    assert (await channel_lines(channel))[-1] == (
        'Turn 1 halted — the build failed. publish failed Wrote a.js. '
        '"Wired the form but the deploy step errored."'
    )


async def test_a_turns_tokens_are_counted_once_even_if_the_event_repeats(
    client, app, settings_env, seat_toml
):
    """Claudette #2354: the meter is a running +=, so 'one event per turn
    carries tokens' is enforced, not trusted — a retried post after a timeout
    the reaper thought failed must not double-charge the ceiling."""
    session = await build_fixture(client, settings_env, seat_toml)
    sid = session["id"]

    async def used() -> int:
        return (
            await client.get(
                f"/apps/sessions/{sid}/harness-view",
                headers=as_bot(client, BROKER_KEY),
            )
        ).json()["tokens_used"]

    detail = {"turn": 1, "files": ["a.js"], "tokens": 40407, "model": "m"}
    assert (await post_stage(client, sid, "files_written", detail)).status_code == 200
    assert await used() == 40407
    # the identical event again (a retry): counted once.
    assert (await post_stage(client, sid, "files_written", detail)).status_code == 200
    assert await used() == 40407
    # turn 2's own tokens still add.
    await post_stage(
        client, sid, "files_written",
        {"turn": 2, "files": ["b.js"], "tokens": 1000, "model": "m"},
    )
    assert await used() == 41407


async def test_the_turn_line_says_exactly_what_the_turn_did(
    client, app, settings_env, seat_toml
):
    """Claim 10. §H, sentence by sentence. These strings ARE the interface —
    they land in the user's room and in both residents' transcripts, and a
    resident that later says "the app I built you" is quoting them."""
    session = await build_fixture(client, settings_env, seat_toml)
    sid, channel = session["id"], session["channel_id"]

    # Nothing before the terminal event: `scoped` and `scaffolded` move the
    # bar, and a room narrating its own scaffolding is noise.
    await post_stage(client, sid, "scoped", {"turn": 1, "model": "claude-opus-5"})
    await post_stage(client, sid, "scaffolded", {"turn": 1})
    assert len(await channel_lines(channel)) == 1  # the opener, and only it

    await post_stage(
        client, sid, "files_written",
        {
            "turn": 1,
            "files": ["index.html", "app.js", "style.css", "README.md"],
            "tokens": 212_400,
            "model": "claude-opus-5",
            "no_changes": False,
            "summary": "A four-file to-do list that stores in localStorage.",
        },
    )
    assert (await channel_lines(channel))[-1] == (
        "Turn 1 done — 4 files written (index.html, app.js, style.css, "
        "README.md), 212k tokens, model claude-opus-5. "
        '"A four-file to-do list that stores in localStorage."'
    )

    # `deployed` is the publish, not a second outcome: one line per turn.
    await post_stage(client, sid, "deployed", {"turn": 1})
    assert len(await channel_lines(channel)) == 2

    await post_stage(
        client, sid, "files_written",
        {"turn": 2, "files": [], "tokens": 800, "model": "claude-opus-5",
         "no_changes": True, "summary": "Already does that."},
    )
    assert (await channel_lines(channel))[-1] == (
        'Turn 2: no changes. "Already does that."'
    )

    # A halt at `scoped` — the stage it re-posts is the last one it reached,
    # and the room hears about it anyway.
    await post_stage(
        client, sid, "scoped",
        {"turn": 3, "halted": "ceiling",
         "reason": "9,900,000 of 10,000,000 tokens spent."},
    )
    assert (await channel_lines(channel))[-1] == (
        "Turn 3 halted — build hit its ceiling. "
        "9,900,000 of 10,000,000 tokens spent."
    )

    await post_stage(client, sid, "scaffolded", {"turn": 4, "halted": "timeout"})
    assert (await channel_lines(channel))[-1] == "Turn 4 halted — the build timed out."

    await post_stage(
        client, sid, "scaffolded",
        {"turn": 5, "halted": "error", "reason": "the turn ended\nwithout a result"},
    )
    # The reason arrived with a newline in it and lands as one line: a build
    # seat's free text is plain text in the transcript, never a second line.
    assert (await channel_lines(channel))[-1] == (
        "Turn 5 halted — the build failed. the turn ended without a result"
    )

    # Sub-thousand tokens print as themselves; an absent model is said so.
    await post_stage(
        client, sid, "files_written", {"turn": 6, "files": ["a.js"], "tokens": 999},
    )
    assert (await channel_lines(channel))[-1] == (
        "Turn 6 done — 1 file written (a.js), 999 tokens, model not reported."
    )

    # Nine files: eight named, the rest counted.
    await post_stage(
        client, sid, "files_written",
        {"turn": 7, "files": [f"f{i}.js" for i in range(9)], "tokens": 1000,
         "model": "m"},
    )
    assert (await channel_lines(channel))[-1] == (
        "Turn 7 done — 9 files written (f0.js, f1.js, f2.js, f3.js, f4.js, "
        "f5.js, f6.js, f7.js, +1 more), 1k tokens, model m."
    )

    # Every line is the seeded `system` bot's, and none of them is the
    # builder's — the broker has no post right in this room (stage 1 #2266).
    system = await db.fetch_one("SELECT id FROM bots WHERE name = 'system'")
    rows = await db.fetch_all(
        "SELECT author_type, author_id FROM messages WHERE channel_id = ?", (channel,)
    )
    assert {(r["author_type"], r["author_id"]) for r in rows} == {
        ("bot", system["id"])
    }


async def test_a_credential_halt_closes_the_session(
    client, app, settings_env, seat_toml
):
    """§E: a turn that tried to publish a credential has earned a human before
    the next one. The line says so and the door shuts in the same breath."""
    session = await build_fixture(client, settings_env, seat_toml)
    sid = session["id"]

    r = await post_stage(
        client, sid, "scaffolded",
        {"turn": 1, "halted": "secret", "quarantine": "/srv/apps-quarantine/x/1"},
    )
    assert r.status_code == 200, r.text
    assert (await channel_lines(session["channel_id"]))[-1] == (
        "Turn 1 halted — the build tried to write a credential; this session "
        "is closed and an admin has been notified."
    )

    view = (
        await client.get(
            f"/apps/sessions/{sid}/harness-view", headers=as_bot(client, BROKER_KEY)
        )
    ).json()
    assert view["open"] is False
    assert view["ended_at"] is not None
    assert view["locked_until"] == view["ended_at"]   # the lock came back too

    # The user's modal finds out the next time it beats, and the harness is
    # refused its next post on the same fact.
    await login(client, "alice")
    beat = await client.post(f"/apps/sessions/{sid}/heartbeat")
    assert beat.status_code == 410
    assert (await post_stage(client, sid, "scoped", {"turn": 2})).status_code == 410

    # The quarantine path rode along in `detail` untouched — an unknown key is
    # still an accepted key.
    reloaded = await db.fetch_one(
        "SELECT detail FROM app_stage_events WHERE session_id = ? ORDER BY id DESC",
        (sid,),
    )
    assert json.loads(reloaded["detail"])["quarantine"] == "/srv/apps-quarantine/x/1"


async def test_detail_is_typed_where_the_server_acts_on_it(
    client, app, settings_env, seat_toml
):
    """The keys that choose a sentence or move a counter are validated at the
    door. Everything else stays free-form, because a publisher that learns a
    new fact should not need a server release to report it."""
    session = await build_fixture(client, settings_env, seat_toml)
    sid = session["id"]

    for bad in (
        {"halted": "wandered off"},          # not one of the four sentences
        {"turn": 0},                         # turns start at 1
        {"tokens": -1},
        {"summary": "s" * 301},
        {"reason": "r" * 301},
        {"files": "index.html"},             # a list, not a name
    ):
        r = await post_stage(client, sid, "scaffolded", bad)
        assert r.status_code == 422, (bad, r.text)

    # And the 2000-char bound stays the outer wall, whatever the keys are.
    over = await post_stage(
        client, sid, "files_written", {"turn": 1, "files": ["x" * 60] * 40},
    )
    assert over.status_code == 422

    # A detail at the brief's worst case — 40 names plus the "+N more" marker,
    # a 300-char summary — fits under the bound. If this ever fails, the cap
    # in the broker's detail builder is the thing that is wrong.
    packed = await post_stage(
        client, sid, "files_written",
        {
            "turn": 3,
            "files": [f"src/component-{i:02d}.js" for i in range(40)] + ["+160 more"],
            "tokens": 431_200,
            "model": "claude-opus-5",
            "no_changes": False,
            "summary": "s" * 300,
        },
    )
    assert packed.status_code == 200, packed.text


async def test_an_admin_publishing_by_hand_keeps_the_lock(
    client, app, settings_env, seat_toml
):
    """D-B3 is scoped to the harness. An admin poking a session nobody is
    watching is exactly the case the lock is there to catch."""
    session = await build_fixture(client, settings_env, seat_toml, admin=True)
    sid = session["id"]
    await db.execute(
        "UPDATE app_sessions SET locked_until = '2000-01-01T00:00:00.000Z' "
        "WHERE id = ?",
        (sid,),
    )

    await login(client, "alice")
    by_hand = await client.post(
        f"/apps/sessions/{sid}/stage", json={"stage": "scaffolded"}
    )
    assert by_hand.status_code == 410

    assert (await post_stage(client, sid, "scaffolded", {"turn": 1})).status_code == 200


async def test_the_migration_adds_the_two_turn_columns(app):
    """012. Both default to 0, which is the truth for a session that never ran
    a turn — including every session created before the migration."""
    rows = await db.fetch_all("PRAGMA table_info(app_sessions)")
    columns = {r["name"]: r for r in rows}
    for name in ("turns", "tokens_used"):
        assert columns[name]["notnull"] == 1
        assert columns[name]["dflt_value"] == "0"


def test_the_turn_line_renders_without_a_database():
    """The §H sentences, straight from the renderer — the one place they are
    written, asserted without a session in the way."""
    from app.routers.apps import _turn_line

    assert _turn_line({}) == "Turn 0 done — 0 files written, 0 tokens, model not reported."
    assert _turn_line({"turn": 2, "no_changes": True}) == "Turn 2: no changes."
    # A summary that is only control characters is no summary at all.
    assert _turn_line({"turn": 2, "no_changes": True, "summary": "\n\t "}) == (
        "Turn 2: no changes."
    )


# ---------------------------------------------------------------------------
# Slice (iv): stop a running turn (SPECS/2026-09-08-apps-stop-turn.md, 2444)
# ---------------------------------------------------------------------------


async def _stage(client, sid: int, stage: str, detail: dict):
    r = await client.post(
        f"/apps/sessions/{sid}/stage", json={"stage": stage, "detail": detail},
        headers=as_bot(client, BROKER_KEY),
    )
    return r


async def _owner_post(client, path: str):
    """The owner's click. as_bot cleared the jar on the last harness call, so
    every owner call re-logs in first — the same user build_fixture used."""
    await login(client, "alice")
    return await client.post(path)


async def _view(client, sid: int) -> dict:
    return (
        await client.get(f"/apps/sessions/{sid}/harness-view",
                         headers=as_bot(client, BROKER_KEY))
    ).json()


async def test_stop_needs_a_running_turn_and_is_idempotent(
    client, app, settings_env, seat_toml
):
    """409 with nothing running: a request that would otherwise sit on the row
    until some later turn inherited it. Once a turn is scoped the stop lands,
    a second click keeps the first timestamp, and harness-view carries it."""
    session = await build_fixture(client, settings_env, seat_toml)
    sid = session["id"]
    nothing = await _owner_post(client, f"/apps/sessions/{sid}/stop")
    assert nothing.status_code == 409
    assert (await _view(client, sid))["stop_requested_at"] is None

    assert (await _stage(client, sid, "scoped", {"turn": 1})).status_code == 200
    first = await _owner_post(client, f"/apps/sessions/{sid}/stop")
    assert first.status_code == 200
    stamp = first.json()["stop_requested_at"]
    assert stamp and first.json()["id"] == sid
    again = await _owner_post(client, f"/apps/sessions/{sid}/stop")
    assert again.status_code == 200 and again.json()["stop_requested_at"] == stamp
    assert (await _view(client, sid))["stop_requested_at"] == stamp


async def test_a_terminal_event_consumes_the_stop_and_the_room_says_stopped(
    client, app, settings_env, seat_toml
):
    """The harvest's record is the terminal event; `stopped` is a closed-set
    reason with its own sentence, and it clears the request so the next turn
    starts unasked."""
    session = await build_fixture(client, settings_env, seat_toml)
    sid = session["id"]
    await _stage(client, sid, "scoped", {"turn": 1})
    await _owner_post(client, f"/apps/sessions/{sid}/stop")
    r = await _stage(client, sid, "scaffolded", {
        "turn": 1, "halted": "stopped", "files": ["index.html"], "tokens": 12,
    })
    assert r.status_code == 200, r.text
    assert (await _view(client, sid))["stop_requested_at"] is None
    lines = await channel_lines(session["channel_id"])
    assert lines[-1].startswith("Turn 1 halted — stopped by the user. Wrote index.html."), lines
    # a second turn is a fresh start: stop is 409 until it is scoped again
    assert (await _owner_post(client, f"/apps/sessions/{sid}/stop")).status_code == 409


async def test_end_while_running_stops_first_and_that_turns_record_still_lands(
    client, app, settings_env, seat_toml
):
    """`end` used to set only `ended_at`; the unit ran to turn_max and its
    record 410'd. Now the stop is recorded first, and the ONE turn that was
    running may still post its terminal event through the closed door — no
    other turn can."""
    session = await build_fixture(client, settings_env, seat_toml)
    sid = session["id"]
    await _stage(client, sid, "scoped", {"turn": 2})
    ended = await _owner_post(client, f"/apps/sessions/{sid}/end")
    assert ended.status_code == 200
    view = await _view(client, sid)
    assert view["open"] is False
    assert view["stop_requested_at"] == view["ended_at"]

    # a different turn's event: the door is shut
    other = await _stage(client, sid, "scoped", {"turn": 3})
    assert other.status_code == 410
    # the running turn's terminal event: accepted, and it consumes the stop
    landed = await _stage(client, sid, "scaffolded", {"turn": 2, "halted": "stopped"})
    assert landed.status_code == 200, landed.text
    assert (await _view(client, sid))["stop_requested_at"] is None
    # and now even that turn is shut out: the stop was the key, and it is spent
    assert (await _stage(client, sid, "scaffolded",
                         {"turn": 2, "halted": "stopped"})).status_code == 410
    # stop on an ended session is 410, like everything else on one
    assert (await _owner_post(client, f"/apps/sessions/{sid}/stop")).status_code == 410


async def test_a_lapsed_lock_does_not_bar_the_owner_from_stopping(
    client, app, settings_env, seat_toml
):
    """The lock is chat exclusivity, not a door (Claudette #2447). An owner
    who walked away and came back to a runaway turn can still stop it."""
    session = await build_fixture(client, settings_env, seat_toml)
    sid = session["id"]
    await _stage(client, sid, "scoped", {"turn": 1})
    await db.execute(
        "UPDATE app_sessions SET locked_until = '2000-01-01T00:00:00.000Z' WHERE id = ?",
        (sid,),
    )
    r = await _owner_post(client, f"/apps/sessions/{sid}/stop")
    assert r.status_code == 200, r.text
    assert (await _view(client, sid))["stop_requested_at"] == r.json()["stop_requested_at"]


async def test_end_with_nothing_running_requests_no_stop(
    client, app, settings_env, seat_toml
):
    session = await build_fixture(client, settings_env, seat_toml)
    sid = session["id"]
    await _stage(client, sid, "scoped", {"turn": 1})
    await _stage(client, sid, "files_written", {"turn": 1, "files": [], "tokens": 1})
    await _owner_post(client, f"/apps/sessions/{sid}/end")
    assert (await _view(client, sid))["stop_requested_at"] is None


async def test_a_ceiling_refusal_is_not_a_running_turn(
    client, app, settings_env, seat_toml
):
    """`spawned: false` never started anything, so there is nothing to stop."""
    session = await build_fixture(client, settings_env, seat_toml)
    sid = session["id"]
    await _stage(client, sid, "scoped", {"turn": 1, "spawned": False, "halted": "ceiling"})
    assert (await _owner_post(client, f"/apps/sessions/{sid}/stop")).status_code == 409


def test_the_stopped_turn_line_reads_like_the_other_halts():
    from app.routers.apps import _turn_line
    line = _turn_line({"turn": 4, "halted": "stopped", "files": ["a.js", "b.js"],
                       "summary": "was halfway through the header"})
    assert line == ('Turn 4 halted — stopped by the user. Wrote a.js, b.js. '
                    '"was halfway through the header"')
