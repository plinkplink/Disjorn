"""Integration tests: disjorn_sdk against a live scratch server (see conftest).

Covers the WP13 gate list: connect/ready, user post -> bot event, bot send ->
user REST fetch, mention context injection, secret-flag exclusion, reconnect
backfill (order + edit-as-current-state), DM exclusion, typing op.

Ordering-based negative checks: instead of "bot receives nothing for N
seconds", we post a marker message after the must-not-arrive one and assert
the *next* MessageCreate the bot sees is the marker (seq order makes this
airtight).
"""

from __future__ import annotations

import asyncio
import base64
import gc
import json
import logging
import sqlite3
from functools import partial
from types import SimpleNamespace
from typing import Any, AsyncIterator, Callable

import httpx
import pytest
import websockets

from disjorn_sdk import client as sdk_client
from disjorn_sdk import (
    ChannelCreate,
    DisjornAuthError,
    DisjornClient,
    Event,
    MessageCreate,
    MessageDelete,
    MessageEdit,
    Presence,
    Ready,
)

pytestmark = pytest.mark.integration

EVENT_TIMEOUT = 10.0

# Smallest valid PNG (1x1, opaque) — keeps the upload test free of Pillow.
PNG_1PX = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------

async def _login(server, username: str) -> httpx.AsyncClient:
    client = httpx.AsyncClient(
        base_url=server.base_url, headers={"Origin": server.origin}, timeout=10
    )
    resp = await client.post(
        "/auth/login", json={"username": username, "password": server.users[username]}
    )
    assert resp.status_code == 200, resp.text
    # httpx withholds a Secure cookie over plain http, so the client carries it
    # the way the TLS terminator in front of the house would deliver it.
    cookie = server.session_cookie
    client.headers["Cookie"] = f"{cookie}={client.cookies[cookie]}"
    return client


async def _next_matching(
    agen: AsyncIterator[Event],
    pred: Callable[[Event], bool],
    timeout: float = EVENT_TIMEOUT,
) -> Event:
    """Next event matching pred; skips others (presence/typing chatter)."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while True:
        remaining = deadline - loop.time()
        assert remaining > 0, "timed out waiting for a matching event"
        event = await asyncio.wait_for(anext(agen), remaining)
        if pred(event):
            return event


def _is_create(content: str) -> Callable[[Event], bool]:
    return lambda e: isinstance(e, MessageCreate) and e.message.get("content") == content


@pytest.fixture
async def alice(server):
    client = await _login(server, "alice")
    yield client
    await client.aclose()


@pytest.fixture
async def main_id(alice) -> int:
    resp = await alice.get("/channels")
    resp.raise_for_status()
    return next(c["id"] for c in resp.json() if c["type"] == "main_feed")


@pytest.fixture
async def bot(server):
    client = DisjornClient(server.base_url, api_key=server.api_key, backoff_initial=0.2)
    yield client
    await client.aclose()


@pytest.fixture
async def bot_stream(bot):
    """(client, event iterator) — connected, Ready already consumed."""
    agen = bot.events()
    ready = await asyncio.wait_for(anext(agen), EVENT_TIMEOUT)
    assert isinstance(ready, Ready) and not ready.reconnected
    assert bot.bot_id == ready.bot_id > 0
    yield bot, agen
    await bot.aclose()
    await agen.aclose()


async def _user_ws(server, http_client: httpx.AsyncClient):
    """User-authenticated WS (cookie on the handshake); ready frame consumed."""
    ws = await websockets.connect(
        server.base_url.replace("http://", "ws://") + "/ws",
        origin=server.origin,
        additional_headers={"Cookie": http_client.headers["Cookie"]},
    )
    ready = json.loads(await asyncio.wait_for(ws.recv(), EVENT_TIMEOUT))
    assert ready.get("type") == "ready"
    return ws


async def _post(user: httpx.AsyncClient, channel_id: int, content: str, **extra: Any) -> dict:
    resp = await user.post(
        f"/channels/{channel_id}/messages", json={"content": content, **extra}
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

async def test_connect_ready(bot_stream):
    bot, _ = bot_stream
    assert isinstance(bot.bot_id, int) and bot.bot_id > 0


async def test_backlog_read(server, alice, main_id, bot):
    """A user files a request via /backlog in chat; the bot reads the table
    through the SDK (WP-L2 resident triage read surface)."""
    text = "SDK-filed backlog item xyzzy"
    await _post(alice, main_id, f"/backlog {text}")

    items = await bot.backlog()
    match = next((it for it in items if it["text"] == text), None)
    assert match is not None, f"filed item not found in {items!r}"
    assert match["author"] == "alice"
    assert match["status"] == "open"
    assert match["spec_ref"] is None
    assert set(match) >= {"id", "text", "author", "created_at", "status", "spec_ref"}


async def test_backlog_pagination(server, alice, main_id, bot):
    """BL-D6: GET /backlog is paginated; backlog() walks the pages by default.

    The /backlog chat command is rate limited, so seed through the API surface
    the SDK actually reads rather than through 60 chat messages."""
    baseline = await bot.backlog()
    for i in range(5):
        await _post(alice, main_id, f"/backlog paging probe {i}")

    all_items = await bot.backlog()
    assert len(all_items) == len(baseline) + 5
    assert [it["id"] for it in all_items] == sorted(it["id"] for it in all_items)

    # Single page + cursor resume reconstructs the same list.
    page = await bot.backlog(limit=2, all_pages=False)
    assert len(page) == 2
    rest = await bot.backlog(from_id=page[-1]["id"] + 1)
    assert [it["id"] for it in page + rest] == [it["id"] for it in all_items]

    # The server's hard max is enforced, not silently clamped.
    with pytest.raises(httpx.HTTPStatusError) as exc:
        await bot.backlog(limit=5000, all_pages=False)
    assert exc.value.response.status_code == 422


def _set_files_safe(server, channel_id: int, on: bool) -> None:
    """Stands in for an admin's /files-safe: the scratch house has no admin."""
    with sqlite3.connect(server.db_path, timeout=10) as conn:
        conn.execute(
            "UPDATE channels SET files_safe = ?, files_safe_since = ? WHERE id = ?",
            (1, "2000-01-01T00:00:00.000Z", channel_id) if on else (0, None, channel_id),
        )


