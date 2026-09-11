"""APPS — the registry, the build sessions, and the stage stream.

SPECS/2026-08-30-apps-tab-v1.md (confirmed by plink, #custodian seq 2211;
stage 1 opened at Round 12) and SPECS/2026-09-06-apps-builder-seat.md §J
(confirmed seq 2302; stage 2 slice (ii) gives the stage stream a TURN).

| method | path                             | who                          |
|--------|----------------------------------|------------------------------|
| GET    | /apps                            | user   — my menu + my quota   |
| GET    | /apps/discover                   | user   — visible, not on menu |
| GET    | /apps/builders                   | user   — the chooser's cards  |
| GET    | /apps/quota                      | user                          |
| POST   | /apps/sessions                   | user   — start a build        |
| GET    | /apps/sessions/{id}              | owner                         |
| GET    | /apps/sessions/{id}/harness-view | broker bot / admin            |
| POST   | /apps/sessions/{id}/heartbeat    | owner  — push the lock out    |
| POST   | /apps/sessions/{id}/end          | owner  — idempotent           |
| POST   | /apps/sessions/{id}/stage        | broker bot / admin            |
| POST   | /apps/sessions/{id}/live         | owner  — publish this build   |
| PATCH  | /apps/{app_id}                   | owner  — name, description    |
| POST   | /apps/{app_id}/menu              | user   — if visible to them   |
| DELETE | /apps/{app_id}/menu              | user   — not the owner's own  |
| POST   | /apps/{app_id}/open              | entitled — a signed grant URL |
| POST   | /apps/{app_id}/revert            | owner  — back to the last one |
| POST   | /apps/{app_id}/share             | owner  — card into a channel  |
| POST   | /apps/{app_id}/remix             | entitled — a copy I own       |
| GET    | /apps/{app_id}/card              | entitled — what a card shows  |
| GET    | /apps/config                     | user   — the apps origin      |

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

THE STAGE ENDPOINT IS THE ONLY WRITER of `app_stage_events`, and it is gated
to the configured harness seats and admins — never to the session's own owner,
who is who the stream is FOR. The consumers (the client store, its stage bar)
subscribe; a second one must be addable without touching the publisher.

A TURN IS THE UNIT STAGE 2 ADDED. The broker hands one build prompt to the
apps-builder seat and posts the turn's progress here: `scoped` at spawn,
`scaffolded` when the marker lands, `files_written` when the unit's result is
read, `deployed` when the tree was published. A halted turn re-posts its LAST
REACHED stage with `detail.halted` set, because the five-stage vocabulary is a
CHECK constraint and a halt is not a sixth stage. That is why every reader that
renders a label keys off `detail`, never off the stage name: `files_written`
is the turn's terminal stage, not literally "files were written"
(SPECS/2026-09-06-apps-builder-seat.md §A, Claudette #2293).

Two things follow from the turn, and both live here rather than in the broker:
`app_sessions.turns` / `.tokens_used` are maintained from `detail` (§J, so the
modal's hidden ceiling never needs the broker's ledger), and the §H system
line is written into the build channel as a SERVER-SIDE effect of the event —
the broker has no post right in that room, and stage 1's two-member wall
(#2266) stays the only path into it.

STAGE 3 ADDED THE SERVING VERBS, and they are the second half of this file.
`live` is the user's explicit "done": it publishes the preview tree to the app's
public root through the host helper, flips `apps.status`, and lands a `live`
stage event on the SAME path the broker's turns take, so the modal's bar and the
room's transcript learn about it the way they learn about everything else. Open,
share, revert and remix hang off that state.

VISIBILITY IS ENFORCED BY A SIGNED GRANT, not by a URL nobody guesses (Wall 2).
`/open` mints one — app id, root set, expiry, and the viewer's context blob,
HMAC'd with a secret this house shares with the gate and with nothing else
(app/apps_grant.py). The gate has no database: what it can serve to whom is
exactly what the house signed. So every mint here is preceded by the same
entitlement question `_visible_to` already answers, and the preview root is
minted for the owner alone.

THE HOST OPERATIONS ARE NOT DONE HERE (D6). Publishing, reverting and cloning a
tree happen as the apps seat, through one fixed-argv `sudo -n` helper with three
modes; this file spawns it and reads three outcomes — done, refused (64, with
one sentence for the user), failed. Nothing about a caller's request becomes a
word of that command line, and there is no shell.

STILL DEFERRED, with the seam named: the screenshot at `live` (D9 — no headless
browser on this host; `card.image_url` is null and that is the whole hook).
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

# D10: new apps are born unnamed-but-nameable. The owner renames via PATCH.
DEFAULT_APP_NAME = "Untitled app"
MAX_APP_NAME_CHARS = 60
MAX_APP_DESCRIPTION_CHARS = 300

# A stage event's `detail` is free-form (filenames, a note), so it is bounded
# the way messages bounds its metadata: an uncapped JSON field on a write path
# is a side channel with a database behind it.
MAX_STAGE_DETAIL_CHARS = 2000

# The two free-text fields inside a detail — the runner's one-line report and a
# halt's reason. Both are rendered into a room message, so they are bounded
# well under the detail's own cap: a sentence, not a paragraph.
MAX_STAGE_LINE_CHARS = 300

# How much of a turn's file list the §H line names before it counts the rest.
# The scroll in the modal shows them all; the transcript line is a sentence.
STAGE_LINE_FILES = 8

# Why a turn stopped. A CLOSED set, because each value picks a sentence: an
# unknown reason would be a turn that halted with nothing said about it.
HaltReason = Literal["timeout", "error", "secret", "ceiling", "stopped"]

# One sentence per halt, keyed by the reason. `ceiling` is the only one the
# broker posts without anything having run (D-1.2b): the refusal still reaches
# the room and the bar, because a handoff that was declined is a fact about
# the build and not just about the broker.
HALT_SENTENCES: dict[str, str] = {
    "ceiling": "build hit its ceiling.",
    "timeout": "the build timed out.",
    "error": "the build failed.",
    # Slice (iv): the user pressed Stop. The harvest tells this from the clock
    # by the marker the launcher's `stop` dropped before `systemctl stop`.
    "stopped": "stopped by the user.",
    "secret": (
        "the build tried to write a credential; this session is closed and an "
        "admin has been notified."
    ),
}

# D4: 12 chars of lowercase base32 (RFC 4648 alphabet, lowercased), minted from
# secrets. 60 bits, and never sequential — an app id lands in a URL path
# segment on an origin shared by every app, and a sequential id there is a map
# of the neighbours.
APP_ID_CHARS = 12
APP_ID_RE = re.compile(r"^[a-z2-7]{12}$")
_APP_ID_ATTEMPTS = 5

# Stage 3. The gate secret is an HMAC key shared with a second process; 32
# bytes is the sha256 block-ish floor below which "we have a secret" stops
# being a true sentence. Shorter (or absent) reads as NOT CONFIGURED, which is
# a supported state of this house and not an error at boot.
MIN_GATE_SECRET_BYTES = 32

# What every endpoint that needs the gate says when it is not configured. One
# sentence, no configuration detail: the person reading it is a user, and the
# person who can fix it is reading the unit's logs, not this body.
GATE_UNCONFIGURED = (
    "App serving is not configured on this house yet, so this app cannot be "
    "opened outside the build modal."
)

# The same answer, for the one misconfiguration that is WORSE than missing:
# the apps origin listed as a house origin. Same 503, because to a user it is
# the same fact — serving is not usable — and the detail that would tell them
# more is a config line only an admin can act on.
GATE_ORIGIN_COLLIDES = (
    "App serving is not configured correctly on this house, so this app "
    "cannot be opened outside the build modal; an admin has the log."
)

# Seconds the host helper gets. A publish is an rsync of a small static tree
# and a rename; a remix is a local `git clone`. Two minutes is a wall, not a
# budget — past it the request has been holding a socket for something that
# is not going to finish.
HELPER_TIMEOUT_SEC = 120

# Exit codes of `disjorn-apps-launch` (D6). 0 done, 64 refused with a sentence
# on stderr, anything else failed. A refusal is the user's business (409 with
# the helper's sentence); a failure is the house's (502, and the log).
HELPER_REFUSED = 64

# The name a remix takes. The parent's name plus one word, because a remix the
# user has not renamed yet must still be distinguishable from its parent in a
# menu that shows both.
REMIX_SUFFIX = " (remix)"

# Roots the gate knows. `preview` is the owner's private view of the tree the
# builder is writing; `live` is what everybody else can be granted.
AppRoot = Literal["live", "preview"]


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


class StageDetail(BaseModel):
    """What a stage event is allowed to SAY about a turn.

    Every field is optional and unknown keys are still accepted, because
    `detail` was and stays free-form: a stage-1 event's bare `{}` is still a
    valid event, and a publisher that learns a new fact should not need a
    server release to report it. What this model buys is that the keys the
    server itself ACTS on — the ones that move `turns`, add to `tokens_used`,
    and choose the sentence written into the room — are typed at the door
    rather than guessed at the point of use. `halted` in particular is a
    closed set: it selects a sentence, so a value nobody wrote a sentence for
    is a 422 here rather than a silent no-line later.

    `protected_namespaces` is cleared for `model`, which names the model the
    turn ran on and is a wire word from the spec, not a pydantic attribute.
    """

    model_config = ConfigDict(extra="allow", protected_namespaces=())

    turn: Optional[int] = Field(default=None, ge=1)
    # False on a ceiling refusal: the event is posted for the room but nothing
    # ran, so it must not advance the turn counter — the next real handoff
    # reuses this turn number (Gable #2347, Claudette #2349).
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
        nothing this model merely defaults to None. It is persisted, echoed on
        the bus, and read back by the modal, so a round trip must not grow
        fields the publisher never wrote."""
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
    """One build session as the BROKER needs to see it (§E.1).

    The broker's four pre-flight checks read exactly this and nothing else: is
    the session there, is it open, whose builder is it, and has it spent its
    ceiling. It is a separate shape from SessionOut on purpose — SessionOut is
    the modal's payload, full of things the harness has no business holding
    (the app's description, the builder's avatar, the owner's quota), and a
    harness that reads the user's view would drift into rendering it.

    `lock_lapsed` is INFORMATIONAL. The lock is the user's chat exclusivity,
    pushed out by the modal's heartbeat; it does not gate a handoff, because a
    resident that hands a prompt over seconds after the user closed the modal
    should still land the turn (keyboard ruling D-A1). `open` is the door.
    """

    session_id: int
    app_id: str
    owner_user_id: int
    builder_bot_id: int
    channel_id: int
    stage: Optional[AppStage] = None
    turns: int
    tokens_used: int
    open: bool
    lock_lapsed: bool
    ended_at: Optional[str] = None
    locked_until: str
    # Slice (iv): set when the owner asked for the running turn to stop. The
    # broker's reaper reads it each poll and sends the unit its stop once.
    stop_requested_at: Optional[str] = None


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


