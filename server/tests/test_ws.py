"""WP5 tests: privacy module (unit) + WebSocket hub (integration).

The hub tests use starlette's sync TestClient as a context manager: its
lifespan runs on a single portal event loop shared by REST calls, WS sessions,
and our DB seeding (via `client.portal.call`), so ordering is deterministic:
after a REST post returns, all fan-out frames are already buffered.

Handshake protocol used throughout: every fresh user connection first receives
{"type": "ready"} then its own presence broadcast; draining those (and the
presence frame on already-open sockets) before proceeding keeps frame order
exact. "X receives nothing" is asserted with a sentinel: trigger a visible
event afterwards and assert it is the connection's NEXT frame.
"""

import asyncio
from contextlib import ExitStack

import pytest
from starlette.testclient import TestClient, WebSocketDisconnect

from app import db, events, privacy
from app import ws as ws_module
from app.routers import auth

PASSWORD = "correct horse battery staple"
PASSWORD_HASH = auth.hash_password(PASSWORD)  # hashed once — argon2 is slow
BOT_KEY = "ws-bot-key-1"
BOT2_KEY = "ws-bot-key-2"


# ---------------------------------------------------------------------------
# privacy.py unit tests (pure functions — no app needed)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "content",
    [
        "Don't tell anyone, but I got the job",
        "dont tell anyone ok?",
        "don’t tell anyone",                      # curly apostrophe
        "DON'T TELL ANYONE",
        "this stays just between us",
        "Between you and me, it's a mess",
        "keep this secret please",
        "keep this between us, alright",
        "keep this   between\nus",                # flexible whitespace
    ],
)
def test_detect_flags_secret_phrases(content):
    assert privacy.detect_flags(content) == {"secret": True}


def test_detect_flags_off_the_record_and_both():
    assert privacy.detect_flags("off the record: I hate mondays") == {
        "off_the_record": True
    }
    assert privacy.detect_flags("Off The Record and just between us") == {
        "secret": True,
        "off_the_record": True,
    }


@pytest.mark.parametrize(
    "content",
    [
        "",
        "a perfectly public message",
        "scoff the record player",        # leading word boundary
        "off the recorder is broken",     # trailing word boundary
        "betweenyouandme",                # no internal boundaries
        "the record was off",             # wrong order
        "keep this secretive habit",      # trailing boundary
    ],
)
def test_detect_flags_negative(content):
    assert privacy.detect_flags(content) == {}


def test_hidden_from_bots():
    assert privacy.hidden_from_bots({}) is False
    assert privacy.hidden_from_bots(None) is False
    assert privacy.hidden_from_bots({"secret": True}) is True
    assert privacy.hidden_from_bots({"off_the_record": True}) is True
    assert privacy.hidden_from_bots({"secret": False}) is False  # only truthy hides


def test_visible_to_bot():
    msg = {"privacy_flags": {}}
    secret = {"privacy_flags": {"secret": True}}
    assert privacy.visible_to_bot(msg, 1, channel_is_bot_member=True) is True
    assert privacy.visible_to_bot(msg, 1, channel_is_bot_member=False) is False
    assert privacy.visible_to_bot(secret, 1, channel_is_bot_member=True) is False


def test_filter_event_for_bot():
    ok = {"type": "message_create", "channel_id": 1, "message": {"privacy_flags": {}}}
    hidden = {
        "type": "message_edit",
        "channel_id": 1,
        "message": {"privacy_flags": {"off_the_record": True}},
    }
    assert privacy.filter_event_for_bot(ok) is ok
    assert privacy.filter_event_for_bot(hidden) is None
    # message_delete: caller-enriched flags decide; bare delete passes.
    delete = {"type": "message_delete", "channel_id": 1, "id": 9, "seq": 3}
    assert privacy.filter_event_for_bot(delete) is delete
    assert (
        privacy.filter_event_for_bot({**delete, "privacy_flags": {"secret": True}})
        is None
    )
    # Non-message events pass through untouched.
    presence = {"type": "presence", "user_id": 1, "status": "online"}
    assert privacy.filter_event_for_bot(presence) is presence


