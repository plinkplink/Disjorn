"""APPS — the registry, the build sessions, and the stage stream (stage 1).

SPECS/2026-08-30-apps-tab-v1.md (confirmed by plink, #custodian seq 2211;
stage 1 opened at Round 12).

| method | path                          | who                          |
|--------|-------------------------------|------------------------------|
| GET    | /apps                         | user   — my menu + my quota   |
| GET    | /apps/discover                | user   — visible, not on menu |
| GET    | /apps/builders                | user   — the chooser's cards  |
| GET    | /apps/quota                   | user                          |
| POST   | /apps/sessions                | user   — start a build        |
| GET    | /apps/sessions/{id}           | owner                         |
| POST   | /apps/sessions/{id}/heartbeat | owner  — push the lock out    |
| POST   | /apps/sessions/{id}/end       | owner  — idempotent           |
| POST   | /apps/sessions/{id}/stage     | broker bot / admin            |
| PATCH  | /apps/{app_id}                | owner  — name, description    |
| POST   | /apps/{app_id}/menu           | user   — if visible to them   |
| DELETE | /apps/{app_id}/menu           | user   — not the owner's own  |

THE MODAL CHAT IS A REAL CHANNEL. A build session creates a channel of type
`app_build` with exactly two members — the owner and the builder bot — and
every message in it flows through the ordinary message path: seq allocation,
history, WS fan-out, typing, the privacy filter, the membership wall. This
router owns none of that and must never grow a copy of it. What it owns is the
registry row, the lock, the quota, and the stage stream.

THE SUMMON IS SERVER-ATTESTED, and it is not here. In an `app_build` channel
the WS hub attaches the `context` block to the member bot's frame for every
USER-authored message, name match or not (app/ws.py). That is the whole
resident integration: a builder resident already summons on `context is not
None`. Nothing in this file talks to a resident, and nothing in the harness
changed.

STAGE 1 IS A SUBSCRIPTION INTERFACE WITH NO PRODUCTION PUBLISHER. The stage
endpoint below is the only writer of `app_stage_events`, it is gated to the
broker seat and admins, and nothing in production calls it yet. The consumer —
the client store and its stage bar — is real, and a second subscriber must be
addable without touching the publisher.

NOT IN STAGE 1, and deliberately not stubbed: the apps-builder seat, the
serving gate and origin, the iframe's real src, the share dialog, remix file
copy, screenshots, the per-build token ceiling. Those are broker-enforced or
later-stage; a placeholder here that pretends to do one of them is worse than
its absence.
"""

import json
import logging
import re
import secrets
from base64 import b32encode
from datetime import datetime, time as dt_time, timedelta, timezone
from typing import Annotated, Any, Optional

from fastapi import APIRouter, Body, Depends, HTTPException
from pydantic import BaseModel, Field, model_validator

from .. import db, events
from ..config import get_settings, read_model_pin
from ..models import AppOut, AppStage, OpenSessionRef, User
from .auth import Actor, get_actor, get_current_user
from .messages import deliver_message
from .slash import SYSTEM_BOT_NAME

logger = logging.getLogger(__name__)

router = APIRouter()

CurrentUser = Annotated[User, Depends(get_current_user)]
CurrentActor = Annotated[Actor, Depends(get_actor)]

# D10: new apps are born unnamed-but-nameable. The owner renames via PATCH.
DEFAULT_APP_NAME = "Untitled app"
MAX_APP_NAME_CHARS = 60
MAX_APP_DESCRIPTION_CHARS = 300

# A stage event's `detail` is free-form (filenames, a note), so it is bounded
# the way messages bounds its metadata: an uncapped JSON field on a write path
# is a side channel with a database behind it.
MAX_STAGE_DETAIL_CHARS = 2000

# D4: 12 chars of lowercase base32 (RFC 4648 alphabet, lowercased), minted from
# secrets. 60 bits, and never sequential — an app id lands in a URL path
# segment on an origin shared by every app, and a sequential id there is a map
# of the neighbours.
APP_ID_CHARS = 12
APP_ID_RE = re.compile(r"^[a-z2-7]{12}$")
_APP_ID_ATTEMPTS = 5


# ---------------------------------------------------------------------------
# Time, quota, and the lock — all three read from the same clock
# ---------------------------------------------------------------------------

