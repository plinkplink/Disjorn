"""WP7 tests: Web Push endpoints, prefs, notifier rules, dead-sub pruning.

`send_push` is monkeypatched (the `sent` fixture) for all notifier-rule tests;
the pruning tests go one level deeper and monkeypatch `pywebpush.webpush` as
imported by services/push.py. WS focus/connection state is faked by
monkeypatching the ws.manager singleton — the notifier only consults
`is_user_connected` / `user_focused_channel_ids`.

Notification tasks are fire-and-forget; tests call notifications.wait_pending()
after posting to make delivery deterministic.
"""

import json
from types import SimpleNamespace

import pytest
from pywebpush import WebPushException

from app import db
from app import ws as ws_module
from app.config import reset_settings_cache
from app.routers import auth, notifications
from app.services import push

PASSWORD = "correct horse battery staple"
PASSWORD_HASH = auth.hash_password(PASSWORD)  # hash once — argon2 is slow
KEYS = {"p256dh": "client-p256dh", "auth": "client-auth"}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def make_user(username: str, display_name: str | None = None) -> int:
    cur = await db.execute(
        "INSERT INTO users (username, password_hash, display_name) VALUES (?, ?, ?)",
        (username, PASSWORD_HASH, display_name or username.capitalize()),
    )
    return cur.lastrowid


async def login(client, username: str) -> str:
    """Login and return the raw session token; the shared jar is cleared so
    each request picks its identity via an explicit Cookie header."""
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


async def add_sub(user_id: int, endpoint: str) -> int:
    cur = await db.execute(
        """INSERT INTO push_subscriptions (user_id, endpoint, keys_json, created_at)
           VALUES (?, ?, ?, ?)""",
        (user_id, endpoint, json.dumps(KEYS), db.utc_now()),
    )
    return cur.lastrowid


async def main_feed_id() -> int:
    row = await db.fetch_one("SELECT id FROM channels WHERE type = 'main_feed'")
    assert row is not None
    return row["id"]


async def make_dm(client, token: str, other_user_id: int) -> int:
    r = await client.post("/dms", json={"user_id": other_user_id}, headers=cookie(token))
    assert r.status_code == 200, r.text
    return r.json()["id"]


async def post_msg(client, token: str, channel_id: int, content: str) -> dict:
    r = await client.post(
        f"/channels/{channel_id}/messages",
        json={"content": content},
        headers=cookie(token),
    )
    assert r.status_code == 200, r.text
    await notifications.wait_pending()  # notifier is fire-and-forget
    return r.json()


@pytest.fixture
def sent(monkeypatch):
    """Monkeypatch services.push.send_push with a sync recorder."""
    calls: list[dict] = []

    def fake_send_push(row, payload):
        calls.append(
            {"user_id": row["user_id"], "endpoint": row["endpoint"], "payload": payload}
        )

    monkeypatch.setattr(push, "send_push", fake_send_push)
    return calls


def notified_user_ids(sent) -> set[int]:
    return {c["user_id"] for c in sent}


# ---------------------------------------------------------------------------
# GET /vapid-public-key
# ---------------------------------------------------------------------------

async def test_vapid_public_key_503_when_unset(client):
    r = await client.get("/vapid-public-key")
    assert r.status_code == 503
    assert "VAPID_PUBLIC_KEY" in r.json()["detail"]
    assert "gen-vapid" in r.json()["detail"]


async def test_vapid_public_key_returned_when_set(client, monkeypatch):
    monkeypatch.setenv("VAPID_PUBLIC_KEY", "test-public-key")
    reset_settings_cache()
    r = await client.get("/vapid-public-key")
    assert r.status_code == 200
    assert r.json() == {"key": "test-public-key"}


# ---------------------------------------------------------------------------
# Subscribe / unsubscribe
# ---------------------------------------------------------------------------

async def test_subscribe_endpoints_require_auth(client):
    r = await client.post(
        "/push/subscribe", json={"endpoint": "https://p.example/1", "keys": KEYS}
    )
    assert r.status_code == 401
    r = await client.request(
        "DELETE", "/push/subscribe", json={"endpoint": "https://p.example/1"}
    )
    assert r.status_code == 401
    assert (await client.get("/notify-prefs")).status_code == 401
    r = await client.put("/notify-prefs", json={"notify_all_main": True})
    assert r.status_code == 401


