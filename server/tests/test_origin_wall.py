"""Boot assertions and the Origin wall (app/origin_wall.py).

The wall is keyed on the session cookie, so every test here is about which
requests carry one: a cookie-bearing write or /ws handshake needs a house
Origin, and everything else — bots on X-Api-Key, an unauthenticated login —
must be untouched by it.

APPS_ORIGIN is the whole point of the wall: same host, different port, and
cookies ignore ports, so untrusted app JS there shares this cookie jar.
"""

import asyncio

import httpx
import pytest
from starlette.testclient import TestClient, WebSocketDenialResponse

from app import db, events
from app import origin_wall
from app.config import reset_settings_cache
from app.routers import auth

from .conftest import HOUSE_ORIGIN

PASSWORD = "correct horse battery staple"
APPS_ORIGIN = "https://test:8443"
BOT_KEY = "origin-wall-bot-key"


async def make_user(username: str = "alice") -> int:
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


async def login(client) -> httpx.Response:
    return await client.post(
        "/auth/login", json={"username": "alice", "password": PASSWORD}
    )


@pytest.fixture
async def bare_client(app):
    """Like `client`, but with no default Origin — the wall's strict case."""
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url=HOUSE_ORIGIN) as c:
        yield c


# ---------------------------------------------------------------------------
# Boot assertions
# ---------------------------------------------------------------------------

async def boot() -> None:
    """Run the app's startup, the way uvicorn does. Raises what it refuses on."""
    from app.main import create_app

    application = create_app()
    async with application.router.lifespan_context(application):
        pass


async def test_boot_refuses_insecure_cookie(tmp_db_path, monkeypatch):
    monkeypatch.setenv("COOKIE_SECURE", "false")
    reset_settings_cache()
    with pytest.raises(RuntimeError, match="COOKIE_SECURE"):
        await boot()


async def test_boot_refuses_empty_house_origins(tmp_db_path, monkeypatch):
    monkeypatch.setenv("HOUSE_ORIGINS", "[]")
    reset_settings_cache()
    with pytest.raises(RuntimeError, match="HOUSE_ORIGINS"):
        await boot()


async def test_boot_accepts_a_configured_house(tmp_db_path):
    await boot()


# ---------------------------------------------------------------------------
# HTTP: cookie + unsafe method
# ---------------------------------------------------------------------------

async def test_cookie_write_from_house_origin_passes(client):
    await make_user()
    await login(client)
    r = await client.patch("/me", json={"display_name": "Allie"})
    assert r.status_code == 200


async def test_cookie_write_from_apps_origin_rejected(client):
    await make_user()
    await login(client)
    r = await client.patch(
        "/me", json={"display_name": "Mallory"}, headers={"Origin": APPS_ORIGIN}
    )
    assert r.status_code == 403
    assert r.json()["detail"] == origin_wall.REJECTED
    # The write never reached the route.
    assert (await client.get("/me")).json()["display_name"] == "Alice"


async def test_cookie_write_without_origin_rejected(bare_client):
    await make_user()
    await login(bare_client)  # no cookie on the way in — the wall lets it through
    r = await bare_client.patch("/me", json={"display_name": "Mallory"})
    assert r.status_code == 403
    assert r.json()["detail"] == origin_wall.REJECTED


async def test_cookie_read_without_origin_passes(bare_client):
    """Safe methods are outside the wall: only unsafe ones and /ws are gated."""
    await make_user()
    await login(bare_client)
    assert (await bare_client.get("/me")).status_code == 200


# ---------------------------------------------------------------------------
# HTTP: no cookie
# ---------------------------------------------------------------------------

