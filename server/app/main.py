"""Disjorn FastAPI application.

Lifespan: connect DB (WAL), run numbered migrations, seed the main_feed
channel (idempotent). Includes every router; WP1 ships them as empty stubs
that later WPs fill in. Search endpoints live in routers/messages.py (WP4),
so there is no separate search router.

Also home to the app-wide 422 handler (see `validation_error_body`): request
validation failures answer with a flat human-readable `detail` string, the
same shape every HTTPException already uses, instead of FastAPI's default
list-of-dicts.
"""

import logging
import re
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exception_handlers import http_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response
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
    slash,
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
        "backlog",
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


# ---------------------------------------------------------------------------
# 422 request-validation bodies
# ---------------------------------------------------------------------------
#
# FastAPI's default RequestValidationError handler answers with
# `{"detail": [ {loc, msg, type, input, url}, ... ]}` — a LIST, and one whose
# entries echo the rejected `input` verbatim. Two problems:
#
#   1. Every consumer (client api.ts, the SDK, curl) reads `detail` as a
#      string, because that is what every HTTPException in this codebase
#      produces. A list therefore degrades to the bare status text
#      ("Unprocessable Entity") and the user learns nothing — the observed
#      symptom when pasting an over-length message.
#   2. `input` is the submitted value. On `POST /channels/{id}/messages` that
#      is the user's message text, so the default body quotes protected
#      content back into responses and any log that records them. House rule:
#      refusals never echo protected content.
#
# So: keep the 422 status, emit a flat sentence naming the field and the
# constraint, and build that sentence ONLY from (a) the sanitized field path,
# (b) the machine error type, and (c) server-authored constraint values from
# `ctx` (the max_length WE declared, the literals WE allow). The rejected
# value never reaches the response, and neither does pydantic's own `msg`
# (safe today, but it is not our string to audit forever).
#
# A bounded machine-readable `errors` list rides alongside for programmatic
# consumers — same three safe ingredients, no `input`.

_MAX_REPORTED_ERRORS = 5
_MAX_FIELD_SEGMENT = 40
_MAX_CONSTRAINT_CHARS = 200

# Field-path segments are echoed, so they are charset-restricted; anything
# outside this set becomes "?".
_UNSAFE_IN_FIELD = re.compile(r"[^A-Za-z0-9_.-]")

# Stand-in for a field-path segment the CALLER named rather than the server.
_OPAQUE_SEGMENT = "<key>"

# Type-slug prefix -> human noun, for the *_type / *_parsing family.
_TYPE_NOUNS = {
    "string": "string",
    "int": "integer",
    "float": "number",
    "decimal": "number",
    "bool": "boolean",
    "list": "list",
    "set": "list",
    "tuple": "list",
    "dict": "object",
    "model_attributes": "object",
    "datetime": "datetime",
    "date": "date",
    "time": "time",
    "uuid": "UUID",
    "url": "URL",
    "bytes": "byte string",
}


def _field_name(loc: tuple[Any, ...]) -> str:
    """Readable, sanitized field path for an error location.

    FastAPI puts the source first ("body", "query", "path", "header",
    "cookie"), then the field path. List indices render as `[0]`.

    Only the first two string segments are echoed, because only those are
    server-declared: the source, then the top-level field/parameter/header
    name from a request model. Anything deeper is either a list index (safe —
    an integer) or a mapping key the CALLER chose (this API has `dict[str, …]`
    fields), which could carry content and is therefore replaced with
    `<key>`. Losing it costs nothing: "body.keys.<key> must be a valid string"
    is as actionable as the real key would have been.
    """
    if not loc:
        return "request"
    out = ""
    named = 0
    for part in loc:
        if isinstance(part, int):
            out += f"[{part}]"
            continue
        named += 1
        if named > 2:
            segment = _OPAQUE_SEGMENT
        else:
            segment = _UNSAFE_IN_FIELD.sub("?", str(part))[:_MAX_FIELD_SEGMENT]
        out = f"{out}.{segment}" if out else segment
    return out or "request"


def _plural(count: Any, noun: str) -> str:
    return f"{count} {noun}" if count == 1 else f"{count} {noun}s"