@pytest.fixture
def files_safe_main(server, main_id):
    _set_files_safe(server, main_id, True)
    yield main_id
    _set_files_safe(server, main_id, False)


async def test_a_bot_attaches_no_file_outside_a_files_safe_channel(server, main_id, bot):
    msg = await bot.send(main_id, "chart incoming")
    with pytest.raises(httpx.HTTPStatusError) as exc:
        await bot.upload([("chart.png", PNG_1PX, "image/png")], message_id=msg["id"])
    assert exc.value.response.status_code == 403
    staged = await bot.upload([("chart.png", PNG_1PX, "image/png")])
    with pytest.raises(httpx.HTTPStatusError) as exc:
        await bot.attach(msg["id"], [staged[0]["id"]])
    assert exc.value.response.status_code == 403
    assert "files-safe" in exc.value.response.json()["detail"]


async def test_upload_attaches_media_to_a_bot_message(
    server, alice, files_safe_main, bot, tmp_path
):
    """The DEFERRED sdk gap: attaching media used to mean hand-rolling
    /upload + /attachments/claim. Both documented flows must work end to end,
    and the result must be visible to a *user* fetching history."""
    main_id = files_safe_main
    # Flow 1 — post, then upload straight onto the bot's own message.
    msg = await bot.send(main_id, "chart incoming")
    atts = await bot.upload([("chart.png", PNG_1PX, "image/png")], message_id=msg["id"])
    assert len(atts) == 1
    att = atts[0]
    assert att["message_id"] == msg["id"]
    assert att["original_filename"] == "chart.png"
    assert att["mime_type"] == "image/png"
    assert att["size_bytes"] == len(PNG_1PX)
    assert att["has_preview"] is True
    assert {"url", "thumb_url", "orig_url"} <= set(att)

    fetched = next(
        m for m in await bot.get_messages(main_id, before_seq=msg["seq"] + 1) if m["id"] == msg["id"]
    )
    assert [a["id"] for a in fetched["attachments"]] == [att["id"]]

    # The signed URL needs no credentials — that is what makes it pasteable.
    async with httpx.AsyncClient(base_url=server.base_url, timeout=10) as anon:
        media = await anon.get(att["url"])
        assert media.status_code == 200, media.text

    # Flow 2 — stage from a path on disk first, then attach() to a new message.
    path = tmp_path / "diagram.png"
    path.write_bytes(PNG_1PX)
    staged = await bot.upload([path])
    assert staged[0]["message_id"] is None  # staged, not linked yet
    msg2 = await bot.send(main_id, "diagram attached")
    linked = await bot.attach(msg2["id"], [staged[0]["id"]])
    assert [a["id"] for a in linked["attachments"]] == [staged[0]["id"]]
    assert linked["attachments"][0]["original_filename"] == "diagram.png"

    # Re-claiming the same ids is idempotent, not an error.
    again = await bot.attach(msg2["id"], [staged[0]["id"]])
    assert [a["id"] for a in again["attachments"]] == [staged[0]["id"]]

    # And a user sees both messages carrying their files.
    resp = await alice.get(f"/channels/{main_id}/messages", params={"limit": 20})
    resp.raise_for_status()
    by_id = {m["id"]: m for m in resp.json()}
    assert len(by_id[msg["id"]]["attachments"]) == 1
    assert len(by_id[msg2["id"]]["attachments"]) == 1

    # Fails closed: a bot may not decorate someone else's message.
    others = await _post(alice, main_id, "alice's own message")
    orphan = await bot.upload([("x.png", PNG_1PX)])
    with pytest.raises(httpx.HTTPStatusError) as exc:
        await bot.attach(others["id"], [orphan[0]["id"]])
    assert exc.value.response.status_code == 403
    assert await bot.upload([]) == []


