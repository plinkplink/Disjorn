"""DisjornClient: WS event stream + REST posting for Disjorn bots.

Protocol truth: server/app/ws.py (WS), server/app/routers/messages.py (REST),
Architecture.md §8. Highlights the SDK builds on:

- WS auth: first frame ``{"op": "auth", "api_key": ...}`` within 5s, then the
  server answers ``{"type": "ready", "bot_id": N}``. Bad key -> close 4401.
- REST auth: ``X-Api-Key`` header.
- Persisted events carry per-channel ``seq``; the client tracks
  ``last_seen_seq`` per channel and, on reconnect, REST-backfills each known
  channel with ``from_seq=last+1``. Backfill is *current state*, not event
  replay: edits arrive already applied inside synthetic MessageCreate events,
  and deleted messages appear as tombstones which the SDK silently skips
  (their seq still advances the cursor).
- The server never sends a bot anything flagged secret/off_the_record, in the
  live stream or in backfill — nothing to filter client-side.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
from typing import Any, AsyncIterator, Awaitable, Callable, Optional

import httpx
import websockets
from websockets.asyncio.client import ClientConnection

from .events import (
    ChannelCreate,
    Event,
    MessageCreate,
    MessageDelete,
    MessageEdit,
    Presence,
    Ready,
    TypingStart,
)

__all__ = ["DisjornClient", "DisjornError", "DisjornAuthError"]

logger = logging.getLogger("disjorn_sdk")

_AUTH_CLOSE_CODE = 4401
_READY_TIMEOUT = 10.0
_BACKFILL_PAGE = 200  # server max for GET .../messages limit


class DisjornError(Exception):
    """Base class for SDK errors."""


class DisjornAuthError(DisjornError):
    """The server rejected the API key (WS close 4401 / REST 401).

    Fatal: the reconnect loop does not retry on this."""


class DisjornClient:
    """Async client for one Disjorn bot.

    Usage::

        client = DisjornClient("http://localhost:8000", api_key="...")
        async for event in client.events():
            ...

    or the convenience runner::

        await client.run(handler)   # handler(event) awaited per event

    Attributes:
        bot_id: the bot's id, set once the first ``ready`` frame arrives.
        ws: the live WebSocket connection (None while disconnected). Exposed
            mainly so tests/operators can force a reconnect via
            ``await client.ws.close()``.
        last_seen_seq: per-channel high-water mark of persisted events seen.
            Persist and re-seed it (:meth:`seed_seq`) to resume across process
            restarts without losing messages.
    """

    def __init__(
        self,
        base_url: str,
        api_key: str,
        *,
        backoff_initial: float = 1.0,
        backoff_max: float = 30.0,
        http_timeout: float = 30.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.bot_id: Optional[int] = None
        self.ws: Optional[ClientConnection] = None
        self.last_seen_seq: dict[int, int] = {}
        self._backoff_initial = backoff_initial
        self._backoff_max = backoff_max
        self._closed = False
        self._http = httpx.AsyncClient(
            base_url=self.base_url,
            headers={"X-Api-Key": api_key},
            timeout=http_timeout,
        )

    # ------------------------------------------------------------------ REST

    async def send(
        self,
        channel_id: int,
        content: str,
        *,
        reply_to: Optional[int] = None,
        emotion: Optional[str] = None,
        emote_refs: Optional[list[Any]] = None,
        privacy_flags: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        """Post a message; returns the server's full materialized message dict.

        ``emotion`` is resolved server-side against the bot's chibi pack into
        ``emote_refs``; unresolvable emotions are silently ignored by the
        server. ``reply_to`` maps to the protocol's ``reply_to_id`` and must
        reference a message in the same channel.
        """
        payload: dict[str, Any] = {"content": content}
        if reply_to is not None:
            payload["reply_to_id"] = reply_to
        if emotion is not None:
            payload["emotion"] = emotion
        if emote_refs is not None:
            payload["emote_refs"] = emote_refs
        if privacy_flags is not None:
            payload["privacy_flags"] = privacy_flags
        resp = await self._http.post(f"/channels/{channel_id}/messages", json=payload)
        self._raise_for_status(resp)
        return resp.json()

    async def get_messages(
        self,
        channel_id: int,
        *,
        from_seq: Optional[int] = None,
        before_seq: Optional[int] = None,
        limit: Optional[int] = None,
    ) -> list[dict[str, Any]]:
        """Fetch messages.

        ``from_seq``: backfill mode — ascending, current-state (edits applied),
        deleted messages as tombstones ``{id, seq, deleted: true}``.
        ``before_seq``: scrollback mode — newest-first, deleted omitted.
        The two are mutually exclusive. Messages hidden from bots by privacy
        flags are entirely absent (no tombstone).
        """
        params: dict[str, Any] = {}
        if from_seq is not None:
            params["from_seq"] = from_seq
        if before_seq is not None:
            params["before_seq"] = before_seq
        if limit is not None:
            params["limit"] = limit
        resp = await self._http.get(f"/channels/{channel_id}/messages", params=params)
        self._raise_for_status(resp)
        return resp.json()

    async def search(
        self,
        q: str,
        *,
        after: Optional[str] = None,
        before: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> list[dict[str, Any]]:
        """Full-text search across the bot's member channels.

        Returns ``[{message: <full payload dict>, channel: {id, type, name}}]``,
        newest first. Scoping and privacy are server-enforced: only channels
        the bot is an explicit member of are searched, and messages flagged
        secret/off_the_record never appear — nothing to filter client-side.

        ``after``/``before`` are optional ISO-8601 bounds on ``created_at``
        (date-only like ``"2026-07-01"`` or full timestamps): ``after`` is
        inclusive, ``before`` exclusive; the server 400s on malformed dates.
        ``limit`` caps the result count (server default and max: 50).
        """
        params: dict[str, Any] = {"q": q}
        if after is not None:
            params["after"] = after
        if before is not None:
            params["before"] = before
        if limit is not None:
            params["limit"] = limit
        resp = await self._http.get("/search", params=params)
        self._raise_for_status(resp)
        return resp.json()

    async def backlog(self) -> list[dict[str, Any]]:
        """Read the feature-request backlog (WP-L2), oldest first.

        Returns ``[{id, text, author, created_at, status, spec_ref}, ...]`` —
        the same items users file with ``/backlog <text>`` in chat. This is the
        resident's triage read surface: get the table straight from the server
        instead of scraping the server-rendered ``/backlog`` chat listing.
        ``status`` is one of ``open``/``spec'd``/``built``/``rejected``;
        ``spec_ref`` is null until an item is triaged into a spec.
        """
        resp = await self._http.get("/backlog")
        self._raise_for_status(resp)
        return resp.json()

    async def members(self, channel_id: int) -> list[dict[str, Any]]:
        """Member listing: ``[{type: "user"|"bot", id, name, status?}, ...]``.

        This is the bot's channel-discovery primitive — ``GET /channels`` is
        user-only; bots learn channels from events and membership."""
        resp = await self._http.get(f"/channels/{channel_id}/members")
        self._raise_for_status(resp)
        return resp.json()

    # -------------------------------------------------------------- WS ops

    async def typing(self, channel_id: int) -> None:
        """Send a typing indicator (WS op; server rate-limits to 1/3s).

        Requires a live connection (i.e. an active :meth:`events` iterator)."""
        ws = self.ws
        if ws is None:
            raise DisjornError("typing() requires a live WS connection (run events())")
        await ws.send(json.dumps({"op": "typing", "channel_id": channel_id}))

    # ------------------------------------------------------------ lifecycle

    def seed_seq(self, channel_id: int, seq: int) -> None:
        """Pre-register a channel cursor so the next (re)connect backfills it.

        Use this to resume across process restarts: persist ``last_seen_seq``
        on shutdown, re-seed on boot, and the first *re*connect backfill will
        cover the gap. (The first connect of a fresh client does not backfill —
        call :meth:`get_messages` yourself for boot-time catch-up, or seed and
        rely on the stream after the first reconnect.)"""
        self.last_seen_seq[channel_id] = max(self.last_seen_seq.get(channel_id, 0), seq)

    async def aclose(self) -> None:
        """Graceful shutdown: stops the events loop and closes HTTP + WS."""
        self._closed = True
        ws, self.ws = self.ws, None
        if ws is not None:
            try:
                await ws.close()
            except Exception:  # noqa: BLE001 — already dying
                logger.debug("ws close during aclose failed", exc_info=True)
        await self._http.aclose()

    # ---------------------------------------------------------- event loop

    async def events(self) -> AsyncIterator[Event]:
        """Connect, authenticate, and yield typed events forever.

        Auto-reconnects with exponential backoff (``backoff_initial`` doubling
        to ``backoff_max``, ±25% jitter). Each successful (re)connect yields
        :class:`Ready`; after a *re*connect, every channel with a known cursor
        is REST-backfilled from ``last_seen_seq + 1`` and missed messages are
        yielded as synthetic ``MessageCreate(backfilled=True)`` events, in seq
        order per channel, before live frames resume. Live ``message_create``
        frames at-or-below the cursor (already covered by backfill) are
        deduplicated away. Exits when :meth:`aclose` is called; raises
        :class:`DisjornAuthError` on a rejected API key."""
        backoff = self._backoff_initial
        connected_before = False
        while not self._closed:
            try:
                ws = await websockets.connect(self._ws_url, open_timeout=10)
            except Exception as exc:  # noqa: BLE001 — connect failures retry
                if self._closed:
                    break
                logger.warning("WS connect to %s failed: %r", self._ws_url, exc)
                await self._sleep_backoff(backoff)
                backoff = min(backoff * 2, self._backoff_max)
                continue
            self.ws = ws
            try:
                await ws.send(json.dumps({"op": "auth", "api_key": self.api_key}))
                ready = json.loads(
                    await asyncio.wait_for(ws.recv(), timeout=_READY_TIMEOUT)
                )
                if not isinstance(ready, dict) or "bot_id" not in ready:
                    raise DisjornError(f"unexpected frame instead of ready: {ready!r}")
                self.bot_id = int(ready["bot_id"])
                backoff = self._backoff_initial
                logger.info(
                    "%s as bot %s", "reconnected" if connected_before else "connected",
                    self.bot_id,
                )
                yield Ready(bot_id=self.bot_id, reconnected=connected_before)
                if connected_before and self.last_seen_seq:
                    async for event in self._backfill():
                        yield event
                connected_before = True
                while True:
                    frame = json.loads(await ws.recv())
                    event = self._parse_frame(frame)
                    if event is not None:
                        yield event
            except websockets.ConnectionClosed as exc:
                code = exc.rcvd.code if exc.rcvd is not None else None
                if code == _AUTH_CLOSE_CODE:
                    raise DisjornAuthError("server rejected API key (close 4401)") from exc
                if not self._closed:
                    logger.warning("WS connection lost (%s); reconnecting", code)
            except (DisjornAuthError, asyncio.CancelledError, GeneratorExit):
                raise
            except Exception:  # noqa: BLE001 — transient; log and reconnect
                if self._closed:
                    break
                logger.exception("WS loop error; reconnecting")
            finally:
                self.ws = None
                try:
                    await ws.close()
                except Exception:  # noqa: BLE001
                    logger.debug("ws close failed", exc_info=True)
            if self._closed:
                break
            await self._sleep_backoff(backoff)
            backoff = min(backoff * 2, self._backoff_max)

    async def run(
        self, handler: Callable[[Event], Awaitable[None]]
    ) -> None:
        """Convenience runner: ``await handler(event)`` per event, with error
        isolation — a handler exception is logged and the loop continues."""
        async for event in self.events():
            try:
                await handler(event)
            except Exception:  # noqa: BLE001 — one bad event never kills the bot
                logger.exception("handler failed on %s", type(event).__name__)

    # ------------------------------------------------------------ internals

    @property
    def _ws_url(self) -> str:
        if self.base_url.startswith("https://"):
            return "wss://" + self.base_url[len("https://"):] + "/ws"
        if self.base_url.startswith("http://"):
            return "ws://" + self.base_url[len("http://"):] + "/ws"
        return self.base_url + "/ws"  # already a ws:// / wss:// URL

    async def _sleep_backoff(self, backoff: float) -> None:
        delay = backoff * random.uniform(0.75, 1.25)
        logger.debug("reconnect in %.1fs", delay)
        await asyncio.sleep(delay)

    async def _backfill(self) -> AsyncIterator[MessageCreate]:
        """Yield missed messages for every known channel, current-state."""
        for channel_id in sorted(self.last_seen_seq):
            from_seq = self.last_seen_seq[channel_id] + 1
            while True:
                try:
                    page = await self.get_messages(
                        channel_id, from_seq=from_seq, limit=_BACKFILL_PAGE
                    )
                except httpx.HTTPError:
                    # Channel gone / membership revoked / server hiccup: skip,
                    # the cursor stays put and the next reconnect retries.
                    logger.warning("backfill failed for channel %s", channel_id,
                                   exc_info=True)
                    break
                for item in page:
                    seq = item["seq"]
                    self.last_seen_seq[channel_id] = max(
                        self.last_seen_seq[channel_id], seq
                    )
                    from_seq = seq + 1
                    if item.get("deleted"):
                        continue  # tombstone: the message is gone; skip
                    logger.debug("backfill: channel %s seq %s", channel_id, seq)
                    yield MessageCreate(
                        channel_id=channel_id, seq=seq, message=item, backfilled=True
                    )
                if len(page) < _BACKFILL_PAGE:
                    break

    def _parse_frame(self, frame: Any) -> Optional[Event]:
        """Server frame -> typed event; None for unknown/duplicate frames."""
        if not isinstance(frame, dict):
            return None
        ftype = frame.get("type")
        if ftype == "message_create":
            channel_id, seq = frame["channel_id"], frame["seq"]
            if seq is not None and seq <= self.last_seen_seq.get(channel_id, 0):
                return None  # already delivered by a backfill pass
            self._advance(channel_id, seq)
            return MessageCreate(
                channel_id=channel_id,
                seq=seq,
                message=frame["message"],
                context=frame.get("context"),
            )
        if ftype == "message_edit":
            channel_id, seq = frame["channel_id"], frame["seq"]
            self._advance(channel_id, seq)
            return MessageEdit(channel_id=channel_id, seq=seq, message=frame["message"])
        if ftype == "message_delete":
            channel_id, seq = frame["channel_id"], frame["seq"]
            self._advance(channel_id, seq)
            return MessageDelete(channel_id=channel_id, id=frame["id"], seq=seq)
        if ftype == "typing_start":
            return TypingStart(
                channel_id=frame["channel_id"],
                author_type=frame["author_type"],
                author_id=frame["author_id"],
            )
        if ftype == "presence":
            return Presence(user_id=frame["user_id"], status=frame["status"])
        if ftype == "channel_create":
            return ChannelCreate(channel=frame["channel"])
        logger.debug("ignoring unknown frame type %r", ftype)
        return None

    def _advance(self, channel_id: int, seq: Optional[int]) -> None:
        if seq is not None:
            self.last_seen_seq[channel_id] = max(
                self.last_seen_seq.get(channel_id, 0), seq
            )

    def _raise_for_status(self, resp: httpx.Response) -> None:
        if resp.status_code == 401:
            raise DisjornAuthError("server rejected API key (HTTP 401)")
        resp.raise_for_status()