def _constraint(error: dict[str, Any]) -> str:
    """Human phrase for one pydantic error — never derived from the input value.

    Only `ctx` values that the *server* declared are interpolated (max_length,
    the allowed literals, a custom validator's own message). Anything not
    recognized degrades to the machine type slug, which is a fixed pydantic
    vocabulary, not caller text.

    NB for future validators: a `ValueError` raised in a model_validator has
    its message surfaced here, so validator messages must not interpolate the
    submitted value.
    """
    etype = str(error.get("type", "") or "invalid")
    ctx = error.get("ctx") or {}

    def c(key: str) -> Any:
        return ctx.get(key)

    if etype == "missing":
        return "is required"
    if etype == "extra_forbidden":
        return "is not a recognized field"
    if etype == "json_invalid":
        return "is not valid JSON"
    if etype == "string_too_long":
        return f"must be at most {_plural(c('max_length'), 'character')}"
    if etype == "string_too_short":
        return f"must be at least {_plural(c('min_length'), 'character')}"
    if etype == "too_long":
        return f"must have at most {_plural(c('max_length'), 'item')}"
    if etype == "too_short":
        return f"must have at least {_plural(c('min_length'), 'item')}"
    if etype == "greater_than":
        return f"must be greater than {c('gt')}"
    if etype == "greater_than_equal":
        return f"must be greater than or equal to {c('ge')}"
    if etype == "less_than":
        return f"must be less than {c('lt')}"
    if etype == "less_than_equal":
        return f"must be less than or equal to {c('le')}"
    if etype == "string_pattern_mismatch":
        return f"must match the pattern {c('pattern')}"
    if etype in ("literal_error", "enum"):
        return f"must be one of: {c('expected')}"
    if etype == "value_error":
        # Raised by our own model_validators; the message is server-authored.
        return str(c("error") or "is invalid")[:_MAX_CONSTRAINT_CHARS]
    for prefix, noun in _TYPE_NOUNS.items():
        if etype.startswith(f"{prefix}_"):
            return f"must be a valid {noun}"
    return f"failed validation ({etype})"


def validation_error_body(errors: list[dict[str, Any]]) -> dict[str, Any]:
    """Build the 422 response body from pydantic's error list.

    Returns `{"detail": "<one flat sentence>", "errors": [{field, type,
    message}, ...]}`. Both are bounded (`_MAX_REPORTED_ERRORS`) and neither
    contains any part of the submitted payload.
    """
    reported = errors[:_MAX_REPORTED_ERRORS]
    parts: list[dict[str, str]] = []
    for error in reported:
        loc = tuple(error.get("loc") or ())
        if error.get("type") == "json_invalid":
            # loc is ("body", <byte offset>) — the offset is noise, not a field.
            loc = loc[:1]
        field = _field_name(loc)
        message = _constraint(error)
        parts.append(
            {"field": field, "type": str(error.get("type", "") or ""), "message": message}
        )

    if not parts:
        detail = "Invalid request."
    else:
        detail = "Invalid request: " + "; ".join(
            f"{p['field']} {p['message']}" for p in parts
        )
        hidden = len(errors) - len(reported)
        if hidden > 0:
            detail += f"; (+{hidden} more validation error{'s' if hidden > 1 else ''})"
    return {"detail": detail, "errors": parts}


async def validation_exception_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    """422 with a flat, actionable, echo-free `detail` string."""
    return JSONResponse(status_code=422, content=validation_error_body(exc.errors()))


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
    slash.init()  # WP-L2: reset the in-process slash rate-limit window
    yield
    await db.close()


def create_app() -> FastAPI:
    # Uvicorn's default logging config only wires its own "uvicorn.*" loggers;
    # the root logger gets no handler, so app-level INFO records (applied
    # migrations, pruned push subscriptions, …) silently vanish. basicConfig
    # is a no-op when a root handler already exists (tests, embedders).
    logging.basicConfig(
        level=logging.INFO, format="%(levelname)s:     %(name)s - %(message)s"
    )

    app = FastAPI(title="Disjorn", lifespan=lifespan)

    # Flat-string 422 detail for every route (see validation_error_body).
    app.add_exception_handler(RequestValidationError, validation_exception_handler)

    app.include_router(auth.router)
    app.include_router(channels.router)
    app.include_router(messages.router)
    app.include_router(media.router)
    app.include_router(notifications.router)
    app.include_router(bots_admin.router)
    app.include_router(slash.router)
    app.include_router(stt.router)
    app.include_router(summarize.router)
    app.include_router(ws.router)

    @app.get("/healthz")
    async def healthz() -> dict[str, bool]:
        return {"ok": True}

    mount_client(app)

    return app


app = create_app()
