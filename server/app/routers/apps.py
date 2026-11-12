"""APPS: the registry, the build sessions, and the stage stream.

The build chat is an ordinary app_build channel; this router must not copy its
message path. The stage endpoint is the only writer of app_stage_events, gated
to harness seats and admins, never the session owner. A halted turn re-posts its
last reached stage with detail.halted, because stage names are a CHECK
constraint: render labels from detail. Visibility is enforced by signed grants,
and every mint is preceded by _visible_to. Host operations run through one
fixed-argv sudo helper, never a shell. Deferred: the screenshot at live (D9).
"""

import json
import logging
import os
import re
import secrets
import subprocess
from base64 import b32encode
from datetime import datetime, time as dt_time, timedelta, timezone
from typing import Annotated, Any, Literal, Optional

from fastapi import APIRouter, Body, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field, model_validator
from starlette.concurrency import run_in_threadpool

from .. import apps_grant, db, events
from ..config import get_settings, read_model_pin
from ..models import AppOut, AppStage, OpenSessionRef, User
from . import channels
from .auth import Actor, get_actor, get_current_user
from .messages import deliver_message
from .slash import SYSTEM_BOT_NAME

logger = logging.getLogger(__name__)

router = APIRouter()

CurrentUser = Annotated[User, Depends(get_current_user)]
CurrentActor = Annotated[Actor, Depends(get_actor)]

# D10: new apps are born unnamed-but-nameable.
DEFAULT_APP_NAME = "Untitled app"
MAX_APP_NAME_CHARS = 60
MAX_APP_DESCRIPTION_CHARS = 300

# A stage event's `detail` is free-form (filenames, a note), so it is bounded the
# way messages bounds its metadata: an uncapped JSON field on a write path is a side
# channel with a database behind it.
MAX_STAGE_DETAIL_CHARS = 2000

# The two free-text fields inside a detail — the runner's one-line report and a
# halt's reason.
MAX_STAGE_LINE_CHARS = 300

# How much of a turn's file list the §H line names before it counts the rest.
STAGE_LINE_FILES = 8

# Why a turn stopped.
HaltReason = Literal["timeout", "error", "secret", "ceiling", "stopped"]

# One sentence per halt, keyed by the reason.
HALT_SENTENCES: dict[str, str] = {
    "ceiling": "build hit its ceiling.",
    "timeout": "the build timed out.",
    "error": "the build failed.",
    # Slice (iv): the user pressed Stop.
    "stopped": "stopped by the user.",
    "secret": (
        "the build tried to write a credential; this session is closed and an "
        "admin has been notified."
    ),
}

# Random, never sequential: a sequential id on the shared origin is a map of neighbours.
APP_ID_CHARS = 12
APP_ID_RE = re.compile(r"^[a-z2-7]{12}$")
_APP_ID_ATTEMPTS = 5

# Shorter (or absent) reads as not configured: a supported state, not a boot error.
MIN_GATE_SECRET_BYTES = 32

# What every endpoint that needs the gate says when it is not configured.
GATE_UNCONFIGURED = (
    "App serving is not configured on this house yet, so this app cannot be "
    "opened outside the build modal."
)

# The same answer, for the one misconfiguration that is WORSE than missing: the apps
# origin listed as a house origin.
GATE_ORIGIN_COLLIDES = (
    "App serving is not configured correctly on this house, so this app "
    "cannot be opened outside the build modal; an admin has the log."
)

# Seconds the host helper gets. Two minutes is a wall, not a budget.
HELPER_TIMEOUT_SEC = 120

# Exit codes of `disjorn-apps-launch` (D6). 0 done, 64 refused with a sentence on
# stderr, anything else failed.
HELPER_REFUSED = 64

# The name a remix takes.
REMIX_SUFFIX = " (remix)"

# Roots the gate knows.
AppRoot = Literal["live", "preview"]


def _now() -> str:
    """UTC now in the house's stored format. Timestamps compare as strings, so `locked_until >
    now` is a string comparison: do not add a second format.
    """
    return db.utc_now()