async def test_user_post_bot_receives_live_events(server, alice, main_id, bot_stream):
    bot, agen = bot_stream

    # create
    posted = await _post(alice, main_id, "hello from alice")
    event = await _next_matching(agen, _is_create("hello from alice"))
    assert isinstance(event, MessageCreate)
    assert event.channel_id == main_id
    assert event.seq == posted["seq"]
    assert event.context is None and event.backfilled is False
    assert event.message["author_type"] == "user"
    assert event.message["author"]["name"] == "alice"
    assert bot.last_seen_seq[main_id] == posted["seq"]

    # edit -> MessageEdit with updated content
    resp = await alice.patch(f"/messages/{posted['id']}", json={"content": "hello (edited)"})
    assert resp.status_code == 200, resp.text
    edit = await _next_matching(agen, lambda e: isinstance(e, MessageEdit))
    assert edit.seq == posted["seq"]
    assert edit.message["content"] == "hello (edited)"
    assert edit.message["edited_at"] is not None

    # delete -> MessageDelete
    resp = await alice.delete(f"/messages/{posted['id']}")
    assert resp.status_code == 200, resp.text
    delete = await _next_matching(agen, lambda e: isinstance(e, MessageDelete))
    assert delete.id == posted["id"] and delete.seq == posted["seq"]


async def test_bot_send_appears_via_user_fetch(server, alice, main_id, bot_stream):
    bot, _ = bot_stream
    trigger = await _post(alice, main_id, "please reply to this")

    sent = await bot.send(
        main_id,
        "bot reply here",
        reply_to=trigger["id"],
        emote_refs=[{"name": "happy"}],
        attribution={"model": "m-1", "verified": True, "summoner": "alice"},
    )
    assert sent["author_type"] == "bot" and sent["author_id"] == bot.bot_id
    assert sent["reply_to_id"] == trigger["id"]
    assert sent["emote_refs"] == [{"name": "happy"}]
    assert sent["attribution"] == {"model": "m-1", "verified": True,
                                   "summoner": "alice"}

    resp = await alice.get(f"/channels/{main_id}/messages", params={"limit": 10})
    resp.raise_for_status()
    fetched = next(m for m in resp.json() if m["id"] == sent["id"])
    assert fetched["content"] == "bot reply here"
    assert fetched["reply_to_id"] == trigger["id"]

    # members() works for bots (their channel-discovery primitive)
    members = await bot.members(main_id)
    names = {(m["type"], m["name"]) for m in members}
    assert {("user", "alice"), ("user", "bob"), ("bot", server.bot_name)} <= names