# ---------------------------------------------------------------------------
# WS fixtures + helpers
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
    """Run an async db helper on the app's own event loop."""
    return wsc.portal.call(coro_fn, *args)


def make_user(wsc, username, status=None):
    cur = call(
        wsc,
        db.execute,
        "INSERT INTO users (username, password_hash, display_name, status) "
        "VALUES (?, ?, ?, ?)",
        (username, PASSWORD_HASH, username.capitalize(), status or "offline"),
    )
    return cur.lastrowid


def make_bot(wsc, name, api_key=BOT_KEY):
    cur = call(
        wsc,
        db.execute,
        "INSERT INTO bots (name, api_key_hash) VALUES (?, ?)",
        (name, auth.hash_api_key(api_key)),
    )
    return cur.lastrowid


def add_member(wsc, channel_id, member_type, member_id):
    call(
        wsc,
        db.execute,
        "INSERT OR IGNORE INTO channel_members (channel_id, member_type, member_id) "
        "VALUES (?, ?, ?)",
        (channel_id, member_type, member_id),
    )


def main_feed_id(wsc):
    row = call(wsc, db.fetch_one, "SELECT id FROM channels WHERE type = 'main_feed'")
    return row["id"]


def login(wsc, username):
    """Login and return the raw session token; the shared jar stays empty so
    every REST/WS call picks its identity via an explicit Cookie header."""
    r = wsc.post("/auth/login", json={"username": username, "password": PASSWORD})
    assert r.status_code == 200, r.text
    token = r.cookies.get(auth.COOKIE_NAME)
    wsc.cookies.clear()
    assert token
    return token


def cookie(token):
    return {"cookie": f"{auth.COOKIE_NAME}={token}"}


def post_msg(wsc, token, channel_id, content):
    r = wsc.post(
        f"/channels/{channel_id}/messages",
        json={"content": content},
        headers=cookie(token),
    )
    assert r.status_code == 200, r.text
    return r.json()


def make_dm(wsc, token, other_user_id):
    r = wsc.post("/dms", json={"user_id": other_user_id}, headers=cookie(token))
    assert r.status_code == 200, r.text
    return r.json()["id"]


def open_user(stack, wsc, token, user_id, *, status="online", peers=()):
    """Open a user WS; drain its ready + own presence, and the presence frame
    on every already-open socket in `peers`."""
    ws = stack.enter_context(wsc.websocket_connect("/ws", headers=cookie(token)))
    assert ws.receive_json() == {"type": "ready", "user_id": user_id}
    expected = {"type": "presence", "user_id": user_id, "status": status}
    assert ws.receive_json() == expected
    for peer in peers:
        assert peer.receive_json() == expected
    return ws


def open_bot(stack, wsc, bot_id, api_key=BOT_KEY):
    ws = stack.enter_context(wsc.websocket_connect("/ws"))
    ws.send_json({"op": "auth", "api_key": api_key})
    assert ws.receive_json() == {"type": "ready", "bot_id": bot_id}
    return ws


def assert_msg_frame(frame, *, type="message_create", channel_id, content=None):
    assert frame["type"] == type
    assert frame["channel_id"] == channel_id
    assert frame["seq"] == frame["message"]["seq"]
    if content is not None:
        assert frame["message"]["content"] == content
    return frame["message"]


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

def test_ws_user_cookie_auth(wsc):
    uid = make_user(wsc, "alice")
    token = login(wsc, "alice")
    with ExitStack() as stack:
        open_user(stack, wsc, token, uid)  # ready + presence asserted inside


def test_ws_bot_key_auth(wsc):
    bot_id = make_bot(wsc, "claudette")
    with ExitStack() as stack:
        open_bot(stack, wsc, bot_id)