async def _require_builder(bot_id: int) -> BuilderOut:
    """The builder card for a bot that may actually be built with, or 400.

    Two facts, both required: the seat is IN the chooser's config, and it has a
    row in `bots`. Shared by the chooser's start-session and by remix so the
    two cannot drift into offering different sets of builders.
    """
    entry = _builder_entry(bot_id)
    builder = await _builder_for(bot_id) if entry is not None else None
    if entry is None or builder is None:
        raise HTTPException(
            status_code=400,
            detail="That builder is not available for app builds on this server",
        )
    return builder


async def _require_quota(user_id: int) -> Quota:
    """The caller's meter, refusing at zero. Every verb that OPENS a session
    (the chooser's and remix's) spends from the same one."""
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


def _one_line(text: Any) -> str:
    """A publisher's free text, made safe to put in a message.

    The runner's summary and a halt's reason both come from a build seat that
    read a prompt somebody typed. They are plain text in the transcript and
    nothing else: control characters (a newline included) collapse to spaces,
    so one line cannot become several and cannot smuggle a blank one, and the
    result is bounded again here rather than trusted from the door.
    """
    if not isinstance(text, str):
        return ""
    flattened = "".join(" " if ch < " " or ch == "\x7f" else ch for ch in text)
    line = " ".join(flattened.split())
    if len(line) > MAX_STAGE_LINE_CHARS:
        # Capped WITH an ellipsis: the first real build's summary was cut
        # mid-word inside its quotes and read as a message that had hung
        # (plink, 2026-09-09). A cut that says it is a cut is a cap; a cut
        # that does not is a bug report.
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
    """The §H line for one turn.

    ONE message per turn outcome, authored by `system`, and it is the build
    summary in the resident's transcript as much as it is the user's receipt
    (§H, parent B9: the resident later says "the app I built you" because the
    transcript says so). Like the opener it carries no context block, so it
    summons nobody.

    A halt wins over `files_written`, whatever stage the event re-posted: a
    turn that died at `scoped` must not leave the room silent, and a turn that
    died after writing files is still a halt (Gable #2295, Claudette #2299).
    """
    turn = detail.get("turn")
    number = turn if isinstance(turn, int) and not isinstance(turn, bool) else 0
    # The summary is the BUILDER's words, from an isolated seat that read a
    # user's prompt — not the house's. It is quoted after the house sentence so
    # neither the user nor the resident reads it as an attestation (Gable #2347,
    # Claudette #2349). Empty summary → the plain line, no dangling quotes.
    summary = _one_line(detail.get("summary"))
    tail = f' "{summary}"' if summary else ""

    halted = detail.get("halted")
    if isinstance(halted, str) and halted in HALT_SENTENCES:
        reason = _one_line(detail.get("reason"))
        # A halt that committed a tree still names what it wrote: the user's
        # next prompt is written against that repo, and "the build failed"
        # with no file list leaves them editing changes they were never told
        # about (Claudette #2352). Halts have facts too.
        raw = detail.get("files")
        halt_files = [f for f in raw if isinstance(f, str)] if isinstance(raw, list) else []
        wrote = f" Wrote {_files_word(halt_files)}." if halt_files else ""
        # A halt that carried a report keeps it: the turn where the runner
        # wrote something and then failed is the turn the report is most worth
        # reading (Claudette #2354). Same quoted-builder-words rendering.
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
    """End a session and release its lock, keeping the FIRST ending.

    Both endings run through here — the owner's `end` verb and the server's
    own on a credential halt — so "ended" means one thing in the table. The
    `ended_at IS NULL` guard is what makes it idempotent: a second call is a
    duplicate click or a retry, not a second ending, and rewriting the
    timestamp would quietly move when the build finished.

    `locked_until` comes back to now in the same statement. The lock is only
    ever read as "is it still in the future", so leaving a future lock on an
    ended session would be a row that answers two questions differently.
    """
    await conn.execute(
        """UPDATE app_sessions SET ended_at = ?, locked_until = ?
            WHERE id = ? AND ended_at IS NULL""",
        (moment, moment, session_id),
    )