async def test_a_bot_trace_reaches_a_user_fetch(server, alice, main_id, bot_stream):
    bot, _ = bot_stream
    trace = {"steps": [{"kind": "memory", "label": "recall backup drill",
                        "outcome": "ok", "reason": None, "ms": 12}],
             "total": 3}
    sent = await bot.send(main_id, "traced reply", trace=trace)
    assert sent["trace"] == trace

    resp = await alice.get(f"/channels/{main_id}/messages", params={"limit": 10})
    resp.raise_for_status()
    fetched = next(m for m in resp.json() if m["id"] == sent["id"])
    assert fetched["trace"] == trace


async def test_mention_gets_context_with_awake_users(server, alice, main_id, bot_stream):
    bot, agen = bot_stream
    ws = await _user_ws(server, alice)  # alice is now "awake"
    try:
        content = f"hey @{server.bot_name}, you around?"
        await _post(alice, main_id, content)
        event = await _next_matching(agen, _is_create(content))
        assert event.context is not None
        awake = {u["name"]: u["status"] for u in event.context["awake_users"]}
        assert "alice" in awake
        assert event.context["channel_state"]["name"]
        assert event.context["privacy_flags_on_current_message"] == {}

        # a non-mention message carries no context
        await _post(alice, main_id, "just chatting, nobody in particular")
        event = await _next_matching(agen, _is_create("just chatting, nobody in particular"))
        assert event.context is None
    finally:
        await ws.close()


async def test_secret_message_never_reaches_bot(server, alice, main_id, bot_stream):
    bot, agen = bot_stream
    secret = await _post(
        alice, main_id, "the launch code is 1234", privacy_flags={"secret": True}
    )
    marker = await _post(alice, main_id, "weather is nice today")

    # The next MessageCreate the bot sees is the marker — the secret one is
    # skipped entirely (seq gap), not delayed.
    event = await _next_matching(agen, lambda e: isinstance(e, MessageCreate))
    assert event.message["content"] == "weather is nice today"
    assert event.seq == marker["seq"] == secret["seq"] + 1

    # And REST backfill hides it too — no payload, not even a tombstone.
    history = await bot.get_messages(main_id, from_seq=secret["seq"], limit=200)
    assert all(m.get("id") != secret["id"] for m in history)

    # The user still sees it.
    resp = await alice.get(f"/channels/{main_id}/messages", params={"limit": 10})
    assert any(m["id"] == secret["id"] for m in resp.json())


async def test_search_finds_posted_message_and_hides_secret(server, alice, main_id, bot):
    posted = await _post(alice, main_id, "the tarragon harvest starts thursday")
    await _post(
        alice, main_id, "tarragon stash location", privacy_flags={"secret": True}
    )

    results = await bot.search("tarragon")
    contents = [r["message"]["content"] for r in results]
    assert "the tarragon harvest starts thursday" in contents
    assert all("stash" not in c for c in contents)  # secret never reaches the bot
    hit = next(r for r in results if r["message"]["id"] == posted["id"])
    assert hit["channel"]["id"] == main_id and hit["channel"]["type"] == "main_feed"

    # Date bounds narrow the window; a future `after` excludes everything
    assert await bot.search("tarragon", after="2099-01-01") == []


async def test_reconnect_backfills_missed_messages_in_order(server, alice, main_id, bot_stream):
    bot, agen = bot_stream

    m1 = await _post(alice, main_id, "reconnect-m1")
    await _next_matching(agen, _is_create("reconnect-m1"))
    assert bot.last_seen_seq[main_id] == m1["seq"]

    # Kill the socket out from under the client, then post while it's down.
    assert bot.ws is not None
    await bot.ws.close()
    m2 = await _post(alice, main_id, "reconnect-m2")
    m3 = await _post(alice, main_id, "reconnect-m3")
    # Edit m2 while disconnected: backfill is current-state, so the synthetic
    # create must carry the EDITED content and no MessageEdit event follows.
    resp = await alice.patch(f"/messages/{m2['id']}", json={"content": "reconnect-m2 (edited)"})
    assert resp.status_code == 200

    ready = await _next_matching(agen, lambda e: isinstance(e, Ready), timeout=20)
    assert ready.reconnected is True

    ev2 = await _next_matching(agen, lambda e: isinstance(e, MessageCreate))
    ev3 = await _next_matching(agen, lambda e: isinstance(e, MessageCreate))
    assert (ev2.seq, ev3.seq) == (m2["seq"], m3["seq"])  # ascending order
    assert ev2.backfilled and ev3.backfilled
    assert ev2.message["content"] == "reconnect-m2 (edited)"
    assert ev2.message["edited_at"] is not None
    assert ev3.message["content"] == "reconnect-m3"
    assert bot.last_seen_seq[main_id] == m3["seq"]

    # Live stream resumes with no duplicates: the very next create is m4.
    m4 = await _post(alice, main_id, "reconnect-m4")
    ev4 = await _next_matching(agen, lambda e: isinstance(e, MessageCreate))
    assert ev4.seq == m4["seq"] and ev4.message["content"] == "reconnect-m4"
    assert ev4.backfilled is False