async def test_subscribe_upsert_and_unsubscribe_scoping(client):
    alice = await make_user("alice")
    bob = await make_user("bob")
    ta, tb = await login(client, "alice"), await login(client, "bob")
    e1, e2 = "https://p.example/sub-1", "https://p.example/sub-2"

    # Subscribe + upsert on the same endpoint: one row, newest keys/owner win.
    r = await client.post(
        "/push/subscribe", json={"endpoint": e1, "keys": KEYS}, headers=cookie(ta)
    )
    assert r.status_code == 200 and r.json() == {"ok": True}
    r = await client.post(
        "/push/subscribe",
        json={"endpoint": e1, "keys": {"p256dh": "rotated", "auth": "rotated"}},
        headers=cookie(ta),
    )
    assert r.status_code == 200
    rows = await db.fetch_all("SELECT * FROM push_subscriptions")
    assert len(rows) == 1
    assert rows[0]["user_id"] == alice
    assert json.loads(rows[0]["keys_json"]) == {"p256dh": "rotated", "auth": "rotated"}

    # Unsubscribe is scoped to the caller: bob cannot remove alice's endpoint.
    r = await client.post(
        "/push/subscribe", json={"endpoint": e2, "keys": KEYS}, headers=cookie(tb)
    )
    assert r.status_code == 200
    r = await client.request(
        "DELETE", "/push/subscribe", json={"endpoint": e1}, headers=cookie(tb)
    )
    assert r.json() == {"ok": True, "removed": False}
    assert len(await db.fetch_all("SELECT * FROM push_subscriptions")) == 2

    r = await client.request(
        "DELETE", "/push/subscribe", json={"endpoint": e2}, headers=cookie(tb)
    )
    assert r.json() == {"ok": True, "removed": True}
    rows = await db.fetch_all("SELECT * FROM push_subscriptions")
    assert [row["endpoint"] for row in rows] == [e1]


# ---------------------------------------------------------------------------
# Notify prefs
# ---------------------------------------------------------------------------

async def test_notify_prefs_roundtrip(client):
    await make_user("alice")
    ta = await login(client, "alice")

    r = await client.get("/notify-prefs", headers=cookie(ta))
    assert r.status_code == 200
    assert r.json() == {"notify_all_main": False}  # migration default: off

    r = await client.put(
        "/notify-prefs", json={"notify_all_main": True}, headers=cookie(ta)
    )
    assert r.status_code == 200 and r.json() == {"notify_all_main": True}
    r = await client.get("/notify-prefs", headers=cookie(ta))
    assert r.json() == {"notify_all_main": True}

    r = await client.put(
        "/notify-prefs", json={"notify_all_main": False}, headers=cookie(ta)
    )
    assert r.status_code == 200
    r = await client.get("/notify-prefs", headers=cookie(ta))
    assert r.json() == {"notify_all_main": False}


# ---------------------------------------------------------------------------
# Notifier: DM rules
# ---------------------------------------------------------------------------

async def test_dm_message_notifies_offline_member_but_never_author(client, sent):
    alice = await make_user("alice")
    bob = await make_user("bob")
    ta = await login(client, "alice")
    await add_sub(alice, "https://p.example/alice")
    await add_sub(bob, "https://p.example/bob")
    dm = await make_dm(client, ta, bob)

    msg = await post_msg(client, ta, dm, "psst bob, **secret plans**")

    assert notified_user_ids(sent) == {bob}  # author alice excluded
    [call] = sent
    assert call["payload"] == {
        "title": "Alice",                          # DM: author display name only
        "body": "psst bob, secret plans",          # markdown roughly stripped
        "channel_id": dm,
        "message_id": msg["id"],
        "url": f"/channels/{dm}",
    }


async def test_focused_member_not_notified_but_unfocused_connection_is(
    client, sent, monkeypatch
):
    alice = await make_user("alice")
    bob = await make_user("bob")
    ta = await login(client, "alice")
    await add_sub(bob, "https://p.example/bob")
    dm = await make_dm(client, ta, bob)
    main = await main_feed_id()

    # Bob connected AND focused on the DM -> suppressed.
    monkeypatch.setattr(
        ws_module.manager, "is_user_connected", lambda uid: uid == bob
    )
    monkeypatch.setattr(
        ws_module.manager,
        "user_focused_channel_ids",
        lambda uid: {dm} if uid == bob else set(),
    )
    await post_msg(client, ta, dm, "are you reading this?")
    assert sent == []

    # Connected but focused elsewhere -> notified (connected alone is not enough).
    monkeypatch.setattr(
        ws_module.manager,
        "user_focused_channel_ids",
        lambda uid: {main} if uid == bob else set(),
    )
    await post_msg(client, ta, dm, "now you are not looking")
    assert notified_user_ids(sent) == {bob}


# ---------------------------------------------------------------------------
# Notifier: main-feed rules (mentions + notify_all_main pref)
# ---------------------------------------------------------------------------

async def test_mention_in_main_feed_notifies_nonpref_user(client, sent):
    await make_user("alice")
    bob = await make_user("bob")           # display name "Bob"; pref off
    carol = await make_user("carol")       # no mention, pref off
    ta = await login(client, "alice")
    await add_sub(bob, "https://p.example/bob")
    await add_sub(carol, "https://p.example/carol")
    main = await main_feed_id()

    # @username mention.
    msg = await post_msg(client, ta, main, "hey @bob look at this")
    assert notified_user_ids(sent) == {bob}
    assert sent[0]["payload"]["title"] == "Alice in #main"
    assert sent[0]["payload"]["url"] == f"/channels/{main}"
    assert sent[0]["payload"]["message_id"] == msg["id"]
    sent.clear()

    # Bare display name as a word, case-insensitive.
    await post_msg(client, ta, main, "maybe BOB knows")
    assert notified_user_ids(sent) == {bob}
    sent.clear()

    # Substring is not a mention.
    await post_msg(client, ta, main, "bobby and the bobcats")
    assert sent == []