def _now() -> str:
    """UTC now in the house's stored format (db.utc_now's format).

    Every timestamp in this feature is written and compared as one of these
    strings. That is what makes `locked_until > now` a plain string comparison:
    the format is fixed-width, zero-padded and UTC, so lexical order IS
    chronological order. Do not introduce a second format here.
    """
    return db.utc_now()


def _iso(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _utc_day_start() -> str:
    """Midnight UTC today — the lower bound of the quota window (D5)."""
    today = datetime.now(timezone.utc).date()
    return _iso(datetime.combine(today, dt_time.min, tzinfo=timezone.utc))


def _next_utc_midnight() -> str:
    """When the meter resets. Shown to the user, so it is a real instant."""
    tomorrow = datetime.now(timezone.utc).date() + timedelta(days=1)
    return _iso(datetime.combine(tomorrow, dt_time.min, tzinfo=timezone.utc))


def _locked_until(now: str) -> str:
    """`now` + the configured TTL, as a comparable timestamp."""
    ttl = max(0, int(get_settings().APPS_SESSION_LOCK_TTL))
    parsed = datetime.strptime(now, "%Y-%m-%dT%H:%M:%S.%fZ").replace(
        tzinfo=timezone.utc
    )
    return _iso(parsed + timedelta(seconds=ttl))


# ---------------------------------------------------------------------------
# Shapes
# ---------------------------------------------------------------------------

class Quota(BaseModel):
    """The visible unit is SESSIONS STARTED per user per UTC day (D5).

    Counted straight off `app_sessions.started_at`; there is no counter table,
    so there is nothing to drift out of agreement with the sessions themselves.
    """

    cap: int
    used: int
    left: int
    resets_at: str


class BuilderOut(BaseModel):
    """One card in the chooser.

    `model` is READ FROM THE SEAT at request time (config.read_model_pin), and
    is None when the seat declares none — which the card renders as "model not
    declared by seat". There is no hardcoded model string in this codebase and
    there must never be one; a card that keeps naming last quarter's model is
    a card that lies without anyone being able to tell.
    """

    bot_id: int
    name: str
    avatar_url: Optional[str] = None
    model: Optional[str] = None
    builds_total: int = 0
    builds_live: int = 0


class StageEventOut(BaseModel):
    id: int
    session_id: int
    stage: AppStage
    detail: dict[str, Any] = Field(default_factory=dict)
    created_at: str


class SessionOut(BaseModel):
    id: int
    app: AppOut
    channel_id: int
    builder: BuilderOut
    started_at: str
    locked_until: str
    ended_at: Optional[str] = None
    stage: Optional[AppStage] = None
    stages: list[StageEventOut] = Field(default_factory=list)
    quota: Quota


class SessionCreate(BaseModel):
    builder_bot_id: int
    # Omitted -> a brand new app. Given -> continue building one I own, which
    # is why it is checked for ownership AND for a live lock before anything
    # is written.
    app_id: Optional[str] = None


class AppPatch(BaseModel):
    name: Optional[str] = Field(default=None, max_length=MAX_APP_NAME_CHARS)
    description: Optional[str] = Field(
        default=None, max_length=MAX_APP_DESCRIPTION_CHARS
    )


class StagePublish(BaseModel):
    stage: AppStage
    detail: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _bound_detail(self) -> "StagePublish":
        """Keep `detail` from becoming an uncapped side channel (messages.py's
        _bound_metadata, same reasoning, same shape)."""
        try:
            encoded = json.dumps(self.detail)
        except (TypeError, ValueError):
            raise ValueError("detail must be JSON-serializable") from None
        if len(encoded) > MAX_STAGE_DETAIL_CHARS:
            raise ValueError(
                f"detail exceeds {MAX_STAGE_DETAIL_CHARS} serialized characters"
            )
        return self


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------

def _check_app_id(app_id: str) -> str:
    """A path app id must look like one this server minted.

    404, not 400, and it never echoes the id: an unparseable id names no app,
    which is exactly what "not found" says, and repeating a caller's string
    back into a response body is how refusals start carrying content.
    """
    if not APP_ID_RE.fullmatch(app_id):
        raise HTTPException(status_code=404, detail="App not found")
    return app_id


async def _require_app(app_id: str) -> dict[str, Any]:
    _check_app_id(app_id)
    row = await db.fetch_one("SELECT * FROM apps WHERE id = ?", (app_id,))
    if row is None:
        raise HTTPException(status_code=404, detail="App not found")
    return row


def _require_app_owner(app: dict[str, Any], user: User) -> None:
    if app["owner_user_id"] != user.id:
        raise HTTPException(
            status_code=403, detail="Only the app's owner can change it"
        )


async def _mint_app_id() -> str:
    """A fresh, unused app id. Collision at 60 bits is not a real event; the
    retry is here so that if one ever happens it is a retry rather than a
    500 with a stack trace pointing at a UNIQUE constraint."""
    for _ in range(_APP_ID_ATTEMPTS):
        candidate = b32encode(secrets.token_bytes(8)).decode("ascii").rstrip("=")
        candidate = candidate.lower()[:APP_ID_CHARS]
        if await db.fetch_one("SELECT 1 FROM apps WHERE id = ?", (candidate,)) is None:
            return candidate
    raise HTTPException(
        status_code=500, detail="Could not allocate an app id; please try again"
    )


async def _quota_for(user_id: int) -> Quota:
    cap = int(get_settings().APPS_DAILY_SESSION_CAP)
    row = await db.fetch_one(
        "SELECT COUNT(*) AS n FROM app_sessions WHERE user_id = ? AND started_at >= ?",
        (user_id, _utc_day_start()),
    )
    used = int(row["n"]) if row is not None else 0
    return Quota(
        cap=cap, used=used, left=max(0, cap - used), resets_at=_next_utc_midnight()
    )


async def _open_sessions_by_app(app_ids: list[str]) -> dict[str, dict[str, Any]]:
    """The live session on each of these apps, if any.

    "Live" is `ended_at IS NULL AND locked_until > now` (D6) — the same
    definition every other reader in this file uses. A session whose lock
    lapsed reads as no session at all, with no sweeper needed to make that
    true.
    """
    if not app_ids:
        return {}
    placeholders = ",".join("?" * len(app_ids))
    rows = await db.fetch_all(
        f"""SELECT id, app_id, channel_id, stage, locked_until FROM app_sessions
            WHERE app_id IN ({placeholders})
              AND ended_at IS NULL AND locked_until > ?
            ORDER BY id""",
        (*app_ids, _now()),
    )
    return {r["app_id"]: r for r in rows}


async def _menu_app_ids(user_id: int) -> set[str]:
    rows = await db.fetch_all(
        "SELECT app_id FROM user_apps WHERE user_id = ?", (user_id,)
    )
    return {r["app_id"] for r in rows}


def _app_out(
    row: dict[str, Any],
    user_id: int,
    menu_ids: set[str],
    open_sessions: dict[str, dict[str, Any]],
) -> AppOut:
    """One app, answered for one caller.

    `on_menu` is true for the owner without a `user_apps` row: your own app is
    in your APPS group by construction, which is the same fact that makes
    "remove my own app from my menu" a refusal rather than a no-op.
    """
    session = open_sessions.get(row["id"])
    return AppOut(
        id=row["id"],
        name=row["name"],
        description=row["description"],
        visibility=row["visibility"],
        status=row["status"],
        owner_user_id=row["owner_user_id"],
        builder_bot_id=row["builder_bot_id"],
        parent_app_id=row["parent_app_id"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        on_menu=row["owner_user_id"] == user_id or row["id"] in menu_ids,
        open_session=OpenSessionRef(
            id=session["id"],
            channel_id=session["channel_id"],
            stage=session["stage"],
            locked_until=session["locked_until"],
        )
        if session is not None
        else None,
    )


async def _apps_out(rows: list[dict[str, Any]], user_id: int) -> list[AppOut]:
    """Batched: one menu query and one open-session query for the whole list,
    rather than two per row."""
    menu_ids = await _menu_app_ids(user_id)
    open_sessions = await _open_sessions_by_app([r["id"] for r in rows])
    return [_app_out(r, user_id, menu_ids, open_sessions) for r in rows]


async def _app_out_one(row: dict[str, Any], user_id: int) -> AppOut:
    return (await _apps_out([row], user_id))[0]


# ---------------------------------------------------------------------------
# Builders — the seats offered in the chooser
# ---------------------------------------------------------------------------

def _builder_entries() -> list[dict[str, Any]]:
    """APPS_BUILDERS, defensively: config is hand-edited, and a malformed entry
    must cost that entry rather than the whole tab."""
    out: list[dict[str, Any]] = []
    for entry in get_settings().APPS_BUILDERS:
        if not isinstance(entry, dict):
            continue
        bot_id = entry.get("bot_id")
        if not isinstance(bot_id, int) or isinstance(bot_id, bool):
            continue
        out.append(
            {
                "bot_id": bot_id,
                "model_source": entry.get("model_source"),
                "model_key": entry.get("model_key"),
            }
        )
    return out


def _builder_entry(bot_id: int) -> Optional[dict[str, Any]]:
    return next((e for e in _builder_entries() if e["bot_id"] == bot_id), None)


async def _build_counts(bot_ids: list[int]) -> dict[int, tuple[int, int]]:
    if not bot_ids:
        return {}
    placeholders = ",".join("?" * len(bot_ids))
    rows = await db.fetch_all(
        f"""SELECT builder_bot_id,
                   COUNT(*) AS total,
                   COALESCE(SUM(status = 'live'), 0) AS live
              FROM apps WHERE builder_bot_id IN ({placeholders})
             GROUP BY builder_bot_id""",
        bot_ids,
    )
    return {r["builder_bot_id"]: (r["total"], r["live"]) for r in rows}


def _builder_out(
    bot: dict[str, Any], model_source: Any, model_key: Any, counts: tuple[int, int]
) -> BuilderOut:
    # Local import: media imports messages imports channels (channels.py's
    # list_members does the same dodge).
    from .media import bot_avatar_url

    total, live = counts
    return BuilderOut(
        bot_id=bot["id"],
        name=bot["name"],
        avatar_url=bot_avatar_url(bot["id"], bot["avatar_path"]),
        model=read_model_pin(
            model_source if isinstance(model_source, str) else None,
            model_key if isinstance(model_key, str) else None,
        ),
        builds_total=total,
        builds_live=live,
    )


async def _builder_for(bot_id: int) -> Optional[BuilderOut]:
    """The card for one builder, whether or not it is still in APPS_BUILDERS.

    A session created yesterday must still render its builder today, even if
    the seat has since been taken out of the chooser — so this resolves from
    the bots table and treats the settings entry as optional decoration.
    """
    bot = await db.fetch_one(
        "SELECT id, name, avatar_path FROM bots WHERE id = ?", (bot_id,)
    )
    if bot is None:
        return None
    entry = _builder_entry(bot_id)
    counts = (await _build_counts([bot_id])).get(bot_id, (0, 0))
    entry = entry or {}
    return _builder_out(bot, entry.get("model_source"), entry.get("model_key"), counts)


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------

async def _require_session(session_id: int, user: User) -> dict[str, Any]:
    row = await db.fetch_one("SELECT * FROM app_sessions WHERE id = ?", (session_id,))
    if row is None:
        raise HTTPException(status_code=404, detail="Build session not found")
    if row["user_id"] != user.id:
        raise HTTPException(
            status_code=403, detail="This build session belongs to someone else"
        )
    return row


def _require_session_open(session: dict[str, Any]) -> None:
    """410 on a session that is over — explicitly ended OR lock lapsed (D6).

    Gone, not forbidden: the session existed and the caller was entitled to it;
    what changed is that it is finished. 410 is the status that says so, and
    the modal turns it into "This session ended" with a dead composer.
    """
    if session["ended_at"] is not None or session["locked_until"] <= _now():
        raise HTTPException(status_code=410, detail="This build session has ended")


async def _stages_for(session_id: int) -> list[StageEventOut]:
    rows = await db.fetch_all(
        """SELECT id, session_id, stage, detail, created_at FROM app_stage_events
           WHERE session_id = ? ORDER BY id""",
        (session_id,),
    )
    out: list[StageEventOut] = []
    for r in rows:
        out.append(
            StageEventOut(
                id=r["id"],
                session_id=r["session_id"],
                stage=r["stage"],
                detail=_loads_detail(r["detail"]),
                created_at=r["created_at"],
            )
        )
    return out


def _loads_detail(raw: Optional[str]) -> dict[str, Any]:
    """A stored detail blob, defensively. It was bounded and JSON on the way
    in; a row that somehow is not stays a row rather than a 500."""
    try:
        value = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


async def _session_out(session: dict[str, Any], user: User) -> SessionOut:
    app = await db.fetch_one("SELECT * FROM apps WHERE id = ?", (session["app_id"],))
    if app is None:  # ON DELETE CASCADE means this cannot happen; fail loudly.
        raise HTTPException(status_code=404, detail="App not found")
    builder = await _builder_for(session["builder_bot_id"])
    if builder is None:
        # The seat's bot row is gone. The session is still real, so answer with
        # the id and nothing invented about a bot that no longer exists.
        builder = BuilderOut(bot_id=session["builder_bot_id"], name="unknown builder")
    return SessionOut(
        id=session["id"],
        app=await _app_out_one(app, user.id),
        channel_id=session["channel_id"],
        builder=builder,
        started_at=session["started_at"],
        locked_until=session["locked_until"],
        ended_at=session["ended_at"],
        stage=session["stage"],
        stages=await _stages_for(session["id"]),
        quota=await _quota_for(user.id),
    )


async def _system_bot_id() -> int:
    row = await db.fetch_one("SELECT id FROM bots WHERE name = ?", (SYSTEM_BOT_NAME,))
    if row is None:  # migration 006 seeds it
        raise RuntimeError("system bot not found (migration 006 not applied?)")
    return row["id"]


def _opener_text(
    app_name: str, user_name: str, builder_name: str, left: int, cap: int
) -> str:
    """The one message the server posts into a new build channel (D8).

    Transcript context for both residents' backfill, and nothing more: it is
    authored by the `system` bot, so it does NOT summon anybody (the hub
    attaches context to USER-authored messages only). The builder's first real
    reply is its own personalized open, which is what the spec asks for and
    what a canned greeting would have pre-empted.
    """
    return (
        f"App build session for «{app_name}» — {user_name} is building with "
        f"{builder_name}. {builder_name}: talk until you have enough to build, "
        f"then say so. {left} of {cap} builds left today after this one."
    )


# ---------------------------------------------------------------------------
# GET /apps, /apps/discover, /apps/builders, /apps/quota
# ---------------------------------------------------------------------------

@router.get("/apps")
async def list_apps(user: CurrentUser) -> dict[str, Any]:
    """The caller's APPS group: apps they own, plus apps they have added.

    This is the sidebar's source. It is NOT derived from channels (D9): an
    `app_build` channel is where a build was talked through, an app is a thing
    that exists afterwards, and the two lists stop matching the moment somebody
    adds a public app they never built.
    """
    rows = await db.fetch_all(
        """SELECT * FROM apps
            WHERE owner_user_id = ?
               OR EXISTS (SELECT 1 FROM user_apps ua
                           WHERE ua.app_id = apps.id AND ua.user_id = ?)
            ORDER BY created_at, id""",
        (user.id, user.id),
    )
    return {
        "apps": [a.model_dump() for a in await _apps_out(rows, user.id)],
        "quota": (await _quota_for(user.id)).model_dump(),
    }


@router.get("/apps/discover")
async def discover_apps(user: CurrentUser) -> dict[str, Any]:
    """Apps the caller may add: public ones, plus ones shared with them.

    Minus what is already on their menu, because a chooser that offers you
    something you already have is a chooser you learn to distrust. Public does
    not mean on-your-menu (Amendment A edit 1) — that distinction is the whole
    reason `app_shares` and `user_apps` are two tables.
    """
    rows = await db.fetch_all(
        """SELECT * FROM apps
            WHERE owner_user_id != ?
              AND (visibility = 'public'
                   OR EXISTS (SELECT 1 FROM app_shares s
                               WHERE s.app_id = apps.id AND s.user_id = ?))
              AND NOT EXISTS (SELECT 1 FROM user_apps ua
                               WHERE ua.app_id = apps.id AND ua.user_id = ?)
            ORDER BY created_at DESC, id""",
        (user.id, user.id, user.id),
    )
    return {"apps": [a.model_dump() for a in await _apps_out(rows, user.id)]}


@router.get("/apps/builders")
async def list_builders(user: CurrentUser) -> list[BuilderOut]:
    """The chooser's builder cards, in configured order.

    A configured bot_id with no row in `bots` is OMITTED rather than rendered
    as a card that cannot be picked — the settings list is a request, the bots
    table is the fact. An empty result is a legitimate answer (no build seat
    configured yet) and the client renders an empty state for it; it is
    deliberately not a boot failure.
    """
    entries = _builder_entries()
    if not entries:
        return []
    bot_ids = [e["bot_id"] for e in entries]
    placeholders = ",".join("?" * len(bot_ids))
    bots = {
        r["id"]: r
        for r in await db.fetch_all(
            f"SELECT id, name, avatar_path FROM bots WHERE id IN ({placeholders})",
            bot_ids,
        )
    }
    counts = await _build_counts(bot_ids)
    return [
        _builder_out(
            bots[e["bot_id"]],
            e["model_source"],
            e["model_key"],
            counts.get(e["bot_id"], (0, 0)),
        )
        for e in entries
        if e["bot_id"] in bots
    ]


@router.get("/apps/quota")
async def get_quota(user: CurrentUser) -> Quota:
    return await _quota_for(user.id)


# ---------------------------------------------------------------------------
# POST /apps/sessions — start a build
# ---------------------------------------------------------------------------

@router.post("/apps/sessions")
async def create_session(body: SessionCreate, user: CurrentUser) -> SessionOut:
    """Start a build session: app (if new) + channel + members + session row.

    ONE transaction for all four, because a session pointing at a channel that
    does not exist is unrecoverable from the client's side. The system opener
    is posted AFTER the commit — it publishes on the bus, and a subscriber must
    never see a message whose channel is still uncommitted.

    Order of refusals is deliberate: builder first (a typo in the request),
    then quota (the caller's own budget), then the app and its lock (somebody
    else's state). Each is a flat sentence, and none of them repeats anything
    the caller sent.
    """
    entry = _builder_entry(body.builder_bot_id)
    builder = await _builder_for(body.builder_bot_id) if entry is not None else None
    if entry is None or builder is None:
        raise HTTPException(
            status_code=400,
            detail="That builder is not available for app builds on this server",
        )

    quota = await _quota_for(user.id)
    if quota.left <= 0:
        raise HTTPException(
            status_code=429,
            detail=(
                f"You have used all {quota.cap} app builds for today; "
                "the meter resets at midnight UTC."
            ),
        )

    app: Optional[dict[str, Any]] = None
    if body.app_id is not None:
        app = await _require_app(body.app_id)
        _require_app_owner(app, user)
        if (await _open_sessions_by_app([app["id"]])).get(app["id"]) is not None:
            raise HTTPException(
                status_code=409,
                detail="This app already has a build session open; "
                       "close it or wait for it to lapse",
            )

    # Everything the opener needs, resolved BEFORE the transaction: the write
    # lock is process-global and spans every await inside the block.
    app_name = app["name"] if app is not None else DEFAULT_APP_NAME
    display_name = user.display_name or user.username
    left_after = max(0, quota.cap - (quota.used + 1))
    opener = _opener_text(
        app_name, display_name, builder.name, left_after, quota.cap
    )
    new_app_id = None if app is not None else await _mint_app_id()
    now = _now()
    locked_until = _locked_until(now)

    async with db.transaction() as conn:
        if new_app_id is not None:
            await conn.execute(
                """INSERT INTO apps (id, owner_user_id, name, builder_bot_id,
                                     created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (new_app_id, user.id, app_name, body.builder_bot_id, now, now),
            )
            app_id = new_app_id
        else:
            assert app is not None
            app_id = app["id"]

        # visibility 'private' is not decoration: every code path that keys on
        # channel visibility (the sidebar filter, slash's is_private_channel,
        # the membership verbs) then fails closed on this channel by the rule
        # it already has, rather than by a special case for a new type.
        cur = await conn.execute(
            """INSERT INTO channels (type, name, visibility, created_by, created_at)
               VALUES ('app_build', ?, 'private', ?, ?)""",
            (app_name, user.id, now),
        )
        channel_id = cur.lastrowid
        for member_type, member_id in (("user", user.id), ("bot", body.builder_bot_id)):
            await conn.execute(
                """INSERT INTO channel_members (channel_id, member_type, member_id)
                   VALUES (?, ?, ?)""",
                (channel_id, member_type, member_id),
            )
        cur = await conn.execute(
            """INSERT INTO app_sessions (app_id, user_id, builder_bot_id, channel_id,
                                         started_at, locked_until)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (app_id, user.id, body.builder_bot_id, channel_id, now, locked_until),
        )
        session_id = cur.lastrowid

    try:
        await deliver_message(channel_id, "bot", await _system_bot_id(), opener)
    except Exception:  # noqa: BLE001
        # The session is durable and usable; the opener is transcript garnish.
        # Failing the request here would hand the caller a 500 for a session
        # that exists and that they can no longer reach.
        logger.exception("app build opener failed for session %s", session_id)

    session = await db.fetch_one(
        "SELECT * FROM app_sessions WHERE id = ?", (session_id,)
    )
    assert session is not None
    return await _session_out(session, user)


@router.get("/apps/sessions/{session_id}")
async def get_session(session_id: int, user: CurrentUser) -> SessionOut:
    """The session, its app, its builder, its stages so far, and the meter.

    Reachable after the lock has lapsed and after an explicit end: reading a
    finished session is how the modal shows what happened. Only the WRITE
    verbs below refuse a session that is over.
    """
    return await _session_out(await _require_session(session_id, user), user)


@router.post("/apps/sessions/{session_id}/heartbeat")
async def heartbeat_session(session_id: int, user: CurrentUser) -> dict[str, Any]:
    """Push `locked_until` out by the TTL. The open modal calls this on a timer.

    This is the whole abandonment story: nobody has to close the modal
    correctly, and no sweeper has to run. A build that stops being watched
    stops being heartbeated, and its lock lapses on its own.
    """
    session = await _require_session(session_id, user)
    _require_session_open(session)
    locked_until = _locked_until(_now())
    await db.execute(
        "UPDATE app_sessions SET locked_until = ? WHERE id = ?",
        (locked_until, session_id),
    )
    return {"id": session_id, "locked_until": locked_until, "ended_at": None}


@router.post("/apps/sessions/{session_id}/end")
async def end_session(session_id: int, user: CurrentUser) -> dict[str, Any]:
    """Close the session and release the lock. Idempotent.

    Idempotent by keeping the FIRST ended_at: a second call is a duplicate
    click or a retry, not a second ending, and rewriting the timestamp would
    quietly move when the build finished.
    """
    session = await _require_session(session_id, user)
    if session["ended_at"] is None:
        ended_at = _now()
        await db.execute(
            "UPDATE app_sessions SET ended_at = ? WHERE id = ? AND ended_at IS NULL",
            (ended_at, session_id),
        )
    else:
        ended_at = session["ended_at"]
    return {"id": session_id, "ended_at": ended_at}


# ---------------------------------------------------------------------------
# POST /apps/sessions/{id}/stage — the publisher half of the stage stream
# ---------------------------------------------------------------------------

def _require_stage_publisher(actor: Actor) -> None:
    """Only the configured build-harness seats, or an admin.

    A stage event is an ATTESTATION about what a build actually did — the modal
    renders it as fact and the app's status hangs off `live`. So the publisher
    list is config a person edits (APPS_STAGE_PUBLISHER_BOT_NAMES), never
    something a caller asserts about itself. The session's own owner is
    deliberately NOT on the list: the user is who the stream is FOR.
    """
    if actor.type == "bot" and actor.bot is not None:
        if actor.bot.name in get_settings().APPS_STAGE_PUBLISHER_BOT_NAMES:
            return
    elif actor.user is not None and actor.user.is_admin:
        return
    raise HTTPException(
        status_code=403,
        detail="Only the build harness or an admin can publish a build stage",
    )


@router.post("/apps/sessions/{session_id}/stage")
async def publish_stage(
    session_id: int, actor: CurrentActor, body: StagePublish = Body(...)
) -> StageEventOut:
    """Persist a stage event, mark the session, publish it to the owner.

    Persisted BEFORE it is published, so a modal that reloads sees the same
    stages a live modal saw. `live` is also the app's done state, so it flips
    `apps.status` in the same transaction — the two facts are one fact.

    Fan-out is the OWNER's sockets only (app/ws.py). Not the builder bot: a
    resident reading the harness's report of its own build back as an inbound
    frame is a loop nobody asked for.
    """
    _require_stage_publisher(actor)
    session = await db.fetch_one("SELECT * FROM app_sessions WHERE id = ?", (session_id,))
    if session is None:
        raise HTTPException(status_code=404, detail="Build session not found")
    _require_session_open(session)

    detail_json = json.dumps(body.detail)
    created_at = _now()
    async with db.transaction() as conn:
        cur = await conn.execute(
            """INSERT INTO app_stage_events (session_id, stage, detail, created_at)
               VALUES (?, ?, ?, ?)""",
            (session_id, body.stage, detail_json, created_at),
        )
        event_id = cur.lastrowid
        await conn.execute(
            "UPDATE app_sessions SET stage = ? WHERE id = ?", (body.stage, session_id)
        )
        if body.stage == "live":
            await conn.execute(
                "UPDATE apps SET status = 'live', updated_at = ? WHERE id = ?",
                (created_at, session["app_id"]),
            )

    await events.publish(
        {
            "type": "app_stage",
            # Internal to the bus, never on the wire: who the frame is for.
            "owner_user_id": session["user_id"],
            "session_id": session_id,
            "app_id": session["app_id"],
            "stage": body.stage,
            "detail": body.detail,
            "created_at": created_at,
        }
    )
    return StageEventOut(
        id=event_id,
        session_id=session_id,
        stage=body.stage,
        detail=body.detail,
        created_at=created_at,
    )


# ---------------------------------------------------------------------------
# PATCH /apps/{app_id} — rename / describe
# ---------------------------------------------------------------------------

@router.patch("/apps/{app_id}")
async def update_app(app_id: str, body: AppPatch, user: CurrentUser) -> AppOut:
    """Rename or re-describe an app. Owner only.

    Bounds are D10 (60 / 300) and they are enforced by the request model, so an
    over-long name is refused before anything is written and the 422 body names
    the field and the limit without echoing what was sent.

    The `app_build` channel keeps the name it was born with. It is a transcript
    of one conversation, not a live mirror of the registry, and renaming rooms
    under people mid-session is how a channel list stops being navigable.
    """
    app = await _require_app(app_id)
    _require_app_owner(app, user)

    fields: dict[str, Any] = {}
    if body.name is not None:
        name = body.name.strip()
        if not name:
            raise HTTPException(status_code=400, detail="An app needs a name")
        fields["name"] = name
    if body.description is not None:
        fields["description"] = body.description.strip()
    if not fields:
        raise HTTPException(
            status_code=400, detail="Nothing to change: send a name or a description"
        )

    fields["updated_at"] = _now()
    assignments = ", ".join(f"{column} = ?" for column in fields)
    await db.execute(
        f"UPDATE apps SET {assignments} WHERE id = ?", (*fields.values(), app_id)
    )

    row = await _require_app(app_id)
    out = await _app_out_one(row, user.id)
    await events.publish(
        {
            "type": "app_update",
            # Internal to the bus; the frame carries the app, not the routing.
            "owner_user_id": row["owner_user_id"],
            "app": out.model_dump(),
        }
    )
    return out


# ---------------------------------------------------------------------------
# The menu (user_apps)
# ---------------------------------------------------------------------------

async def _visible_to(app: dict[str, Any], user: User) -> bool:
    """May this user have this app on their menu at all?

    Owner, explicitly shared with, or public. This is the wall the client's
    filtered discover list is only a convenience in front of — a caller that
    posts a private app id straight at the endpoint gets the refusal, not the
    app.
    """
    if app["owner_user_id"] == user.id or app["visibility"] == "public":
        return True
    row = await db.fetch_one(
        "SELECT 1 FROM app_shares WHERE app_id = ? AND user_id = ?",
        (app["id"], user.id),
    )
    return row is not None


@router.post("/apps/{app_id}/menu")
async def add_to_menu(app_id: str, user: CurrentUser) -> AppOut:
    """Put an app in the caller's APPS group. Idempotent."""
    app = await _require_app(app_id)
    if not await _visible_to(app, user):
        raise HTTPException(
            status_code=403, detail="This app has not been shared with you"
        )
    if app["owner_user_id"] != user.id:
        await db.execute(
            """INSERT OR IGNORE INTO user_apps (user_id, app_id, added_at)
               VALUES (?, ?, ?)""",
            (user.id, app_id, _now()),
        )
    return await _app_out_one(app, user.id)


@router.delete("/apps/{app_id}/menu")
async def remove_from_menu(app_id: str, user: CurrentUser) -> dict[str, bool]:
    """Take an app off the caller's APPS group. Idempotent.

    The owner cannot take their own app off their own menu (400): their menu
    row is implied by ownership, so the delete would report success and change
    nothing. Deleting the APP is a later stage's verb, and it is a different
    one.
    """
    app = await _require_app(app_id)
    if app["owner_user_id"] == user.id:
        raise HTTPException(
            status_code=400,
            detail="You own this app, so it stays in your apps list",
        )
    cur = await db.execute(
        "DELETE FROM user_apps WHERE user_id = ? AND app_id = ?", (user.id, app_id)
    )
    return {"ok": True, "removed": cur.rowcount > 0}