async def test_dm_traffic_never_reaches_bot(server, alice, main_id, bot_stream):
    bot, agen = bot_stream

    bob_http = await _login(server, "bob")
    try:
        bob_id = (await bob_http.get("/me")).json()["id"]
    finally:
        await bob_http.aclose()

    resp = await alice.post("/dms", json={"user_id": bob_id})
    assert resp.status_code == 200, resp.text
    dm_id = resp.json()["id"]
    assert dm_id != main_id

    await _post(alice, dm_id, "psst bob, private stuff")
    marker = await _post(alice, main_id, "public marker after dm")

    event = await _next_matching(agen, lambda e: isinstance(e, MessageCreate))
    assert event.channel_id == main_id
    assert event.message["content"] == "public marker after dm"
    assert marker["seq"] == event.seq
    assert dm_id not in bot.last_seen_seq

    # Bot REST access to the DM is denied outright.
    with pytest.raises(httpx.HTTPStatusError) as exc_info:
        await bot.get_messages(dm_id, from_seq=1)
    assert exc_info.value.response.status_code == 403


async def test_text_channel_create_event_and_membership_gate(server, alice, bot_stream):
    """A user-created text channel reaches the bot as a typed ChannelCreate,
    but its messages stay invisible until the bot is explicitly added."""
    bot, agen = bot_stream

    resp = await alice.post("/channels", json={"name": "sdk-test-channel"})
    assert resp.status_code == 200, resp.text
    created = resp.json()
    assert created["type"] == "text" and created["name"] == "sdk-test-channel"
    cid = created["id"]

    event = await _next_matching(agen, lambda e: isinstance(e, ChannelCreate))
    assert event.channel == {
        "id": cid,
        "type": "text",
        "name": "sdk-test-channel",
        "visibility": "public",
    }

    # Not a member yet: REST access denied, live messages skipped entirely.
    with pytest.raises(httpx.HTTPStatusError) as exc_info:
        await bot.get_messages(cid, from_seq=1)
    assert exc_info.value.response.status_code == 403
    await _post(alice, cid, "pre-membership message")

    # Any user may add the bot (flat access on text channels).
    resp = await alice.post(f"/channels/{cid}/bots", json={"bot_id": bot.bot_id})
    assert resp.status_code == 200, resp.text

    posted = await _post(alice, cid, "hello sdk channel")
    event = await _next_matching(agen, _is_create("hello sdk channel"))
    assert event.channel_id == cid and event.seq == posted["seq"]
    assert bot.last_seen_seq[cid] == posted["seq"]

    # Member now: bot can post + the channel shows up in bot search.
    sent = await bot.send(cid, "bot checking in")
    assert sent["channel_id"] == cid
    results = await bot.search("sdk channel")
    assert any(r["channel"]["id"] == cid and r["channel"]["type"] == "text" for r in results)


async def test_typing_op_accepted_and_broadcast(server, alice, main_id, bot_stream):
    bot, _ = bot_stream
    ws = await _user_ws(server, alice)
    try:
        await bot.typing(main_id)
        while True:
            frame = json.loads(await asyncio.wait_for(ws.recv(), EVENT_TIMEOUT))
            if frame.get("type") == "typing_start":
                break
        assert frame["channel_id"] == main_id
        assert frame["author_type"] == "bot"
        assert frame["author_id"] == bot.bot_id
    finally:
        await ws.close()


# ---------------------------------------------------------------------------
# A slow consumer
# ---------------------------------------------------------------------------

FAST_PING = 0.2