async def _running_turn(conn: Any, session_id: int) -> Optional[int]:
    """The turn number of the turn that is running NOW, or None.

    A turn is running from its `scoped` event until its terminal event: a
    `files_written`, or any event carrying `detail.halted`. Derived from the
    stage stream rather than kept as a column, so it cannot disagree with the
    events the modal and the room already saw. A ceiling refusal
    (`spawned: false`) never started a turn and does not count as one.
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
    """Close the session and release the lock. Idempotent (_close_session).

    Ending while a turn runs STOPS THE TURN FIRST (slice (iv)): the stop is
    recorded before `ended_at`, in one transaction, so the reaper's next
    harness-view read sees both and the unit gets its `systemctl stop`. Before
    this, `end` only set `ended_at`, the unit ran to `turn_max`, and its own
    record 410'd on arrival (Gable #2393).
    """
    session = await _require_session(session_id, user)
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
    """Ask for the running turn to stop (slice (iv)). Owner-only, same auth
    as `end`.

    This records a request; it does not stop anything itself. The broker's
    reaper reads `stop_requested_at` off harness-view and sends the unit ONE
    `systemctl stop`; the unit's TERM trap harvests; the harvest's record is
    the turn's terminal event and lands in the room as `stopped by the user`.
    Nothing here promises when — the hard bound is the unit's stop timeout,
    and the dialog says "about a minute and a half" for that reason.

    Idempotent: a second click keeps the first timestamp. 409 when no turn is
    running (there is nothing to stop, and saying so beats a request that
    would sit on the row until some later turn inherited it). 410 on an ENDED
    session only — not on a lapsed lock (Claudette #2447): the lock is chat
    exclusivity, not a door, and the owner who walked away from a modal and
    came back to a runaway turn is exactly the owner who needs this verb.
    `end` gates the same way; publish_stage's harness path already did.
    """
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


# ---------------------------------------------------------------------------
# POST /apps/sessions/{id}/stage — the publisher half of the stage stream
# ---------------------------------------------------------------------------

def _require_stage_publisher(actor: Actor) -> bool:
    """Only the configured build-harness seats, or an admin.

    A stage event is an ATTESTATION about what a build actually did — the modal
    renders it as fact and the app's status hangs off `live`. So the publisher
    list is config a person edits (APPS_STAGE_PUBLISHER_BOT_NAMES), never
    something a caller asserts about itself. The session's own owner is
    deliberately NOT on the list: the user is who the stream is FOR.

    Returns whether the caller is the HARNESS rather than an admin. The two
    are not interchangeable at the session gate: the harness reports turns it
    already ran, and an admin is a person poking the stream by hand.
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
    """The stage stream's ONE writer, inside the caller's transaction.

    Insert the event, move the session's stage pointer, and — because `live` is
    the app's done state and not just a label on a bar — flip `apps.status` in
    the same transaction. Two facts that are one fact must not be two writes
    that can disagree.

    Both publishers go through here: the broker's `POST …/stage` and the
    owner's `POST …/live`. The alternative was a second INSERT with the same
    columns in the `live` endpoint, which is exactly how a stream grows a
    second shape nobody notices until a reader keys off the difference.

    `move_pointer` is false for a ceiling refusal (`spawned: false`): the event
    posts for the room but nothing ran, and rewinding a session sitting at
    `deployed` back to `scoped` would be a bar that lies.
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
    """The `app_stage` frame, published AFTER commit, to the owner's sockets.

    Same shape from both publishers, for the same reason the write is shared:
    the modal's stage bar must not be able to tell who moved it.
    """
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
    """What the broker reads before it hands a prompt to the build seat (§E.1).

    Publisher-gated by the same list that gates the stage endpoint, because it
    is the same relationship in the other direction: the harness reports turns
    here and asks about them here, and nobody else does either.

    It answers about a session that has ENDED as readily as one that is open —
    `open` is a field, not a status code. A refusal the broker has to turn back
    into "this build session has ended" is a refusal that tells it less than
    the row does.
    """
    _require_stage_publisher(actor)
    session = await _publisher_session(session_id)
    return HarnessView(
        session_id=session["id"],
        app_id=session["app_id"],
        owner_user_id=session["user_id"],
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
    )


@router.post("/apps/sessions/{session_id}/stage")
async def publish_stage(
    session_id: int, actor: CurrentActor, body: StagePublish = Body(...)
) -> StageEventOut:
    """Persist a stage event, mark the session, publish it to the owner, and —
    when the event ends a turn — write the turn's line into the room.

    Persisted BEFORE it is published, so a modal that reloads sees the same
    stages a live modal saw. `live` is also the app's done state, so it flips
    `apps.status` in the same transaction — the two facts are one fact. So is
    a credential halt and the session's ending: §E closes the session on a
    secret, and a turn recorded as quarantined next to a session still taking
    handoffs would be two answers to one question.

    THE SESSION GATE DEPENDS ON WHO IS ASKING (keyboard ruling D-B3). For the
    harness, only `ended_at` closes the door: the turn already ran, and the
    record of it has to land in the room even if the user's modal went away
    and let the lock lapse mid-build. An admin publishing by hand keeps the
    old gate, lapse included — a person poking a session nobody is watching is
    the case the lock is there to catch.

    Fan-out is the OWNER's sockets only (app/ws.py). Not the builder bot: a
    resident reading the harness's report of its own build back as an inbound
    frame is a loop nobody asked for. The §H line is a different path on
    purpose — it is a MESSAGE in the build channel, so both members see it the
    way they see everything else in the room.
    """
    from_harness = _require_stage_publisher(actor)
    session = await _publisher_session(session_id)
    if from_harness:
        if session["ended_at"] is not None:
            # Slice (iv): `end` while a turn ran stopped the turn first, and
            # the record of THAT turn must still land — it is the room's only
            # account of what the stop kept. Only that turn gets through the
            # 410: the one still running per the stream, and only while the
            # stop it was given is still on the row.
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
        # A spawned:false event (a ceiling refusal) posts for the room but
        # nothing ran, so it moves NOTHING about the session's progress — not
        # the turn counter (below) and not the stage pointer, which would
        # otherwise rewind a session sitting at `deployed` back to `scoped`
        # (Claudette #2352 BLOCK 2).
        event_id = await _write_stage_event(
            conn, session, body.stage, detail_json, created_at,
            move_pointer=body.detail.spawned is not False,
        )
        # The turn counter is a MAX, not an increment: one turn posts several
        # events, and a counter that added one each time would count events.
        # A spawned:false event (a ceiling refusal) is NOT a turn — nothing
        # ran — so it never advances the counter, and the next real handoff
        # reuses its number (Gable #2347).
        if body.detail.turn is not None and body.detail.spawned is not False:
            await conn.execute(
                "UPDATE app_sessions SET turns = MAX(turns, ?) WHERE id = ?",
                (body.detail.turn, session_id),
            )
        # Usage lands ONCE PER TURN, on the event that read the runner's
        # result — which is `files_written` for a turn that finished and the
        # re-posted `scoped`/`scaffolded` for a turn that burned tokens and
        # then halted (result.json carries `usage` on those paths too, since
        # 863c909). Meter on the PRESENCE of `tokens`, never on the stage
        # NAME, or a build that fails repeatedly — the exact runaway a ceiling
        # exists to stop — never trips it (Claudette #2352 BLOCK 1). §E's
        # invariant: exactly one event per turn carries `tokens`. A
        # spawned:false refusal carries none and is guarded regardless.
        # …and the meter is a running `+=`, so "exactly one event per turn
        # carries tokens" cannot be left to the reaper's good behaviour: a
        # retried post after a timeout it thought failed would double-charge
        # the ceiling. Enforced here — a turn's tokens are counted only if no
        # EARLIER event for the same turn already carried them (Claudette
        # #2354). The just-inserted row is excluded by id.
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
        # A terminal event consumes the stop request, whatever the reason the
        # turn ended for: the next turn of this session starts unasked.
        if terminal and body.detail.spawned is not False:
            await conn.execute(
                "UPDATE app_sessions SET stop_requested_at = NULL WHERE id = ?",
                (session_id,),
            )

    await _publish_stage_frame(session, body.stage, detail, created_at)

    if body.stage == "files_written" or halted is not None:
        try:
            await deliver_message(
                session["channel_id"], "bot", await _system_bot_id(),
                _turn_line(detail),
            )
        except Exception:  # noqa: BLE001
            # The event is durable and the bar has already moved; failing the
            # request here would tell the broker its turn did not land when it
            # did, and the broker would post the whole thing again.
            logger.exception("app build turn line failed for session %s", session_id)

    return StageEventOut(
        id=event_id,
        session_id=session_id,
        stage=body.stage,
        detail=detail,
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


# ---------------------------------------------------------------------------
# Stage 3: the gate's half of the house
# (SPECS/2026-09-09-apps-serving-gate.md D6–D9)
# ---------------------------------------------------------------------------

def _gate_config() -> tuple[bytes, str]:
    """The secret and the origin, or 503 with a sentence.

    NOT a boot assertion, deliberately (D-config): a house with no apps origin
    still builds apps — the modal, the chat, the turns and the stage bar all
    work — and only the four verbs that need somewhere to point a browser
    refuse. Turning the gate on must not be a prerequisite for the house
    starting, or the feature's config becomes the house's uptime.
    """
    settings = get_settings()
    secret = settings.APPS_GATE_SECRET.encode("utf-8")
    origin = settings.APPS_ORIGIN_BASE.rstrip("/")
    if len(secret) < MIN_GATE_SECRET_BYTES or not origin:
        raise HTTPException(status_code=503, detail=GATE_UNCONFIGURED)
    # …and it must not be one of US (Claudette #2551). The read side of the
    # origin wall rests on the apps origin being FOREIGN to this house: that is
    # what makes a cookie-bearing write from app-served JS a cross-origin
    # request the wall rejects. Put the apps origin into HOUSE_ORIGINS and the
    # wall waves that JS through on every cookie the browser already attached,
    # and nothing anywhere says so. Refusing to mint is the fail-closed answer
    # — no grant, no app in a frame, no JS to wave through — and it is checked
    # here, at the mint, rather than at boot for the same reason nothing else
    # about the gate is: a house with an unconfigured gate still builds apps.
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
    """`<origin>/<app>/` — plus `preview/` for the owner's root.

    The trailing slash is load-bearing: the app's own relative asset paths
    resolve against it, and D4's builder rule (relative paths only) assumes it.
    """
    return f"{origin}/{app_id}/" + ("preview/" if root == "preview" else "")


def _house_absolute(path: Optional[str]) -> Optional[str]:
    """A house-relative URL made absolute for a document on the apps origin.

    An avatar URL is `/avatars/3?v=…`, which means nothing inside an iframe
    served from a different origin. This is identity metadata for the context
    blob, not a promise that the image loads: the per-app CSP is `'self'`, so
    in v1 an app that tries to render it is refused by the browser. Absolute is
    still the honest value — the relative one would silently point at a file
    the gate does not have.
    """
    if not path:
        return None
    origins = get_settings().HOUSE_ORIGINS
    return f"{origins[0].rstrip('/')}{path}" if origins else path


async def _context_blob(
    secret: bytes, app: dict[str, Any], user: User
) -> dict[str, Any]:
    """What the app is told about the person looking at it (D2/D5).

    The viewer's id here is the OPAQUE per-app id, never the account id: an app
    may recognise a returning visitor and must not be able to name them to
    another app. `shared_with` is display names — the app's own "who else is
    here" — and `theme` is the seam the client fills later, empty rather than
    absent so an app can read it without a guard.
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
    """Owner, shared-with, or public — the same wall `_visible_to` is, said as
    a refusal. Every grant this file mints is behind it."""
    if not await _visible_to(app, user):
        raise HTTPException(
            status_code=403, detail="This app has not been shared with you"
        )


# ---------------------------------------------------------------------------
# The host helper — three modes, fixed argv, no shell (D6)
# ---------------------------------------------------------------------------

def _run_launch_helper(argv: list[str]) -> subprocess.CompletedProcess:
    """Spawn `disjorn-apps-launch`. THE ONLY place this process spawns anything.

    One function so there is one thing to audit and one thing to fake in a
    test. `subprocess.run` with a LIST and no `shell=`: the app ids in that
    list are already `^[a-z2-7]{12}$` by the time they get here, and the helper
    shape-checks them again on its own side, but the argv form is what makes
    both of those belt-and-braces rather than the only defence.
    """
    return subprocess.run(  # noqa: S603 — fixed argv, no shell, checked ids
        argv,
        capture_output=True,
        text=True,
        timeout=HELPER_TIMEOUT_SEC,
        check=False,
    )


async def _launch(mode: str, *args: str) -> None:
    """Run one helper mode and turn its exit code into this house's answer.

    Off the event loop (the helper takes as long as an rsync takes, and this
    process serves every other request meanwhile). Three outcomes and no
    fourth: 0 done, 64 refused — the helper's one sentence becomes a 409 the
    user can act on — anything else failed, which is a 502 and a log line,
    because a failure is the house's problem and its details are not the
    caller's business.
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


# ---------------------------------------------------------------------------
# GET /apps/config — what the client needs to know about the origin
# ---------------------------------------------------------------------------

@router.get("/apps/config")
async def apps_config(user: CurrentUser) -> dict[str, Any]:
    """The apps origin, so the client can tell an apps link from any other
    link and render it as a card (D8).

    `origin_base` is "" on a house with no gate configured, and `configured`
    says so directly rather than making the client infer it from an empty
    string — the buttons that need the gate are disabled by that flag.
    """
    return {
        "origin_base": get_settings().APPS_ORIGIN_BASE.rstrip("/"),
        "configured": _gate_configured(),
    }


# ---------------------------------------------------------------------------
# POST /apps/{id}/open — mint a grant
# ---------------------------------------------------------------------------

class OpenRequest(BaseModel):
    root: AppRoot = "live"


@router.post("/apps/{app_id}/open")
async def open_app(
    app_id: str, user: CurrentUser, body: Optional[OpenRequest] = None
) -> dict[str, str]:
    """A URL that opens this app for THIS viewer, and for nobody they send it to.

    The grant is the entitlement (Wall 2): it is minted here, behind the same
    visibility check every other read of an app passes, and the gate serves
    nothing without one. It carries the app id, so a grant for one app cannot
    open another on the shared origin; it carries the roots, so `preview` — the
    tree the builder is still writing — is reachable only by the owner; and it
    expires, so a link that leaks stops working on its own.

    Minted FRESH on every call rather than cached: revocation is then bounded
    by the TTL, and the context blob (who else it is shared with, the viewer's
    name) is current every time the app is opened.
    """
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
        # Not 403: the viewer is entitled, there is simply nothing published
        # yet. The modal says "press Live" and a card says "not live yet".
        raise HTTPException(status_code=409, detail="This app is not live yet")

    owner = app["owner_user_id"] == user.id
    token = apps_grant.mint(
        secret,
        app_id=app["id"],
        roots=["live", "preview"] if owner else ["live"],
        ctx=await _context_blob(secret, app, user),
    )
    return {"url": f"{_app_url(origin, app['id'], root)}?t={token}"}


# ---------------------------------------------------------------------------
# POST /apps/sessions/{id}/live — the user's explicit "done"
# ---------------------------------------------------------------------------

async def _session_files(session_id: int) -> list[str]:
    """Every file this session's turns reported writing, in order, deduped.

    Read back off the stage stream rather than kept in a column: the events are
    already the record of what the build did, and a second store of the same
    fact is a second thing that can be wrong.
    """
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
    """The B9 summary, written into the build room by `system`.

    The user's receipt AND the resident's transcript: the builder reads this
    room, so this line is how it later knows the app it built is live and where
    it lives. Same authorship as the §H turn lines — `system`, so it carries no
    context block and summons nobody.
    """
    noun = "turn" if turns == 1 else "turns"
    tail = f", files: {_files_word(files)}" if files else ""
    return f"**{_one_line(app_name)}** is live at {url} — {turns} {noun}{tail}"


@router.post("/apps/sessions/{session_id}/live")
async def publish_live(session_id: int, user: CurrentUser) -> SessionOut:
    """Publish this build's preview tree to the app's public root.

    Owner only, and only when there is something to publish and nobody writing
    it: a turn in flight would have the publisher snapshotting a half-written
    tree, and a session that has never reached `deployed` has no tree at all.
    Both are 409s with sentences, because both become true on their own if the
    user waits or builds.

    The gate on the SESSION is `ended_at`, not the lock (stop_turn's precedent,
    Claudette #2447): the lock is the modal's chat exclusivity, and an owner
    who let it lapse and came back to a finished build is exactly the owner who
    means to press this.

    Order matters. The helper runs FIRST and outside any transaction — it is a
    filesystem operation that takes seconds and the write lock here is
    process-global. Only once it reports done do we record `live`, which is the
    same write the broker's publisher makes: one path, so the modal's bar, the
    app's status and the room all move together.

    AND EXACTLY ONE PUBLISH RUNS AT A TIME (Claudette #2551). A publish ROTATES
    the trees — live becomes live.prev, preview becomes live — so two of them
    racing leaves `live.prev` a copy of `live`: Revert keeps its button and the
    tree behind it is the one already being served, with nothing in the house
    saying the previous deploy is gone. The claim is a CONDITIONAL WRITE on the
    session row, `stop_requested_at`'s shape (slice (iv)): the database decides
    which press wins, not a read-then-check here that two requests can both
    pass. The loser gets 409 and spawns nothing. The flag clears as soon as the
    helper returns, success or failure, because pressing Live AGAIN later is a
    re-publish and is allowed — what is refused is a second one now.
    """
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

    # The claim. `WHERE live_publishing_at IS NULL` is the whole exclusion: a
    # second press arrives, changes no rows, and is told so.
    claim = await db.execute(
        """UPDATE app_sessions SET live_publishing_at = ?
            WHERE id = ? AND live_publishing_at IS NULL""",
        (_now(), session_id),
    )
    if claim.rowcount == 0:
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
            # The app IS live and the bar has moved; a failed transcript line is
            # not a reason to tell the owner their publish failed (publish_stage's
            # §H line is guarded the same way, for the same reason).
            logger.exception("app live line failed for session %s", session_id)
    finally:
        # Released whatever happened, including a refusal or a 502: the user
        # who fixes what the helper complained about presses Live again, and a
        # flag left set would meet them with a lie about a publish in flight.
        await db.execute(
            "UPDATE app_sessions SET live_publishing_at = NULL WHERE id = ?",
            (session_id,),
        )

    fresh = await db.fetch_one("SELECT * FROM app_sessions WHERE id = ?", (session_id,))
    assert fresh is not None
    return await _session_out(fresh, user)


# ---------------------------------------------------------------------------
# POST /apps/{id}/revert — back to the previous live deploy
# ---------------------------------------------------------------------------

async def _build_room(app_id: str) -> Optional[int]:
    """The channel this app is built in — its open session's, or its last one's.

    An app has at most one open session at a time, but it has a session
    history, and a verb pressed between builds still has a transcript to land
    in: the most recent room is the one the owner and the builder were last
    talking in. None only when an app has never had a session, which the
    registry cannot produce today and a caller must still survive.
    """
    row = await db.fetch_one(
        """SELECT channel_id FROM app_sessions WHERE app_id = ?
            ORDER BY (ended_at IS NULL) DESC, id DESC LIMIT 1""",
        (app_id,),
    )
    return row["channel_id"] if row is not None else None


def _revert_line(app_name: str) -> str:
    """The one line a revert writes. Same authorship and shape as the B9 line:
    `system`, so it carries no context block and summons nobody."""
    return f"**{_one_line(app_name)}** was reverted to its previous live tree."


@router.post("/apps/{app_id}/revert")
async def revert_app(app_id: str, user: CurrentUser) -> dict[str, Any]:
    """Swap `live` and `live.prev`. Owner only, live only.

    The house writes NOTHING here: which tree is served is a fact about the
    filesystem, and the app is just as live after a revert as before it. The
    helper refuses (64 → 409) when there is no previous deploy to come back to,
    which is the one check this endpoint deliberately does not duplicate — the
    directory it would test is the helper's to own, and two answers to "is
    there a previous live" is one too many.

    What it DOES write is one line into the build room (Claudette #2551). The
    app the room was talking about changed underneath it: the builder's
    transcript says a tree was published and the URL now serves a different
    one, and a resident reading back would otherwise answer questions about
    code that is no longer live. Posted after the helper returns, so the line
    is a record of something that happened; guarded like the B9 line, because
    the tree IS swapped and a failed message is not a failed revert.
    """
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


# ---------------------------------------------------------------------------
# POST /apps/{id}/share — the card is a message (D8)
# ---------------------------------------------------------------------------

class ShareRequest(BaseModel):
    """`visibility` is ABSENT by default, not `"shared"` (Claudette #2551).

    Sharing is a widening verb — it hands an app to a room — and a default of
    `"shared"` made it a narrowing one for any app that was already public: the
    owner who pressed Share into one more channel silently took the app back
    off the house. None means "at least shared, and never less than it is now";
    a value means the owner said it, including when they said `"shared"` about
    a public app.
    """

    channel_id: int
    visibility: Optional[Literal["shared", "public"]] = None


def _shared_visibility(current: str, requested: Optional[str]) -> str:
    """What an app's visibility becomes when it is shared into a room.

    Widen, never narrow, unless the owner said the narrower word themselves.
    `private` and `shared` both become `shared` — the room's members were just
    entitled by rows, and that IS shared. `public` stays public: it is already
    wider than anything this verb grants, and quietly dropping back to `shared`
    would un-entitle the whole house on a press the owner read as "share this
    with one more room".
    """
    if requested is not None:
        return requested
    return "public" if current == "public" else "shared"


@router.post("/apps/{app_id}/share")
async def share_app(
    app_id: str, body: ShareRequest, user: CurrentUser
) -> dict[str, Any]:
    """Share a live app into a channel: entitle its members, then post the card.

    `app_shares` is a SNAPSHOT taken now, not a subscription to the channel:
    somebody who joins the room tomorrow was not shared with today, and a
    membership table that quietly kept granting access would make "shared with"
    mean something nobody chose. Public is the exception and it says so — it
    entitles everyone by visibility, not by rows.

    The card is an ordinary message authored by the SHARING USER through the
    same path every other message takes (deliver_message): seq allocation,
    fan-out, history, unreads. No new message type and no raw row — a client
    that predates the card renderer shows a link that works.

    SHARING NEVER NARROWS (Claudette #2551). See ShareRequest: a public app
    stays public unless this request explicitly asked for something else.
    """
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
    # A build chat has two members and one purpose. Sharing into it would
    # entitle the builder bot's... nobody, and post a card into a transcript
    # the resident reads as instructions.
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


# ---------------------------------------------------------------------------
# POST /apps/{id}/remix — a copy I own, with a session open on it
# ---------------------------------------------------------------------------

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
    """Copy an app I can see into an app I own, and open a build session on it.

    Remix by talking (parent spec): the answer to "this but for my book club"
    is a session, so this endpoint's response IS the chooser's — same shape,
    same modal, same everything downstream. It spends the same daily build
    meter and takes the same locks, because it starts the same kind of session;
    the meter is checked BEFORE the clone so a refused remix leaves nothing on
    disk.

    The row is written before the helper runs and DELETED if ANYTHING after it
    fails — the clone itself, or the session this endpoint opens on it
    (Claudette #2551): an app row whose repo was never cloned is an app that
    opens a session on nothing, and an app row whose session could not be
    opened is a row nobody asked for on a menu. Order the other way round
    (clone first) would leave the directory instead, which the helper's own
    "refuses if the child exists" rule then makes unrecoverable at that id.
    """
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
        # No `user_apps` row is written, and that is the menu being correct
        # rather than the menu being skipped: the remixer OWNS the child, and
        # an owner is on their own menu by construction (`_app_out`'s
        # `on_menu`, and the reason add_to_menu writes nothing for an owner and
        # remove_from_menu refuses). A row here would be the one state
        # `remove_from_menu`'s 400 says cannot exist.
        return await create_session(
            SessionCreate(builder_bot_id=builder_bot_id, app_id=child_id), user
        )
    except BaseException:
        # The ROW goes. The clone on disk is the helper's — this process is not
        # the apps seat and has no business rm-ing a tree it could not have
        # written; what the house leaves behind is an audit line naming the id,
        # which is what an operator needs to find the orphan.
        await db.execute("DELETE FROM apps WHERE id = ?", (child_id,))
        logger.warning(
            "remix of %s rolled back; app row %s deleted, any clone on disk is "
            "the helper's to reap", parent["id"], child_id,
        )
        raise


# ---------------------------------------------------------------------------
# GET /apps/{id}/card — what a shared link renders as
# ---------------------------------------------------------------------------

class CardOut(BaseModel):
    """One app as a card in a room (D8/D9).

    `image_url` is the D9 seam and is null today: there is no headless browser
    on this host, so there is no screenshot to point at, and a card that
    invented a placeholder URL would be a 404 in every room it landed in. The
    client draws its own tile from the name.
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
    """The card's facts, for entitled viewers only.

    404 rather than 403 on a viewer who is not entitled, and the same 404 an
    unknown id gets. This endpoint is fetched from a rendered message body, so
    it is the one apps read that a stranger can aim at a guessed id — and
    "forbidden" would confirm the app exists, which on an origin every app
    shares is the one thing a probe is after. `open`, `share` and `remix` say
    403 instead, deliberately (Claudette #2477): they are reached from a UI
    that already showed the app, by someone who already holds its id, and a
    403 there tells the person the true reason a button did nothing. The
    line is "what a stranger can aim at a guess": only this read.
    """
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
        # Whether Revert has anything to come back to. Read off the filesystem
        # because that is where the answer is: the house records `live`, the
        # helper rotates the trees, and only the tree knows if there are two.
        has_previous_live=os.path.isdir(
            os.path.join(get_settings().APPS_WWW_ROOT, app["id"], "live.prev")
        ),
        parent_app_id=app["parent_app_id"],
        can_remix=live and configured,
        image_url=None,
        live_url=_app_url(origin, app["id"]) if (live and configured) else None,
    )