async def test_api_key_bodyless_posts_without_origin_pass(bare_client):
    """The body-less POSTs a browser fires WITHOUT a preflight, key-authenticated.

    They are why the wall is keyed on the cookie rather than mounted per route:
    a route-wide check would 403 every bot on exactly these calls. None of them
    may answer with the wall's refusal — what the route itself says (401 for a
    bot on a user-only verb) is the route's business, not the wall's.
    """
    await make_bot()
    row = await db.fetch_one("SELECT id FROM channels WHERE type = 'main_feed'")
    paths = [
        f"/channels/{row['id']}/leave",
        f"/channels/{row['id']}/join",
        "/auth/logout",
    ]
    for path in paths:
        r = await bare_client.post(path, headers={"X-Api-Key": BOT_KEY})
        assert r.status_code != 403, path
        assert origin_wall.REJECTED not in r.text, path


async def test_api_key_write_without_origin_passes(bare_client):
    """A bot's real write, unchanged by the wall: no cookie, no Origin, 200."""
    bot_id = await make_bot()
    row = await db.fetch_one("SELECT id FROM channels WHERE type = 'main_feed'")
    await db.execute(
        "INSERT INTO channel_members (channel_id, member_type, member_id) "
        "VALUES (?, 'bot', ?)",
        (row["id"], bot_id),
    )
    r = await bare_client.post(
        f"/channels/{row['id']}/messages",
        json={"content": "still here"},
        headers={"X-Api-Key": BOT_KEY},
    )
    assert r.status_code == 200, r.text


async def test_login_without_origin_passes(bare_client):
    """No cookie on the way in, so nothing to protect: login must still work."""
    await make_user()
    assert (await login(bare_client)).status_code == 200


# ---------------------------------------------------------------------------
# WebSocket handshake
# ---------------------------------------------------------------------------

@pytest.fixture
def wsc(tmp_db_path):
    """Sync TestClient with NO default Origin — each connect sets its own."""
    events.clear_subscribers()
    asyncio.run(db.close())

    from app.main import create_app

    with TestClient(create_app()) as client:
        yield client
    events.clear_subscribers()


def ws_login(wsc) -> str:
    wsc.portal.call(
        db.execute,
        "INSERT INTO users (username, password_hash, display_name) VALUES (?, ?, ?)",
        ("alice", auth.hash_password(PASSWORD), "Alice"),
    )
    r = wsc.post("/auth/login", json={"username": "alice", "password": PASSWORD})
    assert r.status_code == 200, r.text
    token = r.cookies.get(auth.COOKIE_NAME)
    wsc.cookies.clear()
    assert token
    return token


def cookie(token: str) -> dict[str, str]:
    return {"cookie": f"{auth.COOKIE_NAME}={token}"}


def test_ws_cookie_handshake_from_apps_origin_rejected(wsc):
    token = ws_login(wsc)
    with pytest.raises(WebSocketDenialResponse) as exc:
        with wsc.websocket_connect(
            "/ws", headers={**cookie(token), "origin": APPS_ORIGIN}
        ):
            pass
    # A denial response IS the proof it was rejected before accept: an accepted
    # socket can only be refused afterwards, with a close code.
    assert exc.value.status_code == 403


def test_ws_cookie_handshake_without_origin_rejected(wsc):
    token = ws_login(wsc)
    with pytest.raises(WebSocketDenialResponse) as exc:
        with wsc.websocket_connect("/ws", headers=cookie(token)):
            pass
    assert exc.value.status_code == 403


def test_ws_cookie_handshake_from_house_origin_connects(wsc):
    token = ws_login(wsc)
    with wsc.websocket_connect(
        "/ws", headers={**cookie(token), "origin": HOUSE_ORIGIN}
    ) as ws:
        assert ws.receive_json()["type"] == "ready"


def test_ws_bot_auth_frame_without_origin_connects(wsc):
    bot_id = wsc.portal.call(
        db.execute,
        "INSERT INTO bots (name, api_key_hash) VALUES (?, ?)",
        ("claw", auth.hash_api_key(BOT_KEY)),
    ).lastrowid
    with wsc.websocket_connect("/ws") as ws:
        ws.send_json({"op": "auth", "api_key": BOT_KEY})
        assert ws.receive_json() == {"type": "ready", "bot_id": bot_id}