def test_ws_rejects_bad_credentials(wsc):
    make_user(wsc, "alice")
    # Bad API key -> 4401.
    with wsc.websocket_connect("/ws") as ws:
        ws.send_json({"op": "auth", "api_key": "wrong-key"})
        with pytest.raises(WebSocketDisconnect) as exc:
            ws.receive_json()
    assert exc.value.code == 4401
    # Invalid first frame (not an auth op) -> 4401, no 5s wait.
    with wsc.websocket_connect("/ws") as ws:
        ws.send_text("this is not json")
        with pytest.raises(WebSocketDisconnect) as exc:
            ws.receive_json()
    assert exc.value.code == 4401
    # Bogus session cookie + non-auth frame -> 4401.
    with wsc.websocket_connect(
        "/ws", headers=cookie("bogus-token")
    ) as ws:
        ws.send_json({"op": "typing", "channel_id": 1})
        with pytest.raises(WebSocketDisconnect) as exc:
            ws.receive_json()
    assert exc.value.code == 4401


# ---------------------------------------------------------------------------
# Message fan-out scoping
# ---------------------------------------------------------------------------

def test_message_fanout_channel_members_only(wsc):
    a, b, c = make_user(wsc, "alice"), make_user(wsc, "bob"), make_user(wsc, "carol")
    ta, tb, tc = login(wsc, "alice"), login(wsc, "bob"), login(wsc, "carol")
    main = main_feed_id(wsc)
    dm = make_dm(wsc, ta, b)

    with ExitStack() as stack:
        wa = open_user(stack, wsc, ta, a)
        wb = open_user(stack, wsc, tb, b, peers=[wa])
        wc = open_user(stack, wsc, tc, c, peers=[wa, wb])

        # DM message: only the two participants receive it.
        post_msg(wsc, ta, dm, "psst bob")
        assert_msg_frame(wa.receive_json(), channel_id=dm, content="psst bob")
        assert_msg_frame(wb.receive_json(), channel_id=dm, content="psst bob")

        # main_feed message: everyone. Carol's FIRST frame is this one —
        # she never saw the DM traffic.
        post_msg(wsc, ta, main, "hello everyone")
        for ws in (wa, wb, wc):
            assert_msg_frame(ws.receive_json(), channel_id=main, content="hello everyone")

        # Edit fans out with the updated payload.
        msg = post_msg(wsc, ta, main, "tpyo")
        for ws in (wa, wb, wc):
            assert_msg_frame(ws.receive_json(), channel_id=main, content="tpyo")
        r = wsc.patch(
            f"/messages/{msg['id']}", json={"content": "typo"}, headers=cookie(ta)
        )
        assert r.status_code == 200
        for ws in (wa, wb, wc):
            frame = assert_msg_frame(
                ws.receive_json(), type="message_edit", channel_id=main, content="typo"
            )
            assert frame["edited_at"] is not None


def test_bot_gets_main_feed_but_not_dm(wsc):
    a, b = make_user(wsc, "alice"), make_user(wsc, "bob")
    ta = login(wsc, "alice")
    bot_id = make_bot(wsc, "claudette")
    main = main_feed_id(wsc)
    add_member(wsc, main, "bot", bot_id)
    dm = make_dm(wsc, ta, b)

    with ExitStack() as stack:
        wbot = open_bot(stack, wsc, bot_id)
        post_msg(wsc, ta, dm, "private dm chatter")       # bot: not a member
        post_msg(wsc, ta, main, "public hello")           # bot: member
        frame = wbot.receive_json()                       # first frame = main msg
        assert_msg_frame(frame, channel_id=main, content="public hello")
        assert "context" not in frame                     # no mention -> no context


# ---------------------------------------------------------------------------
# Privacy filtering on the stream
# ---------------------------------------------------------------------------

