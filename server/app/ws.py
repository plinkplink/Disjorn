"""WebSocket hub (WP5): single realtime endpoint for humans and bots.

Endpoint: GET /ws (websocket).

Auth:
    Humans — `disjorn_session` cookie on the handshake (WP2 session semantics,
             sliding expiry refresh included).
    Bots   — first frame `{"op": "auth", "api_key": "..."}` within AUTH_TIMEOUT
             seconds.
    Anything else -> close 4401.
    On success the server sends `{"type": "ready", "user_id"|"bot_id": N}`.

Client -> server ops (JSON frames; invalid/unknown frames are ignored):
    {"op": "auth", "api_key": str}          bots only, first frame
    {"op": "typing", "channel_id": N}       users + bots; membership-checked;
                                            rate-limited to 1 per 3s per
                                            (sender, channel)
    {"op": "status", "status": "online"|"idle"|"dnd"}
                                            users only; persists users.status
                                            and broadcasts presence
    {"op": "focus", "channel_id": N|null}   users only; tracked per connection
                                            for WP7 notification suppression

Server -> client frames (Architecture §8.2; ephemeral events carry no seq):
    {"type": "message_create"|"message_edit", "channel_id", "seq", "message"}
    {"type": "message_delete", "channel_id", "id", "seq"}
    {"type": "typing_start", "channel_id", "author_type", "author_id"}
    {"type": "presence", "user_id", "status"}

Fan-out (bus subscriber, registered idempotently by init() from the app
lifespan): message events go to connected users who are members of the channel
(main_feed = every user) and to connected bots that are EXPLICIT members
(bot_channel_ids semantics), after privacy.filter_event_for_bot — a
secret/off_the_record message reaches no bot in any form, not even the
tombstone of its deletion. When a message_create mentions a receiving bot
(`@name` or its name as a word, case-insensitive), that bot's copy — and only
that bot's — gets a "context" block (Architecture §8.3).

Exports for WP7: `manager` (ConnectionManager singleton) with
`is_user_connected(user_id)` and `user_focused_channel_ids(user_id)`.
"""

import asyncio
import json
import logging
import re
import time
from typing import Any, Optional

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from . import db, events, privacy
from .routers.auth import COOKIE_NAME, _bot_for_key, _user_for_token
from .routers.channels import is_member

logger = logging.getLogger(__name__)

router = APIRouter()

AUTH_TIMEOUT = 5.0          # seconds for a bot's first auth frame
TYPING_INTERVAL = 3.0       # min seconds between typing_start per sender+channel
CLOSE_UNAUTHENTICATED = 4401

_MESSAGE_EVENT_TYPES = ("message_create", "message_edit", "message_delete")


# ---------------------------------------------------------------------------
# Connection manager (module-level singleton; exported for WP7)
# ---------------------------------------------------------------------------

