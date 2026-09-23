"""Shared test fixtures: tmp SQLite per test, app instance, httpx AsyncClient.

The `app` fixture runs the FastAPI lifespan (migrations + main_feed seed), so
any test using `app`/`client` has a fully migrated fresh database and a live
db.py shared connection.
"""

import json

import httpx
import pytest

from app import db, events
from app.config import reset_settings_cache

# The house origin for the whole suite. It is also the client's base_url, so
# every test request is same-origin: Secure cookies flow (httpx returns them
# only over https), and the Origin wall sees an allowed Origin. Both are
# required — startup refuses COOKIE_SECURE=false and an empty HOUSE_ORIGINS,
# and the wall 403s a cookie-bearing unsafe request whose Origin is absent or
# foreign.
HOUSE_ORIGIN = "https://test"


@pytest.fixture
def tmp_db_path(tmp_path, monkeypatch):
    """Point config at a fresh SQLite file + data dir under tmp_path."""
    db_path = tmp_path / "disjorn.db"
    monkeypatch.setenv("DB_PATH", str(db_path))
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    # Settings also read server/.env (production deployment values). Env vars
    # take priority in pydantic-settings, so pin the test-critical ones here:
    # the two boot-required security values, and the keyless VAPID default the
    # notification tests assume.
    monkeypatch.setenv("COOKIE_SECURE", "true")
    monkeypatch.setenv("HOUSE_ORIGINS", json.dumps([HOUSE_ORIGIN]))
    monkeypatch.setenv("VAPID_PUBLIC_KEY", "")
    monkeypatch.setenv("VAPID_PRIVATE_KEY", "")
    reset_settings_cache()
    yield db_path
    reset_settings_cache()


@pytest.fixture
async def app(tmp_db_path):
    """App instance with lifespan running (DB connected, migrated, seeded)."""
    # Isolate global state: bus subscribers + any leftover connection.
    events.clear_subscribers()
    await db.close()

    from app.main import create_app

    application = create_app()
    async with application.router.lifespan_context(application):
        yield application

    await db.close()
    events.clear_subscribers()


@pytest.fixture
def house_origin() -> str:
    return HOUSE_ORIGIN


@pytest.fixture
async def client(app):
    """httpx AsyncClient wired to the app via ASGI transport."""
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url=HOUSE_ORIGIN, headers={"Origin": HOUSE_ORIGIN}
    ) as c:
        yield c