def _iso(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _utc_day_start() -> str:
    """Midnight UTC today — the lower bound of the quota window (D5)."""
    today = datetime.now(timezone.utc).date()
    return _iso(datetime.combine(today, dt_time.min, tzinfo=timezone.utc))


def _next_utc_midnight() -> str:
    """When the meter resets."""
    tomorrow = datetime.now(timezone.utc).date() + timedelta(days=1)
    return _iso(datetime.combine(tomorrow, dt_time.min, tzinfo=timezone.utc))


def _locked_until(now: str) -> str:
    """`now` + the configured TTL, as a comparable timestamp."""
    ttl = max(0, int(get_settings().APPS_SESSION_LOCK_TTL))
    parsed = datetime.strptime(now, "%Y-%m-%dT%H:%M:%S.%fZ").replace(
        tzinfo=timezone.utc
    )
    return _iso(parsed + timedelta(seconds=ttl))


class Quota(BaseModel):
    """The visible unit is builds per user per UTC day. A session counts once it has spent a
    turn, or while it is open with a live lock; a dialog closed with no turn does not count.
    Counted straight off app_sessions, with no counter table to drift.
    """

    cap: int
    used: int
    left: int
    resets_at: str


class BuilderOut(BaseModel):
    """One card in the chooser. `model` is read from the seat at request time; None renders as
    'model not declared by seat'. Never hardcode a model name.
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
    mode: Literal["app", "repo"] = "app"
    repo_slug: Optional[str] = None


class SessionCreate(BaseModel):
    builder_bot_id: int
    # Omitted -> a brand new app.
    app_id: Optional[str] = None


class AppPatch(BaseModel):
    name: Optional[str] = Field(default=None, max_length=MAX_APP_NAME_CHARS)
    description: Optional[str] = Field(
        default=None, max_length=MAX_APP_DESCRIPTION_CHARS
    )


class StageDetail(BaseModel):
    """What a stage event may say about a turn. Unknown keys are accepted. `halted` is a closed
    set because it selects a sentence.
    """

    model_config = ConfigDict(extra="allow", protected_namespaces=())

    turn: Optional[int] = Field(default=None, ge=1)
    # False on a ceiling refusal: the event is posted for the room but nothing ran,
    # so it must not advance the turn counter — the next real handoff reuses this
    # turn number.
    spawned: Optional[bool] = None
    files: Optional[list[str]] = None
    tokens: Optional[int] = Field(default=None, ge=0)
    model: Optional[str] = None
    no_changes: Optional[bool] = None
    summary: Optional[str] = Field(default=None, max_length=MAX_STAGE_LINE_CHARS)
    halted: Optional[HaltReason] = None
    reason: Optional[str] = Field(default=None, max_length=MAX_STAGE_LINE_CHARS)


class StagePublish(BaseModel):
    stage: AppStage
    detail: StageDetail = Field(default_factory=StageDetail)

    def detail_dict(self) -> dict[str, Any]:
        """The detail EXACTLY as it was sent — set keys and unknown keys, and
        nothing this model merely defaults to None."""
        return self.detail.model_dump(exclude_unset=True)

    @model_validator(mode="after")
    def _bound_detail(self) -> "StagePublish":
        """Keep `detail` from becoming an uncapped side channel (messages.py's
        _bound_metadata, same reasoning, same shape)."""
        try:
            encoded = json.dumps(self.detail_dict())
        except (TypeError, ValueError):
            raise ValueError("detail must be JSON-serializable") from None
        if len(encoded) > MAX_STAGE_DETAIL_CHARS:
            raise ValueError(
                f"detail exceeds {MAX_STAGE_DETAIL_CHARS} serialized characters"
            )
        return self


class HarnessView(BaseModel):
    """One build session as the broker needs it. Separate from SessionOut, the modal's payload,
    so the harness never holds the user's view. `lock_lapsed` is informational: a handoff still
    lands.
    """

    session_id: int
    app_id: str
    owner_user_id: int
    owner_username: Optional[str] = None
    builder_bot_id: int
    channel_id: int
    stage: Optional[AppStage] = None
    turns: int
    tokens_used: int
    open: bool
    lock_lapsed: bool
    ended_at: Optional[str] = None
    locked_until: str
    # Slice (iv): set when the owner asked for the running turn to stop.
    stop_requested_at: Optional[str] = None
    mode: Literal["app", "repo"] = "app"
    repo_slug: Optional[str] = None


def _check_app_id(app_id: str) -> str:
    """A path app id must look like one this server minted. 404, not 400, and the id is never
    echoed back.
    """
    if not APP_ID_RE.fullmatch(app_id):
        raise HTTPException(status_code=404, detail="App not found")
    return app_id


async def _require_app(app_id: str) -> dict[str, Any]:
    _check_app_id(app_id)
    row = await db.fetch_one(
        "SELECT * FROM apps WHERE id = ? AND deleted_at IS NULL", (app_id,)
    )
    if row is None:
        raise HTTPException(status_code=404, detail="App not found")
    return row


def _require_app_owner(app: dict[str, Any], user: User) -> None:
    if app["owner_user_id"] != user.id:
        raise HTTPException(
            status_code=403, detail="Only the app's owner can change it"
        )


async def _mint_app_id() -> str:
    """A fresh, unused app id. Retry on collision, so a collision is never a 500."""
    for _ in range(_APP_ID_ATTEMPTS):
        candidate = b32encode(secrets.token_bytes(8)).decode("ascii").rstrip("=")
        candidate = candidate.lower()[:APP_ID_CHARS]
        if await db.fetch_one("SELECT 1 FROM apps WHERE id = ?", (candidate,)) is None:
            return candidate
    raise HTTPException(
        status_code=500, detail="Could not allocate an app id; please try again"
    )


async def _require_builder(bot_id: int) -> BuilderOut:
    """The builder card for a bot that may actually be built with, or 400."""
    entry = _builder_entry(bot_id)
    builder = await _builder_for(bot_id) if entry is not None else None
    if entry is None or builder is None:
        raise HTTPException(
            status_code=400,
            detail="That builder is not available for app builds on this server",
        )
    return builder


async def _require_quota(user_id: int) -> Quota:
    """The caller's meter, refusing at zero. Every verb that opens a session spends it."""
    quota = await _quota_for(user_id)
    if quota.left <= 0:
        raise HTTPException(
            status_code=429,
            detail=(
                f"You have used all {quota.cap} app builds for today; "
                "the meter resets at midnight UTC."
            ),
        )
    return quota


async def _quota_for(user_id: int) -> Quota:
    """Builds used today. A session counts once it has spent a turn, or while it is still open
    with a live lock: a dialog opened and closed with no turn must not cost a build. `turns`
    is the signal, not the row; a deleted app's builds of today (`deleted_builds`) still count."""
    cap = int(get_settings().APPS_DAILY_SESSION_CAP)
    day = _utc_day_start()
    row = await db.fetch_one(
        """SELECT (SELECT COUNT(*) FROM app_sessions s
                  JOIN apps a ON a.id = s.app_id AND a.deleted_at IS NULL
                 WHERE s.user_id = ? AND s.started_at >= ? AND s.mode = 'app'
                   AND (s.turns > 0
                        OR (s.ended_at IS NULL AND s.locked_until > ?)))
                + (SELECT COALESCE(SUM(deleted_builds), 0) FROM apps
                    WHERE owner_user_id = ? AND deleted_at >= ?) AS n""",
        (user_id, day, _now(), user_id, day),
    )
    used = int(row["n"]) if row is not None else 0
    return Quota(
        cap=cap, used=used, left=max(0, cap - used), resets_at=_next_utc_midnight()
    )


async def _open_sessions_by_app(app_ids: list[str]) -> dict[str, dict[str, Any]]:
    """The live session on each app, if any. A lapsed lock reads as no session; there is no
    sweeper.
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
    """One app, answered for one caller. on_menu is true for the owner without a user_apps row,
    so removing one's own app is a refusal, not a no-op.
    """
    session = open_sessions.get(row["id"])
    return AppOut(
        id=row["id"],
        name=row["name"],
        description=row["description"],
        visibility=row["visibility"],
        status=row["status"] if row["deleted_at"] is None else "deleting",
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
    """Batched: one menu query and one open-session query for the whole list, rather
    than two per row."""
    menu_ids = await _menu_app_ids(user_id)
    open_sessions = await _open_sessions_by_app([r["id"] for r in rows])
    return [_app_out(r, user_id, menu_ids, open_sessions) for r in rows]


async def _app_out_one(row: dict[str, Any], user_id: int) -> AppOut:
    return (await _apps_out([row], user_id))[0]


def _builder_entries() -> list[dict[str, Any]]:
    """APPS_BUILDERS, defensively: config is hand-edited, and a malformed entry must
    cost that entry rather than the whole tab."""
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
               AND deleted_at IS NULL
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
    """The card for one builder, whether or not it is still in APPS_BUILDERS. A session must
    still render its builder after the seat leaves the chooser.
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
    """410 on a session that is over — explicitly ended OR lock lapsed (D6)."""
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
    """A stored detail blob, defensively."""
    try:
        value = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


async def _session_out(session: dict[str, Any], user: User) -> SessionOut:
    app = await _require_app(session["app_id"])
    builder = await _builder_for(session["builder_bot_id"])
    if builder is None:
        # The seat's bot row is gone.
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
        mode=session["mode"],
        repo_slug=session["repo_slug"],
    )


async def _system_bot_id() -> int:
    row = await db.fetch_one("SELECT id FROM bots WHERE name = ?", (SYSTEM_BOT_NAME,))
    if row is None:  # migration 006 seeds it
        raise RuntimeError("system bot not found (migration 006 not applied?)")
    return row["id"]