class ConnectionManager:
    """Tracks live sockets, presence, per-connection focus, typing rate limits.

    Multi-device is fine: a user (or bot) may hold several connections; presence
    transitions fire only on first-connect / last-disconnect.
    """

    def __init__(self) -> None:
        self.user_conns: dict[int, set[WebSocket]] = {}
        self.bot_conns: dict[int, set[WebSocket]] = {}
        self.user_status: dict[int, str] = {}   # effective status while connected
        self._focus: dict[Any, Optional[int]] = {}      # per user connection
        self._typing_last: dict[tuple[str, int, int], float] = {}

    def reset(self) -> None:
        """Drop all state — called on app startup (and by tests)."""
        self.user_conns.clear()
        self.bot_conns.clear()
        self.user_status.clear()
        self._focus.clear()
        self._typing_last.clear()

    # -- connect / disconnect ------------------------------------------------

    def connect_user(self, ws: WebSocket, user_id: int, status: str) -> bool:
        """Register a user connection. Returns True if this is their first."""
        conns = self.user_conns.setdefault(user_id, set())
        first = not conns
        conns.add(ws)
        self.user_status[user_id] = status
        self._focus[ws] = None
        return first

    def disconnect_user(self, ws: WebSocket, user_id: int) -> bool:
        """Unregister a user connection. Returns True if it was their last."""
        self._focus.pop(ws, None)
        conns = self.user_conns.get(user_id)
        if conns is None:
            return False
        conns.discard(ws)
        if conns:
            return False
        del self.user_conns[user_id]
        self.user_status.pop(user_id, None)
        return True

    def connect_bot(self, ws: WebSocket, bot_id: int) -> None:
        self.bot_conns.setdefault(bot_id, set()).add(ws)

    def disconnect_bot(self, ws: WebSocket, bot_id: int) -> None:
        conns = self.bot_conns.get(bot_id)
        if conns is None:
            return
        conns.discard(ws)
        if not conns:
            del self.bot_conns[bot_id]

    # -- queries -------------------------------------------------------------

    def is_user_connected(self, user_id: int) -> bool:
        """WP7 export: does the user have any live WS connection?"""
        return bool(self.user_conns.get(user_id))

    def user_focused_channel_ids(self, user_id: int) -> set[int]:
        """WP7 export: channels any of the user's connections has focused."""
        return {
            cid
            for ws in self.user_conns.get(user_id, ())
            if (cid := self._focus.get(ws)) is not None
        }

    def connected_user_ids(self) -> list[int]:
        return list(self.user_conns)

    def connected_bot_ids(self) -> list[int]:
        return list(self.bot_conns)

    def user_sockets(self, user_id: int) -> list[WebSocket]:
        return list(self.user_conns.get(user_id, ()))

    def bot_sockets(self, bot_id: int) -> list[WebSocket]:
        return list(self.bot_conns.get(bot_id, ()))

    def all_sockets(self) -> list[WebSocket]:
        out: list[WebSocket] = []
        for conns in self.user_conns.values():
            out.extend(conns)
        for conns in self.bot_conns.values():
            out.extend(conns)
        return out

    # -- focus / typing ------------------------------------------------------

    def set_focus(self, ws: WebSocket, channel_id: Optional[int]) -> None:
        if ws in self._focus:
            self._focus[ws] = channel_id

    def typing_allowed(self, sender_type: str, sender_id: int, channel_id: int) -> bool:
        """Rate limit: at most one typing_start per sender per channel per 3s."""
        key = (sender_type, sender_id, channel_id)
        now = time.monotonic()
        last = self._typing_last.get(key)
        if last is not None and now - last < TYPING_INTERVAL:
            return False
        self._typing_last[key] = now
        return True


manager = ConnectionManager()


def init() -> None:
    """Wire the hub: reset connection state, (re-)subscribe to the event bus.

    Called from the app lifespan on every startup. events.subscribe dedupes,
    and tests clear subscribers between apps, so this is idempotent per run
    and fresh per test app.
    """
    manager.reset()
    events.subscribe(handle_bus_event)


# ---------------------------------------------------------------------------
# Sending
# ---------------------------------------------------------------------------

async def _send(ws: WebSocket, frame: dict[str, Any]) -> None:
    """Best-effort send; a dead socket never breaks fan-out (its own receive
    loop notices the disconnect and cleans up)."""
    try:
        await ws.send_json(frame)
    except Exception:  # noqa: BLE001 — socket already closing/closed
        logger.debug("ws send failed (socket closing?)", exc_info=True)


async def _broadcast_presence(user_id: int, status: str) -> None:
    """Presence goes to everyone — all users and all bots (coarse; no seq)."""
    frame = {"type": "presence", "user_id": user_id, "status": status}
    for ws in manager.all_sockets():
        await _send(ws, frame)


async def _broadcast_typing(sender_type: str, sender_id: int, channel_id: int) -> None:
    """typing_start to the channel's connected members, excluding the sender."""
    frame = {
        "type": "typing_start",
        "channel_id": channel_id,
        "author_type": sender_type,
        "author_id": sender_id,
    }
    for uid in manager.connected_user_ids():
        if sender_type == "user" and uid == sender_id:
            continue
        if not await is_member(channel_id, "user", uid):
            continue
        for ws in manager.user_sockets(uid):
            await _send(ws, frame)
    for bot_id in manager.connected_bot_ids():
        if sender_type == "bot" and bot_id == sender_id:
            continue
        if not await is_member(channel_id, "bot", bot_id):
            continue
        for ws in manager.bot_sockets(bot_id):
            await _send(ws, frame)


