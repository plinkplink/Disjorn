"""Disjorn FastAPI application.

Lifespan: connect DB (WAL), run numbered migrations, seed the main_feed
channel (idempotent). Includes every router; WP1 ships them as empty stubs
that later WPs fill in. Search endpoints live in routers/messages.py (WP4),
so there is no separate search router.
"""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from . import db
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

    @app.get("/healthz")
    async def healthz() -> dict[str, bool]:
        return {"ok": True}

    return app


app = create_app()