@pytest.fixture
def fast_keepalive(monkeypatch):
    """The SDK's own sockets ping every 0.2 s and give a pong 0.2 s: the
    library's 20 s / 20 s keepalive, scaled so a lost pong costs under a second."""
    monkeypatch.setattr(sdk_client, "websockets", SimpleNamespace(
        connect=partial(websockets.connect, ping_interval=FAST_PING,
                        ping_timeout=FAST_PING),
        ConnectionClosed=websockets.ConnectionClosed,
    ))


async def _until(cond: Callable[[], bool], timeout: float = EVENT_TIMEOUT) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not cond():
        assert loop.time() < deadline, "timed out waiting for a condition"
        await asyncio.sleep(0.01)


def _has_read(bot: DisjornClient, channel_id: int, message: dict) -> Callable[[], bool]:
    """The reader has this message off the socket, delivered or not."""
    return lambda: bot._read_seq.get(channel_id, 0) >= message["seq"]


async def _flip_presence(ws, times: int, last: str = "online") -> None:
    """`times` presence frames from this user, each one seen back before the next."""
    for i in range(times):
        status = last if i == times - 1 else ("idle", "dnd")[i % 2]
        await ws.send(json.dumps({"op": "status", "status": status}))
        while True:
            frame = json.loads(await asyncio.wait_for(ws.recv(), EVENT_TIMEOUT))
            if frame.get("type") == "presence" and frame["status"] == status:
                break


async def _creates_until(agen: AsyncIterator[Event], last: dict) -> list[Event]:
    """Every Ready and MessageCreate up to and including the message `last`."""
    out: list[Event] = []
    while True:
        event = await _next_matching(
            agen, lambda e: isinstance(e, (Ready, MessageCreate)))
        out.append(event)
        if isinstance(event, MessageCreate) and event.seq == last["seq"]:
            return out


@pytest.mark.parametrize("enqueue_only", [False, True],
                         ids=["turn-in-handler", "handler-only-enqueues"])
async def test_a_turn_longer_than_the_keepalive_keeps_its_socket(
    server, alice, main_id, bot, fast_keepalive, enqueue_only
):
    """Over 16 frames arrive during one turn, a message among them: it is
    delivered live after the turn, on the same connection."""
    readies: list[Ready] = []
    creates: list[MessageCreate] = []
    in_turn, done = asyncio.Event(), asyncio.Event()

    async def turn(event: Event) -> None:
        if isinstance(event, Ready):
            readies.append(event)
        if not isinstance(event, MessageCreate):
            return
        creates.append(event)
        if event.message["content"] == "turn-a":
            in_turn.set()
            await asyncio.sleep(6 * FAST_PING)
        if event.message["content"] == "after-b":
            done.set()

    inbox: asyncio.Queue = asyncio.Queue()

    async def enqueue(event: Event) -> None:
        inbox.put_nowait(event)

    async def serve() -> None:
        while True:
            await turn(await inbox.get())

    tasks = [asyncio.create_task(bot.run(enqueue if enqueue_only else turn))]
    if enqueue_only:
        tasks.append(asyncio.create_task(serve()))
    ws = await _user_ws(server, alice)
    try:
        await _post(alice, main_id, "turn-a")
        await asyncio.wait_for(in_turn.wait(), EVENT_TIMEOUT)
        await _flip_presence(ws, 20)
        summon = await _post(alice, main_id, "summon-b")
        await _flip_presence(ws, 4)
        await _post(alice, main_id, "after-b")
        await asyncio.wait_for(done.wait(), EVENT_TIMEOUT)
    finally:
        await ws.close()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    assert [r.reconnected for r in readies] == [False]
    assert [e.message["content"] for e in creates] == ["turn-a", "summon-b", "after-b"]
    b = creates[1]
    assert b.seq == summon["seq"] and b.backfilled is False


async def test_presence_waiting_for_a_busy_consumer_keeps_only_the_newest(
    server, alice, main_id, bot_stream
):
    bot, agen = bot_stream
    me = (await alice.get("/me")).json()["id"]
    ws = await _user_ws(server, alice)
    try:
        await _flip_presence(ws, 9, last="dnd")
        marker = await _post(alice, main_id, "after the flips")
        await _until(_has_read(bot, main_id, marker))

        seen: list[str] = []
        while True:
            event = await asyncio.wait_for(anext(agen), EVENT_TIMEOUT)
            if isinstance(event, Presence) and event.user_id == me:
                seen.append(event.status)
            if isinstance(event, MessageCreate) and event.seq == marker["seq"]:
                break
        assert seen == ["dnd"]
    finally:
        await _flip_presence(ws, 1)
        await ws.close()


