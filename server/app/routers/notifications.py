"""Notifications module (WP7): Web Push endpoints + the bus-driven notifier.

Endpoints:
    GET    /vapid-public-key        (no auth) — {"key": ...}; 503 if unconfigured
    POST   /push/subscribe          (user)    — {endpoint, keys}; upsert on endpoint
    DELETE /push/subscribe          (user)    — {endpoint}; remove own subscription
    GET    /notify-prefs            (user)    — {"notify_all_main": bool}
    PUT    /notify-prefs            (user)    — {"notify_all_main": bool}

Bus notifier (registered via init() from the main.py lifespan, mirroring
ws.init(); never at import time): on `message_create`, decide who gets a Web
Push and fire it off as an asyncio task — message flow is never blocked.

Rules (Architecture §9):
    Candidates  = the channel's USER members (main_feed = all users), minus
                  the author (when the author is a user).
    Suppressed  = recipient is WS-connected AND has that channel focused
                  (ws.manager.is_user_connected + user_focused_channel_ids).
    Eligible    = channel is a DM,
                  OR the content mentions the recipient's username/display
                  name as a word (optionally @-prefixed, case-insensitive),
                  OR (main_feed and the recipient's notify_all_main pref set).
    Notify when: candidate AND NOT suppressed AND eligible.

Privacy: `secret`/`off_the_record` flags gate BOTS, not humans — flagged
messages still push to human members like any other message.

Payload (what the WP11 service worker receives):
    {"title": author display name ("<author> in #<channel>" for main_feed),
     "body": ~120-char snippet, markdown roughly stripped
             ("📎 attachment" for attachment-only messages),
     "channel_id": int, "message_id": int, "url": "/channels/{id}"}
"""

import asyncio
import json
import logging
import re
from typing import Annotated, Any, Optional

from fastapi import APIRouter, Body, Depends, HTTPException
from pydantic import BaseModel, Field

from .. import db, events
from ..config import get_settings
from ..models import User
from ..services import push
from ..ws import manager
from .auth import get_current_user

logger = logging.getLogger(__name__)

router = APIRouter()

CurrentUser = Annotated[User, Depends(get_current_user)]

SNIPPET_LEN = 120


# ---------------------------------------------------------------------------
# Wiring (called from the app lifespan — no import-time subscribers)
# ---------------------------------------------------------------------------

def init() -> None:
    """Subscribe the notifier to the event bus. Idempotent (events.subscribe
    dedupes); tests clear subscribers between apps."""
    events.subscribe(handle_bus_event)


# ---------------------------------------------------------------------------
# REST: VAPID key, subscriptions, prefs
# ---------------------------------------------------------------------------

class SubscribeRequest(BaseModel):
    endpoint: str = Field(min_length=1)
    keys: dict[str, str] = Field(default_factory=dict)  # {"p256dh": ..., "auth": ...}


class UnsubscribeRequest(BaseModel):
    endpoint: str = Field(min_length=1)


class NotifyPrefs(BaseModel):
    notify_all_main: bool


@router.get("/vapid-public-key")
async def vapid_public_key() -> dict[str, str]:
    """Public VAPID key for the client's pushManager.subscribe(). No auth —
    it is public by definition and the client needs it pre-login-ish early."""
    key = get_settings().VAPID_PUBLIC_KEY
    if not key:
        raise HTTPException(
            status_code=503,
            detail=(
                "Web Push is not configured on this server: VAPID_PUBLIC_KEY is "
                "unset. Generate keys with `python cli.py gen-vapid` and set "
                "VAPID_PUBLIC_KEY / VAPID_PRIVATE_KEY in the environment or .env."
            ),
        )
    return {"key": key}


@router.post("/push/subscribe")
async def push_subscribe(body: SubscribeRequest, user: CurrentUser) -> dict[str, bool]:
    """Store (or refresh) a push subscription. Upsert on endpoint: browsers
    rotate keys and users re-login — the newest owner/keys win."""
    await db.execute(
        """INSERT INTO push_subscriptions (user_id, endpoint, keys_json, created_at)
           VALUES (?, ?, ?, ?)
           ON CONFLICT (endpoint)
           DO UPDATE SET user_id = excluded.user_id, keys_json = excluded.keys_json""",
        (user.id, body.endpoint, json.dumps(body.keys), db.utc_now()),
    )
    return {"ok": True}


@router.delete("/push/subscribe")
async def push_unsubscribe(
    user: CurrentUser, body: UnsubscribeRequest = Body(...)
) -> dict[str, bool]:
    """Remove one of the caller's subscriptions (scoped to the caller — you
    cannot unsubscribe someone else's endpoint)."""
    cur = await db.execute(
        "DELETE FROM push_subscriptions WHERE endpoint = ? AND user_id = ?",
        (body.endpoint, user.id),
    )
    return {"ok": True, "removed": cur.rowcount > 0}


@router.get("/notify-prefs")
async def get_notify_prefs(user: CurrentUser) -> NotifyPrefs:
    row = await db.fetch_one(
        "SELECT notify_all_main FROM users WHERE id = ?", (user.id,)
    )
    assert row is not None
    return NotifyPrefs(notify_all_main=bool(row["notify_all_main"]))


@router.put("/notify-prefs")
async def put_notify_prefs(body: NotifyPrefs, user: CurrentUser) -> NotifyPrefs:
    await db.execute(
        "UPDATE users SET notify_all_main = ? WHERE id = ?",
        (int(body.notify_all_main), user.id),
    )
    return body


