"""Disjorn FastAPI application.

Lifespan: connect DB (WAL), run numbered migrations, seed the main_feed
channel (idempotent). Includes every router; WP1 ships them as empty stubs
that later WPs fill in. Search endpoints live in routers/messages.py (WP4),
so there is no separate search router.
"""

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exception_handlers import http_exception_handler
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import db, ws
from .config import get_settings
from .routers import (
    auth,
    bots_admin,
    channels,
    media,
    messages,
    notifications,
    stt,
    summarize,
)

logger = logging.getLogger(__name__)

MAIN_FEED_NAME = "main"

# Built PWA output (WP14). If absent (dev/tests without a client build) the
# app serves the API only and skips static mounting entirely.
CLIENT_DIST = Path(__file__).resolve().parents[2] / "client" / "dist"

# Top-level path segments owned by the API. The SPA fallback never swallows
# these, so unknown API paths keep returning JSON 404s instead of index.html.
API_PREFIXES = frozenset(
    {
        "auth",
        "me",
        "channels",
        "dms",
        "messages",
        "search",
        "media",
        "avatars",
        "picker",
        "upload",
        "attachments",
        "unfurl",
        "summarize",
        "stt",
        "chibi",
        "push",
        "vapid-public-key",
        "notify-prefs",
        "healthz",
        "ws",
        "bots",
    }
)


def mount_client(app: FastAPI) -> None:
    """Serve the built client (client/dist) with an SPA fallback.

    /assets and /icons are mounted as static directories. Everything else is
    handled by a 404 exception handler: unmatched GETs outside API_PREFIXES
    serve a root-level dist file if one exists (sw.js, manifest.webmanifest,
    favicons) and fall back to index.html for SPA deep links. API paths keep
    their JSON 404s. A handler (rather than a catch-all route) is used so it
    never shadows API routes — including ones registered after startup.

    No-op when the build output is missing, keeping dev servers and tests
    unaffected.
    """
    index_html = CLIENT_DIST / "index.html"
    if not index_html.is_file():
        logger.info("client/dist not found — static serving disabled (API only)")
        return

    for subdir in ("assets", "icons"):
        directory = CLIENT_DIST / subdir
        if directory.is_dir():
            app.mount(f"/{subdir}", StaticFiles(directory=directory), name=f"client-{subdir}")

    @app.exception_handler(404)
    async def spa_fallback(request: Request, exc: StarletteHTTPException) -> Response:
        path = request.url.path.lstrip("/")
        top = path.split("/", 1)[0]
        if request.method in ("GET", "HEAD") and top not in API_PREFIXES:
            if path and ".." not in path.split("/"):
                candidate = CLIENT_DIST / path
                if candidate.is_file():  # root-level files: sw.js, manifest, favicons…
                    media_type = (
                        "application/manifest+json" if candidate.suffix == ".webmanifest" else None
                    )
                    return FileResponse(candidate, media_type=media_type)
            return FileResponse(index_html)
        return await http_exception_handler(request, exc)


async def seed_main_feed() -> int:
    """Ensure exactly one main_feed channel exists; return its id. Idempotent."""
    row = await db.fetch_one("SELECT id FROM channels WHERE type = 'main_feed'")
    if row is not None:
        return row["id"]
    cur = await db.execute(
        "INSERT INTO channels (type, name) VALUES ('main_feed', ?)",
        (MAIN_FEED_NAME,),
    )
    logger.info("seeded main_feed channel id=%s", cur.lastrowid)
    return cur.lastrowid


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    await db.connect()
    applied = await db.run_migrations()
    if applied:
        logger.info("applied migrations: %s", ", ".join(applied))
    await seed_main_feed()
    ws.init()  # WP5: reset hub state + (re-)subscribe fan-out to the event bus
    notifications.init()  # WP7: subscribe the Web Push notifier to the event bus
    yield
    await db.close()


def create_app() -> FastAPI:
    app = FastAPI(title="Disjorn", lifespan=lifespan)

    app.include_router(auth.router)
    app.include_router(channels.router)
    app.include_router(messages.router)
    app.include_router(media.router)
    app.include_router(notifications.router)
    app.include_router(bots_admin.router)
    app.include_router(stt.router)
    app.include_router(summarize.router)
    app.include_router(ws.router)

    @app.get("/healthz")
    async def healthz() -> dict[str, bool]:
        return {"ok": True}

    mount_client(app)

    return app


app = create_app()