def test_secret_message_hidden_from_bot_including_delete(wsc):
    a, b = make_user(wsc, "alice"), make_user(wsc, "bob")
    ta, tb = login(wsc, "alice"), login(wsc, "bob")
    bot_id = make_bot(wsc, "claudette")
    main = main_feed_id(wsc)
    add_member(wsc, main, "bot", bot_id)

    with ExitStack() as stack:
        wb = open_user(stack, wsc, tb, b)
        wbot = open_bot(stack, wsc, bot_id)

        # NL trigger -> server-side secret flag; humans see it, the bot never does.
        secret = post_msg(wsc, ta, main, "don't tell anyone, but the cake is a lie")
        assert secret["privacy_flags"] == {"secret": True}
        frame = assert_msg_frame(wb.receive_json(), channel_id=main)
        assert frame["privacy_flags"] == {"secret": True}

        # Deleting the hidden message: humans get the tombstone, the bot gets
        # nothing — not even the delete of a message it never saw.
        r = wsc.delete(f"/messages/{secret['id']}", headers=cookie(ta))
        assert r.status_code == 200
        assert wb.receive_json() == {
            "type": "message_delete",
            "channel_id": main,
            "id": secret["id"],
            "seq": secret["seq"],
        }

        # Edit that ADDS a hidden flag: bot saw the original but not the edit.
        plain = post_msg(wsc, ta, main, "totally innocent")
        assert_msg_frame(wb.receive_json(), channel_id=main, content="totally innocent")
        assert_msg_frame(wbot.receive_json(), channel_id=main, content="totally innocent")
        r = wsc.patch(
            f"/messages/{plain['id']}",
            json={"content": "keep this secret please"},
            headers=cookie(ta),
        )
        assert r.status_code == 200
        assert_msg_frame(wb.receive_json(), type="message_edit", channel_id=main)

        # Sentinel: the bot's next frame skips everything hidden above.
        post_msg(wsc, ta, main, "all clear")
        assert_msg_frame(wbot.receive_json(), channel_id=main, content="all clear")
        assert_msg_frame(wb.receive_json(), channel_id=main, content="all clear")


# ---------------------------------------------------------------------------
# Typing + rate limit
# ---------------------------------------------------------------------------

def test_typing_broadcast_and_rate_limit(wsc):
    a, b = make_user(wsc, "alice"), make_user(wsc, "bob")
    ta, tb = login(wsc, "alice"), login(wsc, "bob")
    bot_id = make_bot(wsc, "claudette")
    main = main_feed_id(wsc)
    add_member(wsc, main, "bot", bot_id)

    with ExitStack() as stack:
        wa = open_user(stack, wsc, ta, a)
        wb = open_user(stack, wsc, tb, b, peers=[wa])
        wbot = open_bot(stack, wsc, bot_id)

        typing = {
            "type": "typing_start",
            "channel_id": main,
            "author_type": "user",
            "author_id": a,
        }
        wa.send_json({"op": "typing", "channel_id": main})
        assert wb.receive_json() == typing        # member users receive
        assert wbot.receive_json() == typing      # member bots receive

        # Second typing within 3s is suppressed; sender never gets its own.
        wa.send_json({"op": "typing", "channel_id": main})
        post_msg(wsc, tb, main, "sentinel")
        assert_msg_frame(wa.receive_json(), channel_id=main, content="sentinel")
        assert_msg_frame(wb.receive_json(), channel_id=main, content="sentinel")
        assert_msg_frame(wbot.receive_json(), channel_id=main, content="sentinel")


# ---------------------------------------------------------------------------
# Presence
# ---------------------------------------------------------------------------

def test_presence_connect_status_disconnect(wsc):
    a, b = make_user(wsc, "alice"), make_user(wsc, "bob")
    make_user(wsc, "dawn", status="dnd")
    d = call(wsc, db.fetch_one, "SELECT id FROM users WHERE username = 'dawn'")["id"]
    ta, tb, td = login(wsc, "alice"), login(wsc, "bob"), login(wsc, "dawn")
    bot_id = make_bot(wsc, "claudette")

    with ExitStack() as stack:
        wa = open_user(stack, wsc, ta, a)
        wbot = open_bot(stack, wsc, bot_id)

        # Connect -> online, broadcast to all users AND all bots.
        with wsc.websocket_connect("/ws", headers=cookie(tb)) as wb:
            assert wb.receive_json() == {"type": "ready", "user_id": b}
            online = {"type": "presence", "user_id": b, "status": "online"}
            assert wb.receive_json() == online
            assert wa.receive_json() == online
            assert wbot.receive_json() == online

            # Explicit status op: persists users.status + broadcasts.
            wb.send_json({"op": "status", "status": "idle"})
            idle = {"type": "presence", "user_id": b, "status": "idle"}
            assert wa.receive_json() == idle
            assert wb.receive_json() == idle
            assert wbot.receive_json() == idle
            row = call(wsc, db.fetch_one, "SELECT status FROM users WHERE id = ?", (b,))
            assert row["status"] == "idle"

        # Last disconnect -> offline.
        offline = {"type": "presence", "user_id": b, "status": "offline"}
        assert wa.receive_json() == offline
        assert wbot.receive_json() == offline

        # Stored idle/dnd is respected on connect (no forced "online").
        wd = open_user(stack, wsc, td, d, status="dnd", peers=[wa, wbot])
        assert wd is not None