# ---------------------------------------------------------------------------
# Payload building
# ---------------------------------------------------------------------------

_FENCE_RE = re.compile(r"```.*?(```|$)", re.DOTALL)
_INLINE_CODE_RE = re.compile(r"`([^`]*)`")
_MD_TOKENS_RE = re.compile(r"\*\*|__|~~|[*_]|^#{1,6}\s+|^>\s?", re.MULTILINE)
_WS_RE = re.compile(r"\s+")


def _snippet(content: str) -> str:
    """~120-char body snippet with markdown-ish syntax roughly stripped."""
    text = _FENCE_RE.sub(" [code] ", content)
    text = _INLINE_CODE_RE.sub(r"\1", text)
    text = _MD_TOKENS_RE.sub("", text)
    text = _WS_RE.sub(" ", text).strip()
    if len(text) > SNIPPET_LEN:
        text = text[: SNIPPET_LEN - 1].rstrip() + "…"
    return text


_MENTION_RE_CACHE: dict[str, re.Pattern[str]] = {}


def _mentions(content: str, *names: Optional[str]) -> bool:
    """True if content contains any name as a standalone word, optionally
    @-prefixed, case-insensitive (same semantics as the WS bot-mention rule)."""
    for name in names:
        if not name:
            continue
        pattern = _MENTION_RE_CACHE.get(name)
        if pattern is None:
            pattern = re.compile(rf"(?<!\w)@?{re.escape(name)}(?!\w)", re.IGNORECASE)
            _MENTION_RE_CACHE[name] = pattern
        if pattern.search(content):
            return True
    return False


def _build_payload(channel: dict[str, Any], message: dict[str, Any]) -> dict[str, Any]:
    author = message.get("author") or {}
    author_name = author.get("name") or "Someone"
    if channel["type"] == "main_feed":
        title = f"{author_name} in #{channel['name']}"
    else:
        title = author_name
    body = _snippet(message.get("content") or "")
    if not body and message.get("attachments"):
        body = "📎 attachment"
    return {
        "title": title,
        "body": body,
        "channel_id": channel["id"],
        "message_id": message.get("id"),
        "url": f"/channels/{channel['id']}",
    }


# ---------------------------------------------------------------------------
# Bus subscriber (fire-and-forget — never blocks message flow)
# ---------------------------------------------------------------------------

_pending_tasks: set[asyncio.Task] = set()


def _spawn(coro) -> asyncio.Task:
    """Fire-and-forget with a strong reference (asyncio only weak-refs tasks)."""
    task = asyncio.create_task(coro)
    _pending_tasks.add(task)
    task.add_done_callback(_pending_tasks.discard)
    return task


async def wait_pending() -> None:
    """Await all in-flight notification tasks — used by tests for determinism."""
    while _pending_tasks:
        await asyncio.gather(*list(_pending_tasks), return_exceptions=True)


def handle_bus_event(event: dict[str, Any]) -> None:
    """Bus subscriber: schedule notification processing as a detached task.

    Deliberately sync and returning None so events.publish never awaits it —
    the publisher (message create path) is not blocked by push work.
    """
    if event.get("type") != "message_create":
        return
    _spawn(_process_message_create(event))


async def _process_message_create(event: dict[str, Any]) -> None:
    try:
        recipients, payload = await _plan_notifications(event)
        if recipients:
            await push.notify_users(recipients, payload)
    except Exception:  # noqa: BLE001 — notifier failures must stay contained
        logger.exception("push notifier failed for event %r", event.get("type"))


async def _plan_notifications(
    event: dict[str, Any],
) -> tuple[list[int], dict[str, Any]]:
    """Compute (recipient user ids, payload) for a message_create event."""
    channel_id = event.get("channel_id")
    message = event.get("message") or {}
    if channel_id is None or not message:
        return [], {}
    channel = await db.fetch_one(
        "SELECT id, type, name FROM channels WHERE id = ?", (channel_id,)
    )
    if channel is None:
        return [], {}

    is_main = channel["type"] == "main_feed"
    is_dm = channel["type"] == "dm_1to1"

    # Candidates: the channel's USER members. main_feed = every user (implicit
    # membership); DMs from explicit rows. Bots never receive pushes.
    if is_main:
        candidates = await db.fetch_all(
            "SELECT id, username, display_name, notify_all_main FROM users"
        )
    else:
        candidates = await db.fetch_all(
            """SELECT u.id, u.username, u.display_name, u.notify_all_main
               FROM channel_members cm JOIN users u ON u.id = cm.member_id
               WHERE cm.channel_id = ? AND cm.member_type = 'user'""",
            (channel_id,),
        )

    author_type = message.get("author_type")
    author_id = message.get("author_id")
    content = message.get("content") or ""

    recipients: list[int] = []
    for u in candidates:
        if author_type == "user" and u["id"] == author_id:
            continue  # never notify the author
        # Suppression: connected AND focused on this channel = actively reading.
        if manager.is_user_connected(u["id"]) and channel_id in manager.user_focused_channel_ids(u["id"]):
            continue
        eligible = (
            is_dm
            or _mentions(content, u["username"], u["display_name"])
            or (is_main and bool(u["notify_all_main"]))
        )
        if eligible:
            recipients.append(u["id"])

    if not recipients:
        return [], {}
    return recipients, _build_payload(channel, message)