def _opener_text(
    app_name: str, user_name: str, builder_name: str, left: int, cap: int
) -> str:
    """The one message posted into a new build channel (D8). Authored by `system`, so it summons
    nobody; the builder's first real reply is its own opener.
    """
    return (
        f"App build session for «{app_name}» — {user_name} is building with "
        f"{builder_name}. {builder_name}: talk until you have enough to build, "
        f"then say so. {left} of {cap} builds left today after this one."
    )


def _one_line(text: Any) -> str:
    """A publisher's free text, made safe for a message. Control characters, newlines included,
    collapse to spaces, so one line cannot become several. The result is re-capped here, not
    trusted from the door.
    """
    if not isinstance(text, str):
        return ""
    flattened = "".join(" " if ch < " " or ch == "\x7f" else ch for ch in text)
    line = " ".join(flattened.split())
    if len(line) > MAX_STAGE_LINE_CHARS:
        # Capped WITH an ellipsis: the first real build's summary was cut mid-word
        # inside its quotes and read as a message that had hung.
        line = line[: MAX_STAGE_LINE_CHARS - 1] + "\u2026"
    return line


def _tokens_word(tokens: int) -> str:
    """`212k` once a turn is into the thousands, the integer below that.

    Rounded on purpose: this number is a bill, not a measurement, and a line
    that reads "211,847 tokens" invites arithmetic the ceiling is not asking
    anybody to do.
    """
    return f"{tokens / 1000:.0f}k" if tokens >= 1000 else str(tokens)


def _files_word(files: list[str]) -> str:
    """The first few names, then a count of the rest."""
    named = [_one_line(f) for f in files[:STAGE_LINE_FILES]]
    rest = len(files) - len(named)
    if rest > 0:
        named.append(f"+{rest} more")
    return ", ".join(named)


def _turn_line(detail: dict[str, Any]) -> str:
    """The §H line for one turn. Authored by `system`, so it summons nobody. A halt wins over
    files_written, whatever stage was re-posted. The builder's summary is quoted after the
    house's sentence, so no one reads it as an attestation.
    """
    turn = detail.get("turn")
    number = turn if isinstance(turn, int) and not isinstance(turn, bool) else 0
    summary = _one_line(detail.get("summary"))
    tail = f' "{summary}"' if summary else ""

    halted = detail.get("halted")
    if isinstance(halted, str) and halted in HALT_SENTENCES:
        reason = _one_line(detail.get("reason"))
        raw = detail.get("files")
        halt_files = [f for f in raw if isinstance(f, str)] if isinstance(raw, list) else []
        wrote = f" Wrote {_files_word(halt_files)}." if halt_files else ""
        # A halt that carried a report keeps it: the turn where the runner wrote
        # something and then failed is the turn the report is most worth reading.
        return (
            f"Turn {number} halted — {HALT_SENTENCES[halted]}"
            + (f" {reason}" if reason else "")
            + wrote
            + tail
        )

    if detail.get("no_changes") is True:
        return f"Turn {number}: no changes.{tail}"

    raw = detail.get("files")
    files = [f for f in raw if isinstance(f, str)] if isinstance(raw, list) else []
    tokens = detail.get("tokens")
    counted = tokens if isinstance(tokens, int) and not isinstance(tokens, bool) else 0
    model = _one_line(detail.get("model")) or "not reported"
    named = f" ({_files_word(files)})" if files else ""
    noun = "file" if len(files) == 1 else "files"
    return (
        f"Turn {number} done — {len(files)} {noun} written{named}, "
        f"{_tokens_word(counted)} tokens, model {model}.{tail}"
    )


async def _close_session(conn: Any, session_id: int, moment: str) -> None:
    """End a session and release its lock, keeping the FIRST ending. The ended_at guard makes it
    idempotent, and locked_until is set to now in the same statement, because the lock is only
    ever read as 'still in the future'.
    """
    await conn.execute(
        """UPDATE app_sessions SET ended_at = ?, locked_until = ?
            WHERE id = ? AND ended_at IS NULL""",
        (moment, moment, session_id),
    )


async def _running_turn(conn: Any, session_id: int) -> Optional[int]:
    """The turn number of the turn running NOW, or None. Derived from the stage stream, never a
    column. A ceiling refusal (spawned false) never started a turn and does not count.
    """
    scoped = await (await conn.execute(
        """SELECT id, detail FROM app_stage_events
            WHERE session_id = ? AND stage = 'scoped'
              AND COALESCE(json_extract(detail, '$.spawned'), 1) != 0
            ORDER BY id DESC LIMIT 1""",
        (session_id,),
    )).fetchone()
    if scoped is None:
        return None
    terminal = await (await conn.execute(
        """SELECT id FROM app_stage_events
            WHERE session_id = ? AND (stage = 'files_written'
                  OR json_extract(detail, '$.halted') IS NOT NULL)
            ORDER BY id DESC LIMIT 1""",
        (session_id,),
    )).fetchone()
    if terminal is not None and terminal["id"] > scoped["id"]:
        return None
    try:
        turn = json.loads(scoped["detail"] or "{}").get("turn")
    except (TypeError, ValueError):
        return None
    return turn if isinstance(turn, int) and not isinstance(turn, bool) else None


async def _request_stop(conn: Any, session_id: int, moment: str) -> None:
    """Record the stop, keeping the FIRST request (idempotent, like ending)."""
    await conn.execute(
        """UPDATE app_sessions SET stop_requested_at = ?
            WHERE id = ? AND stop_requested_at IS NULL""",
        (moment, session_id),
    )


@router.get("/apps")
async def list_apps(user: CurrentUser) -> dict[str, Any]:
    """The caller's APPS group: apps they own or have added."""
    rows = await db.fetch_all(
        """SELECT * FROM apps
            WHERE (deleted_at IS NULL
                   AND (owner_user_id = ?
                        OR EXISTS (SELECT 1 FROM user_apps ua
                                    WHERE ua.app_id = apps.id AND ua.user_id = ?)))
               OR (owner_user_id = ? AND deleted_at IS NOT NULL
                   AND purged_at IS NULL)
            ORDER BY created_at, id""",
        (user.id, user.id, user.id),
    )
    return {
        "apps": [a.model_dump() for a in await _apps_out(rows, user.id)],
        "quota": (await _quota_for(user.id)).model_dump(),
    }