async def test_a_full_queue_stops_the_reader_and_loses_nothing(
    server, alice, main_id, caplog
):
    bot = DisjornClient(server.base_url, api_key=server.api_key,
                        backoff_initial=0.2, max_queued_events=3)
    agen = bot.events()
    try:
        await _next_matching(agen, lambda e: isinstance(e, Ready))
        posted = [await _post(alice, main_id, f"full-{i}") for i in range(7)]
        # Three wait and the reader holds a fourth; it reads no further.
        await _until(_has_read(bot, main_id, posted[2]))
        await asyncio.sleep(0.3)
        assert bot._read_seq[main_id] <= posted[3]["seq"]
        assert main_id not in bot.last_seen_seq
        assert any("reading stops" in r.getMessage() for r in caplog.records)

        got = [e for e in await _creates_until(agen, posted[-1])
               if isinstance(e, MessageCreate)]
        assert [e.seq for e in got] == [m["seq"] for m in posted]
        assert not any(e.backfilled for e in got)
    finally:
        await bot.aclose()
        await agen.aclose()


async def test_a_queue_full_past_the_keepalive_resyncs_by_backfill(
    server, alice, main_id, fast_keepalive
):
    """The bound's fallback is the old behaviour: the socket drops and the
    backfill returns the rest, each message once and in order."""
    bot = DisjornClient(server.base_url, api_key=server.api_key,
                        backoff_initial=0.2, max_queued_events=3)
    agen = bot.events()
    try:
        await _next_matching(agen, lambda e: isinstance(e, Ready))
        posted = [await _post(alice, main_id, f"flood-{i}") for i in range(30)]
        await asyncio.sleep(6 * FAST_PING)

        got = await _creates_until(agen, posted[-1])
        creates = [e for e in got if isinstance(e, MessageCreate)]
        assert [e.seq for e in creates] == [m["seq"] for m in posted]
        assert any(isinstance(e, Ready) and e.reconnected for e in got)
        assert creates[0].backfilled is False and creates[-1].backfilled is True
    finally:
        await bot.aclose()
        await agen.aclose()


async def test_the_cursor_moves_on_delivery_so_a_crash_skips_nothing(
    server, alice, main_id, bot_stream
):
    bot, agen = bot_stream
    m1 = await _post(alice, main_id, "crash-m1")
    await _next_matching(agen, _is_create("crash-m1"))
    m2 = await _post(alice, main_id, "crash-m2")
    m3 = await _post(alice, main_id, "crash-m3")
    await _until(_has_read(bot, main_id, m3))

    # Read but never handed over: the cursor a bot would persist stays at m1.
    assert bot.last_seen_seq == {main_id: m1["seq"]}
    saved = dict(bot.last_seen_seq)
    await bot.aclose()
    with pytest.raises(StopAsyncIteration):
        await asyncio.wait_for(anext(agen), EVENT_TIMEOUT)
    assert bot.last_seen_seq == saved

    restarted = DisjornClient(server.base_url, api_key=server.api_key,
                              backoff_initial=0.2)
    for channel_id, seq in saved.items():
        restarted.seed_seq(channel_id, seq)
    stream = restarted.events()
    try:
        await _next_matching(stream, lambda e: isinstance(e, Ready))
        await restarted.ws.close()
        ready = await _next_matching(stream, lambda e: isinstance(e, Ready), timeout=20)
        assert ready.reconnected is True
        ev2 = await _next_matching(stream, lambda e: isinstance(e, MessageCreate))
        ev3 = await _next_matching(stream, lambda e: isinstance(e, MessageCreate))
        assert (ev2.seq, ev3.seq) == (m2["seq"], m3["seq"])
        assert ev2.backfilled and ev3.backfilled
        assert restarted.last_seen_seq[main_id] == m3["seq"]
    finally:
        await restarted.aclose()
        await stream.aclose()