# ---------------------------------------------------------------------------
# Bus subscriber: message-event fan-out
# ---------------------------------------------------------------------------

def _frame_for_event(event: dict[str, Any]) -> dict[str, Any]:
    """Bus event -> outbound frame (§8.2). Persisted events carry seq."""
    etype = event["type"]
    if etype == "message_delete":
        return {
            "type": etype,
            "channel_id": event["channel_id"],
            "id": event["id"],
            "seq": event["seq"],
        }
    message = event["message"]
    return {
        "type": etype,
        "channel_id": event["channel_id"],
        "seq": message.get("seq"),
        "message": message,
    }


_MENTION_RE_CACHE: dict[str, re.Pattern[str]] = {}


def _mentions_bot(content: str, bot_name: str) -> bool:
    """`@name` or the bot's name as a standalone word, case-insensitive."""
    pattern = _MENTION_RE_CACHE.get(bot_name)
    if pattern is None:
        pattern = re.compile(rf"(?<!\w)@?{re.escape(bot_name)}(?!\w)", re.IGNORECASE)
        _MENTION_RE_CACHE[bot_name] = pattern
    return pattern.search(content) is not None


async def _context_block(
    channel: dict[str, Any], message: dict[str, Any]
) -> dict[str, Any]:
    """Structured context injection (§8.3), from already-filtered data."""
    awake_users = []
    connected = sorted(manager.connected_user_ids())
    if connected:
        placeholders = ",".join("?" * len(connected))
        names = {
            r["id"]: r["display_name"]
            for r in await db.fetch_all(
                f"SELECT id, display_name FROM users WHERE id IN ({placeholders})",
                connected,
            )
        }
        for uid in connected:
            status = manager.user_status.get(uid, "online")
            if status == "offline":
                continue
            awake_users.append(
                {"id": uid, "name": names.get(uid, f"user-{uid}"), "status": status}
            )
    return {
        "awake_users": awake_users,
        "channel_state": {"name": channel["name"]},
        "privacy_flags_on_current_message": message.get("privacy_flags") or {},
    }


async def handle_bus_event(event: dict[str, Any]) -> None:
    """Bus subscriber: fan message events out to connected users and bots."""
    if event.get("type") not in _MESSAGE_EVENT_TYPES:
        return
    channel_id = event.get("channel_id")
    if channel_id is None:
        return
    channel = await db.fetch_one(
        "SELECT id, type, name FROM channels WHERE id = ?", (channel_id,)
    )
    if channel is None:
        return

    frame = _frame_for_event(event)

    # Users: channel members only (main_feed = every user).
    for uid in manager.connected_user_ids():
        if channel["type"] != "main_feed" and not await is_member(
            channel_id, "user", uid
        ):
            continue
        for ws in manager.user_sockets(uid):
            await _send(ws, frame)

    # Bots: explicit members only, privacy-filtered. message_delete carries no
    # payload, so enrich it with the (soft-deleted) row's flags — the delete of
    # a hidden message must be hidden too.
    bot_event = event
    if event["type"] == "message_delete":
        row = await db.fetch_one(
            "SELECT privacy_flags FROM messages WHERE id = ?", (event["id"],)
        )
        if row is not None:
            bot_event = {
                **event,
                "privacy_flags": json.loads(row["privacy_flags"] or "{}"),
            }

    connected_bots = manager.connected_bot_ids()
    if not connected_bots:
        return
    bot_names: dict[int, str] = {}
    if event["type"] == "message_create":
        placeholders = ",".join("?" * len(connected_bots))
        bot_names = {
            r["id"]: r["name"]
            for r in await db.fetch_all(
                f"SELECT id, name FROM bots WHERE id IN ({placeholders})",
                connected_bots,
            )
        }
    for bot_id in connected_bots:
        if not await is_member(channel_id, "bot", bot_id):
            continue
        filtered = privacy.filter_event_for_bot(bot_event)
        if filtered is None:
            continue
        bot_frame = _frame_for_event(filtered)
        if filtered["type"] == "message_create":
            name = bot_names.get(bot_id)
            message = filtered["message"]
            if name and _mentions_bot(message.get("content") or "", name):
                bot_frame = {
                    **bot_frame,
                    "context": await _context_block(channel, message),
                }
        for ws in manager.bot_sockets(bot_id):
            await _send(ws, bot_frame)