@router.get("/apps/discover")
async def discover_apps(user: CurrentUser) -> dict[str, Any]:
    """Apps the caller may add to their APPS group, minus those already on it."""
    rows = await db.fetch_all(
        """SELECT * FROM apps
            WHERE owner_user_id != ? AND deleted_at IS NULL
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
    """The build seats the caller may choose from, in configured order."""
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


@router.post("/apps/sessions")
async def create_session(body: SessionCreate, user: CurrentUser) -> SessionOut:
    """Start a build session on a new app or one the caller owns."""
    builder = await _require_builder(body.builder_bot_id)
    quota = await _require_quota(user.id)

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

    # Everything the opener needs, resolved BEFORE the transaction: the write lock
    # is process-global and spans every await inside the block.
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
            kept = await (await conn.execute(
                "SELECT 1 FROM apps WHERE id = ? AND deleted_at IS NULL",
                (app_id,),
            )).fetchone()
            if kept is None:
                raise HTTPException(status_code=404, detail="App not found")

        # 'private' is load-bearing: every visibility check then fails closed on this channel.
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
        logger.exception("app build opener failed for session %s", session_id)

    session = await db.fetch_one(
        "SELECT * FROM app_sessions WHERE id = ?", (session_id,)
    )
    assert session is not None
    return await _session_out(session, user)


PLATFORM_APP_ID = "disjornrepo2"
PLATFORM_APP_NAME = "Disjorn platform"
PLATFORM_BUILD_BOT_MISSING = (
    "No build bot has an account on this house, so no build can start."
)


class RepoSessionError(RuntimeError):
    pass


async def _platform_build_bot_id() -> int:
    name = get_settings().PLATFORM_BUILD_BOT
    row = await db.fetch_one("SELECT id FROM bots WHERE name = ?", (name,))
    if row is None:
        logger.warning("no build bot named %s; repo session refused", name)
        raise RepoSessionError(PLATFORM_BUILD_BOT_MISSING)
    return row["id"]


async def _ensure_platform_app(owner_user_id: int, builder_bot_id: int) -> str:
    if await db.fetch_one("SELECT 1 FROM apps WHERE id = ?", (PLATFORM_APP_ID,)):
        return PLATFORM_APP_ID
    now = _now()
    await db.execute(
        """INSERT OR IGNORE INTO apps (id, owner_user_id, name, builder_bot_id,
                                       created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (PLATFORM_APP_ID, owner_user_id, PLATFORM_APP_NAME, builder_bot_id, now, now),
    )
    return PLATFORM_APP_ID


async def create_repo_session(user_id: int, channel_id: int) -> dict[str, Any]:
    builder_bot_id = await _platform_build_bot_id()
    app_id = await _ensure_platform_app(user_id, builder_bot_id)
    now = _now()
    cur = await db.execute(
        """INSERT INTO app_sessions (app_id, user_id, builder_bot_id, channel_id,
                                     started_at, locked_until, mode)
           VALUES (?, ?, ?, ?, ?, ?, 'repo')""",
        (app_id, user_id, builder_bot_id, channel_id, now, _locked_until(now)),
    )
    session = await db.fetch_one(
        "SELECT * FROM app_sessions WHERE id = ?", (cur.lastrowid,)
    )
    assert session is not None
    return session


async def mark_repo_queued(session: dict[str, Any], slug: str) -> None:
    detail = {"turn": 1, "summary": f"queued: loop/{slug}"}
    created_at = _now()
    async with db.transaction() as conn:
        await conn.execute(
            "UPDATE app_sessions SET repo_slug = ? WHERE id = ?",
            (slug, session["id"]),
        )
        await _write_stage_event(
            conn, session, "scoped", json.dumps(detail), created_at
        )
    await _publish_stage_frame(session, "scoped", detail, created_at)


async def end_repo_session(session_id: int) -> None:
    async with db.transaction() as conn:
        await _close_session(conn, session_id, _now())


@router.get("/apps/sessions/{session_id}")
async def get_session(session_id: int, user: CurrentUser) -> SessionOut:
    """The session, its app, its builder, its stages so far, and the meter."""
    return await _session_out(await _require_session(session_id, user), user)


@router.post("/apps/sessions/{session_id}/heartbeat")
async def heartbeat_session(session_id: int, user: CurrentUser) -> dict[str, Any]:
    """Push a build session's lock out by the TTL; the open modal calls this on a timer."""
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
    """End a build session and release its lock."""
    session = await _require_session(session_id, user)
    # Ending during a turn stops the turn first, in one transaction before ended_at.
    if session["ended_at"] is None:
        ended_at = _now()
        async with db.transaction() as conn:
            if await _running_turn(conn, session_id) is not None:
                await _request_stop(conn, session_id, ended_at)
            await _close_session(conn, session_id, ended_at)
    else:
        ended_at = session["ended_at"]
    return {"id": session_id, "ended_at": ended_at}


@router.post("/apps/sessions/{session_id}/stop")
async def stop_turn(session_id: int, user: CurrentUser) -> dict[str, Any]:
    """Ask the running build turn to stop; the broker's reaper sends the stop."""
    session = await _require_session(session_id, user)
    if session["ended_at"] is not None:
        raise HTTPException(status_code=410, detail="This build session has ended")
    async with db.transaction() as conn:
        if await _running_turn(conn, session_id) is None:
            raise HTTPException(status_code=409, detail="No turn is running")
        if session["stop_requested_at"] is None:
            await _request_stop(conn, session_id, _now())
    row = await db.fetch_one(
        "SELECT stop_requested_at FROM app_sessions WHERE id = ?", (session_id,)
    )
    return {"id": session_id, "stop_requested_at": row["stop_requested_at"]}


def _require_stage_publisher(actor: Actor) -> bool:
    """Only the configured build-harness seats, or an admin. The session owner is deliberately
    not on the list: the user is who the stream is for.
    """
    if actor.type == "bot" and actor.bot is not None:
        if actor.bot.name in get_settings().APPS_STAGE_PUBLISHER_BOT_NAMES:
            return True
    elif actor.user is not None and actor.user.is_admin:
        return False
    raise HTTPException(
        status_code=403,
        detail="Only the build harness or an admin can publish a build stage",
    )


async def _write_stage_event(
    conn: Any,
    session: dict[str, Any],
    stage: AppStage,
    detail_json: str,
    created_at: str,
    *,
    move_pointer: bool = True,
) -> int:
    """The stage stream's one writer, inside the caller's transaction. `live` also flips
    apps.status in the same transaction. A ceiling refusal does not move the stage pointer, or a
    session at `deployed` would rewind to `scoped`.
    """
    cur = await conn.execute(
        """INSERT INTO app_stage_events (session_id, stage, detail, created_at)
           VALUES (?, ?, ?, ?)""",
        (session["id"], stage, detail_json, created_at),
    )
    if move_pointer:
        await conn.execute(
            "UPDATE app_sessions SET stage = ? WHERE id = ?",
            (stage, session["id"]),
        )
    if stage == "live":
        await conn.execute(
            "UPDATE apps SET status = 'live', updated_at = ? WHERE id = ?",
            (created_at, session["app_id"]),
        )
    return cur.lastrowid


async def _publish_stage_frame(
    session: dict[str, Any],
    stage: AppStage,
    detail: dict[str, Any],
    created_at: str,
) -> None:
    """The `app_stage` frame, published AFTER commit, to the owner's sockets."""
    await events.publish(
        {
            "type": "app_stage",
            # Internal to the bus, never on the wire: who the frame is for.
            "owner_user_id": session["user_id"],
            "session_id": session["id"],
            "app_id": session["app_id"],
            "stage": stage,
            "detail": detail,
            "created_at": created_at,
        }
    )


async def _publisher_session(session_id: int) -> dict[str, Any]:
    """The session row a publisher named, or 404."""
    session = await db.fetch_one(
        "SELECT * FROM app_sessions WHERE id = ?", (session_id,)
    )
    if session is None:
        raise HTTPException(status_code=404, detail="Build session not found")
    return session


@router.get("/apps/sessions/{session_id}/harness-view")
async def harness_view(session_id: int, actor: CurrentActor) -> HarnessView:
    """What the broker reads before handing a prompt to the build seat."""
    _require_stage_publisher(actor)
    session = await _publisher_session(session_id)
    owner = await db.fetch_one(
        "SELECT username FROM users WHERE id = ?", (session["user_id"],))
    return HarnessView(
        session_id=session["id"],
        app_id=session["app_id"],
        owner_user_id=session["user_id"],
        owner_username=(owner or {}).get("username"),
        builder_bot_id=session["builder_bot_id"],
        channel_id=session["channel_id"],
        stage=session["stage"],
        turns=session["turns"],
        tokens_used=session["tokens_used"],
        open=session["ended_at"] is None,
        lock_lapsed=session["locked_until"] <= _now(),
        ended_at=session["ended_at"],
        locked_until=session["locked_until"],
        stop_requested_at=session["stop_requested_at"],
        mode=session["mode"],
        repo_slug=session["repo_slug"],
    )


@router.post("/apps/sessions/{session_id}/stage")
async def publish_stage(
    session_id: int, actor: CurrentActor, body: StagePublish = Body(...)
) -> StageEventOut:
    """Record a build stage event from the harness or an admin."""
    from_harness = _require_stage_publisher(actor)
    session = await _publisher_session(session_id)
    if from_harness:
        if session["ended_at"] is not None:
            # Slice (iv): `end` while a turn ran stopped the turn first, and the
            # record of THAT turn must still land — it is the room's only account of
            # what the stop kept.
            running = (await _running_turn(db, session_id)
                       if session["stop_requested_at"] is not None else None)
            if running is None or body.detail.turn != running:
                raise HTTPException(status_code=410,
                                    detail="This build session has ended")
    else:
        _require_session_open(session)

    detail = body.detail_dict()
    detail_json = json.dumps(detail)
    created_at = _now()
    halted = body.detail.halted
    terminal = body.stage == "files_written" or halted is not None
    async with db.transaction() as conn:
        event_id = await _write_stage_event(
            conn, session, body.stage, detail_json, created_at,
            move_pointer=body.detail.spawned is not False,
        )
        # A spawned:false event (a ceiling refusal) is not a turn and moves nothing: no
        # counter, no stage pointer. The turn counter is a MAX, not an increment, because
        # one turn posts several events.
        if body.detail.turn is not None and body.detail.spawned is not False:
            await conn.execute(
                "UPDATE app_sessions SET turns = MAX(turns, ?) WHERE id = ?",
                (body.detail.turn, session_id),
            )
        # Meter on the presence of `tokens`, never on the stage name, and count a turn's
        # tokens once: the meter is a running +=, so a retried post must not double-charge.
        if body.detail.tokens is not None and body.detail.spawned is not False:
            prior = await conn.execute(
                """SELECT 1 FROM app_stage_events
                    WHERE session_id = ? AND id != ?
                      AND json_extract(detail, '$.turn') = ?
                      AND json_extract(detail, '$.tokens') IS NOT NULL
                    LIMIT 1""",
                (session_id, event_id, body.detail.turn),
            )
            already = await prior.fetchone()
            if already is None:
                await conn.execute(
                    "UPDATE app_sessions SET tokens_used = tokens_used + ? WHERE id = ?",
                    (body.detail.tokens, session_id),
                )
        if halted == "secret":
            await _close_session(conn, session_id, created_at)
        # A terminal event consumes the stop request, whatever the reason the turn
        # ended for: the next turn of this session starts unasked.
        if terminal and body.detail.spawned is not False:
            await conn.execute(
                "UPDATE app_sessions SET stop_requested_at = NULL WHERE id = ?",
                (session_id,),
            )

    await _publish_stage_frame(session, body.stage, detail, created_at)

    if (body.stage == "files_written" or halted is not None) and session.get("mode", "app") == "app":
        try:
            await deliver_message(
                session["channel_id"], "bot", await _system_bot_id(),
                _turn_line(detail),
            )
        except Exception:  # noqa: BLE001
            # The event is durable and the bar has already moved; failing the
            # request here would tell the broker its turn did not land when it did,
            # and the broker would post the whole thing again.
            logger.exception("app build turn line failed for session %s", session_id)

    return StageEventOut(
        id=event_id,
        session_id=session_id,
        stage=body.stage,
        detail=detail,
        created_at=created_at,
    )


@router.patch("/apps/{app_id}")
async def update_app(app_id: str, body: AppPatch, user: CurrentUser) -> AppOut:
    """Rename or re-describe an app; owner only."""
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


async def _visible_to(app: dict[str, Any], user: User) -> bool:
    """May this user have this app on their menu at all?"""
    if app["owner_user_id"] == user.id or app["visibility"] == "public":
        return True
    row = await db.fetch_one(
        "SELECT 1 FROM app_shares WHERE app_id = ? AND user_id = ?",
        (app["id"], user.id),
    )
    return row is not None


@router.post("/apps/{app_id}/menu")
async def add_to_menu(app_id: str, user: CurrentUser) -> AppOut:
    """Put an app in the caller's APPS group."""
    app = await _require_app(app_id)
    if not await _visible_to(app, user):
        raise HTTPException(
            status_code=403, detail="This app has not been shared with you"
        )
    # The owner is on their own menu by construction, so no user_apps row is written.
    if app["owner_user_id"] != user.id:
        await db.execute(
            """INSERT OR IGNORE INTO user_apps (user_id, app_id, added_at)
               VALUES (?, ?, ?)""",
            (user.id, app_id, _now()),
        )
    return await _app_out_one(app, user.id)


@router.delete("/apps/{app_id}/menu")
async def remove_from_menu(app_id: str, user: CurrentUser) -> dict[str, bool]:
    """Take an app off the caller's APPS group; the owner cannot remove their own (400)."""
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


def _gate_config() -> tuple[bytes, str]:
    """The secret and the origin, or 503 with a sentence. Not a boot assertion: a house without
    a gate still builds apps.
    """
    settings = get_settings()
    secret = settings.APPS_GATE_SECRET.encode("utf-8")
    origin = settings.APPS_ORIGIN_BASE.rstrip("/")
    if len(secret) < MIN_GATE_SECRET_BYTES or not origin:
        raise HTTPException(status_code=503, detail=GATE_UNCONFIGURED)
    # …and it must not be one of US: an apps origin in HOUSE_ORIGINS would let app JS ride
    # the house cookies through the origin wall, with nothing saying so. Refuse to mint.
    if origin in {o.rstrip("/") for o in settings.HOUSE_ORIGINS}:
        logger.error(
            "APPS_ORIGIN_BASE %s is also in HOUSE_ORIGINS: the origin wall "
            "would treat app-served JS as the house. Refusing to mint grants.",
            origin,
        )
        raise HTTPException(status_code=503, detail=GATE_ORIGIN_COLLIDES)
    return secret, origin


def _gate_configured() -> bool:
    """The same question asked where a REFUSAL is not the answer — the card's
    `can_remix` and the config endpoint both need the fact, not the 503."""
    try:
        _gate_config()
    except HTTPException:
        return False
    return True


def _app_url(origin: str, app_id: str, root: str = "live") -> str:
    """`<origin>/<app>/`, plus `preview/` for the owner's root. The trailing slash is load-
    bearing for relative asset paths.
    """
    return f"{origin}/{app_id}/" + ("preview/" if root == "preview" else "")


def _house_absolute(path: Optional[str]) -> Optional[str]:
    """A house-relative URL made absolute for a document on the apps origin. The per-app CSP is
    'self', so an app cannot load it anyway; absolute is still the honest value.
    """
    if not path:
        return None
    origins = get_settings().HOUSE_ORIGINS
    return f"{origins[0].rstrip('/')}{path}" if origins else path


async def _context_blob(
    secret: bytes, app: dict[str, Any], user: User
) -> dict[str, Any]:
    """What the app is told about the viewer. The viewer id is the opaque per-app id, never the
    account id.
    """
    rows = await db.fetch_all(
        """SELECT u.display_name FROM app_shares s
             JOIN users u ON u.id = s.user_id
            WHERE s.app_id = ? ORDER BY u.id""",
        (app["id"],),
    )
    return {
        "version": 1,
        "user": {
            "id": apps_grant.opaque_user_id(secret, user.id, app["id"]),
            "name": user.display_name or user.username,
            "avatar_url": _house_absolute(user.avatar_url),
        },
        "shared_with": [r["display_name"] for r in rows],
        "theme": {},
    }


async def _require_entitled(app: dict[str, Any], user: User) -> None:
    """Owner, shared-with, or public: the same wall as _visible_to. Every grant is minted behind
    it.
    """
    if not await _visible_to(app, user):
        raise HTTPException(
            status_code=403, detail="This app has not been shared with you"
        )


def _run_launch_helper(argv: list[str]) -> subprocess.CompletedProcess:
    """Spawn disjorn-apps-launch. The only place this process spawns anything: an argv list, no
    shell.
    """
    return subprocess.run(  # noqa: S603 — fixed argv, no shell, checked ids
        argv,
        capture_output=True,
        text=True,
        timeout=HELPER_TIMEOUT_SEC,
        check=False,
    )


async def _launch(mode: str, *args: str) -> None:
    """Run one helper mode and turn its exit code into this house's answer. It runs off the
    event loop. 0 is done; 64 is refused, a 409 carrying the helper's sentence; anything else is
    a failed 502.
    """
    argv = [str(part) for part in get_settings().APPS_LAUNCH_HELPER]
    argv += [mode, *args]
    try:
        result = await run_in_threadpool(_run_launch_helper, argv)
    except (OSError, subprocess.SubprocessError):
        logger.exception("apps launch helper %s could not run", mode)
        raise HTTPException(
            status_code=502,
            detail="The house could not reach its app publisher; an admin has the log.",
        ) from None
    if result.returncode == 0:
        return
    sentence = _one_line(result.stderr)
    if result.returncode == HELPER_REFUSED:
        raise HTTPException(
            status_code=409,
            detail=sentence or "The house refused that app operation.",
        )
    logger.error(
        "apps launch helper %s exited %s: %s", mode, result.returncode, sentence
    )
    raise HTTPException(
        status_code=502,
        detail="The house could not finish that app operation; an admin has the log.",
    )


@router.get("/apps/config")
async def apps_config(user: CurrentUser) -> dict[str, Any]:
    """The apps origin, so the client can tell an apps link from any other link."""
    return {
        "origin_base": get_settings().APPS_ORIGIN_BASE.rstrip("/"),
        "configured": _gate_configured(),
    }


class OpenRequest(BaseModel):
    root: AppRoot = "live"


@router.post("/apps/{app_id}/open")
async def open_app(
    app_id: str, user: CurrentUser, body: Optional[OpenRequest] = None
) -> dict[str, str]:
    """Mint a signed grant URL that opens this app for the calling viewer."""
    secret, origin = _gate_config()
    root = (body or OpenRequest()).root
    app = await _require_app(app_id)
    await _require_entitled(app, user)

    if root == "preview":
        if app["owner_user_id"] != user.id:
            raise HTTPException(
                status_code=403,
                detail="Only the app's owner can open its preview",
            )
    elif app["status"] != "live":
        # Not 403: the viewer is entitled, there is simply nothing published yet.
        raise HTTPException(status_code=409, detail="This app is not live yet")

    owner = app["owner_user_id"] == user.id
    token = apps_grant.mint(
        secret,
        app_id=app["id"],
        roots=["live", "preview"] if owner else ["live"],
        ctx=await _context_blob(secret, app, user),
    )
    return {"url": f"{_app_url(origin, app['id'], root)}?t={token}"}


async def _session_files(session_id: int) -> list[str]:
    """Every file this session's turns reported writing, in order, deduped."""
    rows = await db.fetch_all(
        "SELECT detail FROM app_stage_events WHERE session_id = ? ORDER BY id",
        (session_id,),
    )
    seen: list[str] = []
    for row in rows:
        files = _loads_detail(row["detail"]).get("files")
        if not isinstance(files, list):
            continue
        for name in files:
            if isinstance(name, str) and name not in seen:
                seen.append(name)
    return seen


def _live_line(app_name: str, url: str, turns: int, files: list[str]) -> str:
    """The B9 summary, written into the build room by `system`. It summons nobody, and the
    builder reads this room, so it learns the app is live.
    """
    noun = "turn" if turns == 1 else "turns"
    tail = f", files: {_files_word(files)}" if files else ""
    return f"**{_one_line(app_name)}** is live at {url} — {turns} {noun}{tail}"


@router.post("/apps/sessions/{session_id}/live")
async def publish_live(session_id: int, user: CurrentUser) -> SessionOut:
    """Publish this build's preview tree to the app's public root."""
    secret, origin = _gate_config()
    session = await _require_session(session_id, user)
    if session["ended_at"] is not None:
        raise HTTPException(status_code=410, detail="This build session has ended")
    if await _running_turn(db, session_id) is not None:
        raise HTTPException(
            status_code=409,
            detail="The builder is still working; wait for this turn to finish",
        )
    deployed = await db.fetch_one(
        """SELECT 1 FROM app_stage_events
            WHERE session_id = ? AND stage = 'deployed' LIMIT 1""",
        (session_id,),
    )
    if deployed is None:
        raise HTTPException(
            status_code=409,
            detail="Nothing has been built in this session yet",
        )
    app = await _require_app(session["app_id"])

    # One publish at a time: a publish rotates the trees, so two racing would leave live.prev
    # a copy of live. The conditional write decides which press wins; the loser gets 409.
    claim = await db.execute(
        """UPDATE app_sessions SET live_publishing_at = ?
            WHERE id = ? AND live_publishing_at IS NULL AND ended_at IS NULL""",
        (_now(), session_id),
    )
    if claim.rowcount == 0:
        if (await _require_session(session_id, user))["ended_at"] is not None:
            raise HTTPException(
                status_code=410, detail="This build session has ended"
            )
        raise HTTPException(
            status_code=409,
            detail="This build is already being published; give it a moment",
        )
    try:
        await _launch("publish", app["id"])

        url = _app_url(origin, app["id"])
        detail = {"url": url}
        created_at = _now()
        async with db.transaction() as conn:
            await _write_stage_event(
                conn, session, "live", json.dumps(detail), created_at
            )
        await _publish_stage_frame(session, "live", detail, created_at)

        try:
            await deliver_message(
                session["channel_id"], "bot", await _system_bot_id(),
                _live_line(
                    app["name"], url, session["turns"], await _session_files(session_id)
                ),
            )
        except Exception:  # noqa: BLE001
            # The app IS live and the bar has moved; a failed transcript line is not
            # a reason to tell the owner their publish failed (publish_stage's §H
            # line is guarded the same way, for the same reason).
            logger.exception("app live line failed for session %s", session_id)
    finally:
        await db.execute(
            "UPDATE app_sessions SET live_publishing_at = NULL WHERE id = ?",
            (session_id,),
        )

    fresh = await db.fetch_one("SELECT * FROM app_sessions WHERE id = ?", (session_id,))
    assert fresh is not None
    return await _session_out(fresh, user)


async def _build_room(app_id: str) -> Optional[int]:
    """The channel this app is built in — its open session's, or its last one's."""
    row = await db.fetch_one(
        """SELECT channel_id FROM app_sessions WHERE app_id = ?
            ORDER BY (ended_at IS NULL) DESC, id DESC LIMIT 1""",
        (app_id,),
    )
    return row["channel_id"] if row is not None else None


def _revert_line(app_name: str) -> str:
    """The one line a revert writes."""
    return f"**{_one_line(app_name)}** was reverted to its previous live tree."


@router.post("/apps/{app_id}/revert")
async def revert_app(app_id: str, user: CurrentUser) -> dict[str, Any]:
    """Swap live and live.prev, back to the previous live deploy."""
    app = await _require_app(app_id)
    _require_app_owner(app, user)
    if app["status"] != "live":
        raise HTTPException(
            status_code=409, detail="This app is not live, so there is nothing to undo"
        )
    await _launch("revert", app["id"])

    channel_id = await _build_room(app["id"])
    if channel_id is not None:
        try:
            await deliver_message(
                channel_id, "bot", await _system_bot_id(), _revert_line(app["name"])
            )
        except Exception:  # noqa: BLE001
            logger.exception("app revert line failed for app %s", app["id"])
    return {"ok": True, "status": app["status"]}


class ShareRequest(BaseModel):
    """`visibility` is absent by default, not 'shared'. Sharing widens: a default of 'shared'
    would narrow a public app the owner shared into one more room.
    """

    channel_id: int
    visibility: Optional[Literal["shared", "public"]] = None


def _shared_visibility(current: str, requested: Optional[str]) -> str:
    """What an app's visibility becomes when shared into a room. Widen, never narrow, unless the
    owner named the narrower word. Public stays public.
    """
    if requested is not None:
        return requested
    return "public" if current == "public" else "shared"


@router.post("/apps/{app_id}/share")
async def share_app(
    app_id: str, body: ShareRequest, user: CurrentUser
) -> dict[str, Any]:
    """Share a live app into a channel and post its card there."""
    secret, origin = _gate_config()
    app = await _require_app(app_id)
    _require_app_owner(app, user)
    if app["status"] != "live":
        raise HTTPException(
            status_code=409,
            detail="Only a live app can be shared; press Live first",
        )

    channel = await db.fetch_one(
        "SELECT * FROM channels WHERE id = ?", (body.channel_id,)
    )
    if channel is None:
        raise HTTPException(status_code=404, detail="Channel not found")
    # Membership before type: a non-member learns nothing about the room, not
    # even what kind of room it is.
    if not await channels.is_member(channel["id"], "user", user.id):
        raise HTTPException(status_code=403, detail="Not a member of this channel")
    # A build chat has two members and one purpose. Sharing into it would post a card
    # into a transcript the builder reads as instructions.
    channels.require_not_app_build(channel)

    member_ids = await channels.channel_user_ids(channel)
    now = _now()
    visibility = _shared_visibility(app["visibility"], body.visibility)
    async with db.transaction() as conn:
        for member_id in member_ids:
            if member_id == app["owner_user_id"]:
                continue  # the owner's entitlement is ownership, not a row
            await conn.execute(
                """INSERT OR IGNORE INTO app_shares (app_id, user_id, added_at)
                   VALUES (?, ?, ?)""",
                (app["id"], member_id, now),
            )
        await conn.execute(
            "UPDATE apps SET visibility = ?, updated_at = ? WHERE id = ?",
            (visibility, now, app["id"]),
        )

    payload = await deliver_message(
        channel["id"], "user", user.id,
        f"shared an app: **{_one_line(app['name'])}** — {_app_url(origin, app['id'])}",
    )

    row = await _require_app(app["id"])
    await events.publish(
        {
            "type": "app_update",
            "owner_user_id": row["owner_user_id"],
            "app": (await _app_out_one(row, user.id)).model_dump(),
        }
    )
    return {"message_id": payload["id"], "visibility": row["visibility"]}


class RemixRequest(BaseModel):
    builder_bot_id: Optional[int] = None


def _remix_builder(parent: dict[str, Any], requested: Optional[int]) -> int:
    """Which seat builds the remix.

    The remixer's choice if they made one; otherwise the parent's builder,
    which is the resident who already knows this app — but only while that seat
    is still offered, because a session cannot be opened on a builder the
    chooser has retired. Failing that, the first configured seat. If none is
    configured at all, the parent's id goes through and `_require_builder`
    answers with the sentence the chooser uses.
    """
    if requested is not None:
        return requested
    entries = _builder_entries()
    if any(entry["bot_id"] == parent["builder_bot_id"] for entry in entries):
        return parent["builder_bot_id"]
    return entries[0]["bot_id"] if entries else parent["builder_bot_id"]


@router.post("/apps/{app_id}/remix")
async def remix_app(
    app_id: str, user: CurrentUser, body: Optional[RemixRequest] = None
) -> SessionOut:
    """Copy an app the caller can see into one they own, with a build session open on it."""
    _gate_config()
    parent = await _require_app(app_id)
    await _require_entitled(parent, user)
    if parent["status"] != "live":
        raise HTTPException(
            status_code=409, detail="An app can be remixed once it is live"
        )
    builder_bot_id = _remix_builder(parent, (body or RemixRequest()).builder_bot_id)
    await _require_builder(builder_bot_id)
    await _require_quota(user.id)

    child_id = await _mint_app_id()
    now = _now()
    name = (parent["name"] + REMIX_SUFFIX)[:MAX_APP_NAME_CHARS]
    await db.execute(
        """INSERT INTO apps (id, owner_user_id, name, description, visibility,
                             builder_bot_id, parent_app_id, status, repo_path,
                             created_at, updated_at)
           VALUES (?, ?, ?, ?, 'private', ?, ?, 'draft', ?, ?, ?)""",
        (child_id, user.id, name, parent["description"], builder_bot_id,
         parent["id"], f"/srv/apps/{child_id}", now, now),
    )
    try:
        await _launch("remix", parent["id"], child_id)
        return await create_session(
            SessionCreate(builder_bot_id=builder_bot_id, app_id=child_id), user
        )
    except BaseException:
        # The ROW goes. The clone on disk belongs to the helper; the audit line names the
        # id so an operator can find the orphan.
        await db.execute("DELETE FROM apps WHERE id = ?", (child_id,))
        logger.warning(
            "remix of %s rolled back; app row %s deleted, any clone on disk is "
            "the helper's to reap", parent["id"], child_id,
        )
        raise


class CardOut(BaseModel):
    """One app as a card in a room. image_url is null: with no headless browser there is no
    screenshot, and a placeholder URL would 404.
    """

    id: str
    name: str
    description: str
    status: str
    builder: BuilderOut
    owner: dict[str, Any]
    has_previous_live: bool
    parent_app_id: Optional[str] = None
    can_remix: bool
    image_url: Optional[str] = None
    live_url: Optional[str] = None


@router.get("/apps/{app_id}/card")
async def app_card(app_id: str, user: CurrentUser) -> CardOut:
    """The card's facts for an entitled viewer; 404 for everyone else."""
    app = await _require_app(app_id)
    if not await _visible_to(app, user):
        raise HTTPException(status_code=404, detail="App not found")

    builder = await _builder_for(app["builder_bot_id"]) or BuilderOut(
        bot_id=app["builder_bot_id"], name="unknown builder"
    )
    owner = await db.fetch_one(
        "SELECT id, display_name FROM users WHERE id = ?", (app["owner_user_id"],)
    )
    configured = _gate_configured()
    origin = get_settings().APPS_ORIGIN_BASE.rstrip("/")
    live = app["status"] == "live"
    return CardOut(
        id=app["id"],
        name=app["name"],
        description=app["description"],
        status=app["status"],
        builder=builder,
        owner={
            "id": app["owner_user_id"],
            "name": owner["display_name"] if owner is not None else "someone",
        },
        has_previous_live=os.path.isdir(
            os.path.join(get_settings().APPS_WWW_ROOT, app["id"], "live.prev")
        ),
        parent_app_id=app["parent_app_id"],
        can_remix=live and configured,
        image_url=None,
        live_url=_app_url(origin, app["id"]) if (live and configured) else None,
    )


DELETE_WHILE_BUILDING = (
    "A build is running on this app; stop it first, then delete the app."
)
DELETE_WHILE_PUBLISHING = (
    "This app is being published; delete it when that has finished."
)
PLATFORM_APP_KEPT = "The platform's build record is not an app and stays."


async def _mark_app_deleted(app: dict[str, Any]) -> list[int]:
    """Tombstone the row, or 409 on a running turn or publish, in one
    transaction. Returns the users who listed it."""
    now = _now()
    async with db.transaction() as conn:
        sessions = await (await conn.execute(
            "SELECT id, live_publishing_at FROM app_sessions WHERE app_id = ?",
            (app["id"],),
        )).fetchall()
        for session in sessions:
            if session["live_publishing_at"] is not None:
                raise HTTPException(status_code=409, detail=DELETE_WHILE_PUBLISHING)
            if await _running_turn(conn, session["id"]) is not None:
                raise HTTPException(status_code=409, detail=DELETE_WHILE_BUILDING)
        builds = await (await conn.execute(
            """SELECT COUNT(*) AS n FROM app_sessions
                WHERE app_id = ? AND user_id = ? AND mode = 'app'
                  AND turns > 0 AND started_at >= ?""",
            (app["id"], app["owner_user_id"], _utc_day_start()),
        )).fetchone()
        viewers = await (await conn.execute(
            """SELECT user_id FROM app_shares WHERE app_id = :id
               UNION SELECT user_id FROM user_apps WHERE app_id = :id""",
            {"id": app["id"]},
        )).fetchall()
        for session in sessions:
            await _close_session(conn, session["id"], now)
        await conn.execute("DELETE FROM app_shares WHERE app_id = ?", (app["id"],))
        await conn.execute("DELETE FROM user_apps WHERE app_id = ?", (app["id"],))
        await conn.execute(
            """UPDATE apps SET deleted_at = ?, deleted_builds = ?, updated_at = ?
                WHERE id = ?""",
            (now, builds["n"], now, app["id"]),
        )
    return sorted({app["owner_user_id"], *(row["user_id"] for row in viewers)})


@router.delete("/apps/{app_id}")
async def delete_app(app_id: str, user: CurrentUser) -> dict[str, bool]:
    """Row first, so a failed later step leaves an app nobody can reach;
    the owner's next call resumes."""
    _check_app_id(app_id)
    app = await db.fetch_one("SELECT * FROM apps WHERE id = ?", (app_id,))
    if app is None or (
        app["deleted_at"] is not None and app["owner_user_id"] != user.id
    ):
        raise HTTPException(status_code=404, detail="App not found")
    _require_app_owner(app, user)
    if app_id == PLATFORM_APP_ID:
        raise HTTPException(status_code=400, detail=PLATFORM_APP_KEPT)

    if app["deleted_at"] is None:
        user_ids = await _mark_app_deleted(app)
        await events.publish(
            {"type": "app_delete", "app_id": app_id, "user_ids": user_ids}
        )
    await _launch("delete", app_id)
    rooms = await db.fetch_all(
        """SELECT * FROM channels
            WHERE type = 'app_build'
              AND id IN (SELECT channel_id FROM app_sessions
                          WHERE app_id = ? AND mode = 'app')""",
        (app_id,),
    )
    for room in rooms:
        await channels.destroy_channel(room, user.id)
    await db.execute(
        "UPDATE apps SET purged_at = ? WHERE id = ? AND purged_at IS NULL",
        (_now(), app_id),
    )
    await events.publish(
        {"type": "app_delete", "app_id": app_id, "user_ids": [user.id]}
    )
    return {"ok": True}