# ---------------------------------------------------------------------------
# Context injection
# ---------------------------------------------------------------------------

def test_context_injection_on_mention_only_for_that_bot(wsc):
    a, b = make_user(wsc, "alice"), make_user(wsc, "bob")
    ta, tb = login(wsc, "alice"), login(wsc, "bob")
    claudette = make_bot(wsc, "claudette", BOT_KEY)
    otto = make_bot(wsc, "otto", BOT2_KEY)
    main = main_feed_id(wsc)
    add_member(wsc, main, "bot", claudette)
    add_member(wsc, main, "bot", otto)

    with ExitStack() as stack:
        wa = open_user(stack, wsc, ta, a)
        wb = open_user(stack, wsc, tb, b, peers=[wa])
        wcl = open_bot(stack, wsc, claudette, BOT_KEY)
        wot = open_bot(stack, wsc, otto, BOT2_KEY)

        # @mention (case-insensitive): only Claudette's copy carries context.
        post_msg(wsc, ta, main, "hey @Claudette what do you think?")
        human_frame = wa.receive_json()
        assert "context" not in human_frame
        wb.receive_json()

        cl_frame = wcl.receive_json()
        assert_msg_frame(cl_frame, channel_id=main)
        ctx = cl_frame["context"]
        assert ctx["awake_users"] == [
            {"id": a, "name": "Alice", "status": "online"},
            {"id": b, "name": "Bob", "status": "online"},
        ]
        assert ctx["channel_state"] == {"name": "main"}
        assert ctx["privacy_flags_on_current_message"] == {}

        ot_frame = wot.receive_json()
        assert ot_frame["message"]["content"].startswith("hey @Claudette")
        assert "context" not in ot_frame

        # Bare name as a word also triggers; substring does not.
        post_msg(wsc, ta, main, "maybe claudette knows")
        wa.receive_json(), wb.receive_json(), wot.receive_json()
        assert "context" in wcl.receive_json()

        post_msg(wsc, ta, main, "the claudettes are a band")
        wa.receive_json(), wb.receive_json(), wot.receive_json()
        assert "context" not in wcl.receive_json()


# ---------------------------------------------------------------------------
# Focus tracking + manager exports (WP7 interface)
# ---------------------------------------------------------------------------

def test_focus_tracking_and_manager_exports(wsc):
    a, b = make_user(wsc, "alice"), make_user(wsc, "bob")
    ta, tb = login(wsc, "alice"), login(wsc, "bob")
    main = main_feed_id(wsc)

    assert ws_module.manager.is_user_connected(a) is False
    with ExitStack() as stack:
        wa = open_user(stack, wsc, ta, a)
        wb = open_user(stack, wsc, tb, b, peers=[wa])
        assert ws_module.manager.is_user_connected(a) is True
        assert ws_module.manager.user_focused_channel_ids(a) == set()

        wa.send_json({"op": "focus", "channel_id": main})
        # Ops are processed in order per connection: once the typing broadcast
        # lands on bob, the focus op before it has been applied.
        wa.send_json({"op": "typing", "channel_id": main})
        assert wb.receive_json()["type"] == "typing_start"
        assert ws_module.manager.user_focused_channel_ids(a) == {main}

        wa.send_json({"op": "focus", "channel_id": None})
        # Confirm via the SAME connection (per-connection op ordering): the
        # status broadcast landing proves the focus op before it was applied.
        wa.send_json({"op": "status", "status": "idle"})
        idle = {"type": "presence", "user_id": a, "status": "idle"}
        assert wa.receive_json() == idle
        assert wb.receive_json() == idle
        assert ws_module.manager.user_focused_channel_ids(a) == set()

    assert ws_module.manager.is_user_connected(a) is False
    assert ws_module.manager.is_user_connected(b) is False