async def test_a_drop_with_events_waiting_keeps_them_live_and_backfills_only_the_gap(
    server, alice, main_id
):
    bot = DisjornClient(server.base_url, api_key=server.api_key, backoff_initial=0.5)
    agen = bot.events()
    try:
        await _next_matching(agen, lambda e: isinstance(e, Ready))
        m0 = await _post(alice, main_id, "drop-m0")
        await _next_matching(agen, _is_create("drop-m0"))

        m1 = await _post(alice, main_id, "drop-m1")
        m2 = await _post(alice, main_id, "drop-m2")
        await _until(_has_read(bot, main_id, m2))
        await bot.ws.close()
        m3 = await _post(alice, main_id, "drop-m3")
        m4 = await _post(alice, main_id, "drop-m4")
        await _until(lambda: bot.ws is not None and _has_read(bot, main_id, m4)(),
                     timeout=20)
        m5 = await _post(alice, main_id, "drop-m5")
        assert bot.last_seen_seq[main_id] == m0["seq"]

        got = await _creates_until(agen, m5)
        assert [(e.seq, e.backfilled) if isinstance(e, MessageCreate) else "reconnected"
                for e in got] == [
            (m1["seq"], False),
            (m2["seq"], False),
            "reconnected",
            (m3["seq"], True),
            (m4["seq"], True),
            (m5["seq"], False),
        ]
        assert bot.last_seen_seq[main_id] == m5["seq"]
    finally:
        await bot.aclose()
        await agen.aclose()


async def test_aclose_ends_a_waiting_consumer_and_its_reader(server, bot_stream):
    bot, agen = bot_stream
    waiting = asyncio.create_task(anext(agen))
    await asyncio.sleep(0.05)
    reader = bot._reader
    assert reader is not None and not reader.done()

    await bot.aclose()
    with pytest.raises(StopAsyncIteration):
        await asyncio.wait_for(waiting, EVENT_TIMEOUT)
    assert reader.done() and bot.ws is None


async def test_aclose_ends_a_reader_that_is_waiting_for_room(server, alice, main_id):
    bot = DisjornClient(server.base_url, api_key=server.api_key, max_queued_events=1)
    agen = bot.events()
    try:
        await _next_matching(agen, lambda e: isinstance(e, Ready))
        posted = [await _post(alice, main_id, f"room-{i}") for i in range(3)]
        await _until(_has_read(bot, main_id, posted[1]))
        reader = bot._reader
        assert not reader.done()
    finally:
        await bot.aclose()
    assert reader.done()
    with pytest.raises(StopAsyncIteration):
        await asyncio.wait_for(anext(agen), EVENT_TIMEOUT)


async def test_a_rejected_key_raises_in_the_consumer(server):
    bot = DisjornClient(server.base_url, api_key="not-a-key", backoff_initial=0.2)
    try:
        with pytest.raises(DisjornAuthError):
            await asyncio.wait_for(bot.run(lambda event: asyncio.sleep(0)), EVENT_TIMEOUT)
        assert bot._reader.done()
    finally:
        await bot.aclose()


@pytest.mark.parametrize("consumer", ["run", "events"])
def test_a_consumer_cancelled_mid_turn_leaves_no_task_behind(server, caplog, consumer):
    """Nobody calls aclose(): the shape of a daemon stopped mid-turn."""
    leftovers: list[asyncio.Task] = []

    async def main() -> None:
        bot = DisjornClient(server.base_url, api_key=server.api_key)
        in_turn = asyncio.Event()

        async def handler(event: Event) -> None:
            in_turn.set()
            await asyncio.sleep(3600)

        async def iterate() -> None:
            async for event in bot.events():
                await handler(event)

        task = asyncio.create_task(bot.run(handler) if consumer == "run" else iterate())
        await asyncio.wait_for(in_turn.wait(), EVENT_TIMEOUT)
        reader = bot._reader
        assert reader is not None and not reader.done()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        if consumer == "run":
            assert reader.done() and bot.ws is None
        leftovers.append(reader)
        await bot._http.aclose()

    with caplog.at_level(logging.WARNING, logger="asyncio"):
        asyncio.run(main())
        gc.collect()
    assert leftovers[0].done()
    assert [r.getMessage() for r in caplog.records if r.name == "asyncio"] == []