async def test_main_feed_respects_notify_all_main_pref(client, sent):
    await make_user("alice")
    bob = await make_user("bob")
    ta, tb = await login(client, "alice"), await login(client, "bob")
    await add_sub(bob, "https://p.example/bob")
    main = await main_feed_id()

    # Pref off (default): a non-mention main-feed message notifies nobody.
    await post_msg(client, ta, main, "morning everyone")
    assert sent == []

    # Pref on: every main-feed message notifies (unless focused/author).
    r = await client.put(
        "/notify-prefs", json={"notify_all_main": True}, headers=cookie(tb)
    )
    assert r.status_code == 200
    await post_msg(client, ta, main, "afternoon everyone")
    assert notified_user_ids(sent) == {bob}
    assert sent[0]["payload"]["body"] == "afternoon everyone"


# ---------------------------------------------------------------------------
# Payload building units
# ---------------------------------------------------------------------------

def test_snippet_strips_markdown_and_truncates():
    assert notifications._snippet("**bold** and `code` and ~~gone~~") == (
        "bold and code and gone"
    )
    assert notifications._snippet("look:\n```py\nprint(1)\n```\ndone") == (
        "look: [code] done"
    )
    long = "word " * 60
    snip = notifications._snippet(long)
    assert len(snip) <= 120 and snip.endswith("…")


def test_attachment_only_message_body():
    channel = {"id": 7, "type": "dm_1to1", "name": None}
    message = {
        "id": 42,
        "content": "",
        "author": {"name": "Alice"},
        "attachments": [{"id": 1}],
    }
    payload = notifications._build_payload(channel, message)
    assert payload["body"] == "📎 attachment"
    assert payload["title"] == "Alice"
    assert payload["url"] == "/channels/7"


# ---------------------------------------------------------------------------
# Dead-subscription pruning (mocks pywebpush.webpush, not send_push)
# ---------------------------------------------------------------------------

def _webpush_raising(status_code: int):
    def fake_webpush(**kwargs):
        raise WebPushException(
            f"push failed ({status_code})",
            response=SimpleNamespace(status_code=status_code),
        )

    return fake_webpush


async def test_dead_subscription_pruned_on_410(client, monkeypatch):
    monkeypatch.setenv("VAPID_PRIVATE_KEY", "test-private-key")
    reset_settings_cache()
    bob = await make_user("bob")
    await add_sub(bob, "https://p.example/dead")

    monkeypatch.setattr(push, "webpush", _webpush_raising(410))
    [row] = await db.fetch_all("SELECT * FROM push_subscriptions")
    await push.notify_users([bob], {"title": "x"})  # must not raise
    assert await db.fetch_all("SELECT * FROM push_subscriptions") == []

    # Transient failure (500) does NOT prune, and still never raises.
    await add_sub(bob, "https://p.example/flaky")
    monkeypatch.setattr(push, "webpush", _webpush_raising(500))
    await push.notify_users([bob], {"title": "x"})
    assert len(await db.fetch_all("SELECT * FROM push_subscriptions")) == 1


async def test_send_push_passes_vapid_config_and_payload(client, monkeypatch):
    monkeypatch.setenv("VAPID_PRIVATE_KEY", "test-private-key")
    monkeypatch.setenv("VAPID_CLAIMS_EMAIL", "mailto:plink@example.com")
    reset_settings_cache()
    bob = await make_user("bob")
    await add_sub(bob, "https://p.example/live")

    seen: dict = {}
    monkeypatch.setattr(push, "webpush", lambda **kw: seen.update(kw))
    [row] = await db.fetch_all("SELECT * FROM push_subscriptions")
    await push.send_push(row, {"title": "hello"})

    assert seen["subscription_info"] == {
        "endpoint": "https://p.example/live",
        "keys": KEYS,
    }
    assert json.loads(seen["data"]) == {"title": "hello"}
    assert seen["vapid_private_key"] == "test-private-key"
    assert seen["vapid_claims"] == {"sub": "mailto:plink@example.com"}


async def test_send_push_skipped_without_private_key(client, monkeypatch):
    # Default test env has no VAPID keys: send_push logs + returns, no webpush call.
    bob = await make_user("bob")
    await add_sub(bob, "https://p.example/nokey")
    called = []
    monkeypatch.setattr(push, "webpush", lambda **kw: called.append(kw))
    [row] = await db.fetch_all("SELECT * FROM push_subscriptions")
    await push.send_push(row, {"title": "x"})
    assert called == []
    assert len(await db.fetch_all("SELECT * FROM push_subscriptions")) == 1