# ---------------------------------------------------------------------------
# Client op handling
# ---------------------------------------------------------------------------

_STATUSES = ("online", "idle", "dnd")


async def _handle_typing(sender_type: str, sender_id: int, data: dict[str, Any]) -> None:
    channel_id = data.get("channel_id")
    if not isinstance(channel_id, int):
        return
    if not await is_member(channel_id, sender_type, sender_id):
        return
    if not manager.typing_allowed(sender_type, sender_id, channel_id):
        return
    await _broadcast_typing(sender_type, sender_id, channel_id)


async def _handle_user_op(ws: WebSocket, user_id: int, data: dict[str, Any]) -> None:
    op = data.get("op")
    if op == "typing":
        await _handle_typing("user", user_id, data)
    elif op == "status":
        status = data.get("status")
        if status not in _STATUSES:
            return
        await db.execute("UPDATE users SET status = ? WHERE id = ?", (status, user_id))
        manager.user_status[user_id] = status
        await _broadcast_presence(user_id, status)
    elif op == "focus":
        channel_id = data.get("channel_id")
        if channel_id is None or isinstance(channel_id, int):
            manager.set_focus(ws, channel_id)
    # unknown ops: ignored


async def _handle_bot_op(bot_id: int, data: dict[str, Any]) -> None:
    if data.get("op") == "typing":
        await _handle_typing("bot", bot_id, data)
    # bots have no status/focus; unknown ops ignored


async def _receive_op(ws: WebSocket) -> Optional[dict[str, Any]]:
    """Next JSON object frame; None for frames that aren't a JSON object."""
    raw = await ws.receive_text()
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return None
    return data if isinstance(data, dict) else None


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------

async def _run_user(ws: WebSocket, user_id: int, stored_status: str) -> None:
    # Connect -> online, unless the user's stored status is idle/dnd.
    status = stored_status if stored_status in ("idle", "dnd") else "online"
    first = manager.connect_user(ws, user_id, status)
    await _send(ws, {"type": "ready", "user_id": user_id})
    if first:
        await _broadcast_presence(user_id, status)
    try:
        while True:
            data = await _receive_op(ws)
            if data is not None:
                await _handle_user_op(ws, user_id, data)
    except WebSocketDisconnect:
        pass
    finally:
        if manager.disconnect_user(ws, user_id):
            await _broadcast_presence(user_id, "offline")


async def _run_bot(ws: WebSocket, bot_id: int) -> None:
    manager.connect_bot(ws, bot_id)
    await _send(ws, {"type": "ready", "bot_id": bot_id})
    try:
        while True:
            data = await _receive_op(ws)
            if data is not None:
                await _handle_bot_op(bot_id, data)
    except WebSocketDisconnect:
        pass
    finally:
        manager.disconnect_bot(ws, bot_id)


@router.websocket("/ws")
async def ws_endpoint(websocket: WebSocket) -> None:
    await websocket.accept()

    # Humans: session cookie on the handshake (sliding-expiry refresh included).
    user = await _user_for_token(websocket.cookies.get(COOKIE_NAME))
    if user is not None:
        await _run_user(websocket, user.id, user.status)
        return

    # Bots: first frame {"op": "auth", "api_key": ...} within AUTH_TIMEOUT.
    try:
        raw = await asyncio.wait_for(websocket.receive_text(), timeout=AUTH_TIMEOUT)
        data = json.loads(raw)
    except (asyncio.TimeoutError, json.JSONDecodeError, ValueError, WebSocketDisconnect):
        await websocket.close(code=CLOSE_UNAUTHENTICATED)
        return
    bot = None
    if isinstance(data, dict) and data.get("op") == "auth":
        bot = await _bot_for_key(data.get("api_key"))
    if bot is None:
        await websocket.close(code=CLOSE_UNAUTHENTICATED)
        return
    await _run_bot(websocket, bot.id)
