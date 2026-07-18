"""Shared test fixtures: tmp SQLite per test, app instance, httpx AsyncClient.

The `app` fixture runs the FastAPI lifespan (migrations + main_feed seed), so
any test using `app`/`client` has a fully migrated fresh database and a live
db.py shared connection.
"""

import httpx
import pytest

from app import db, events
from app.config import reset_settings_cache


@pytest.fixture
def tmp_db_path(tmp_path, monkeypatch):
    """Point config at a fresh SQLite file + data dir under tmp_path."""
    db_path = tmp_path / "disjorn.db"
    monkeypatch.setenv("DB_PATH", str(db_path))
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    # Settings also read server/.env (production deployment values). Env vars
    # take priority in pydantic-settings, so pin the test-critical ones here:
    # Secure cookies never flow over the http:// ASGI transport, and the
    # notification tests assume a keyless VAPID default.
    monkeypatch.setenv("COOKIE_SECURE", "false")
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
async def client(app):
    """httpx AsyncClient wired to the app via ASGI transport."""
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
