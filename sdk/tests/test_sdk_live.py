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
import json
from typing import Any, AsyncIterator, Callable

import httpx
import pytest
import websockets

from disjorn_sdk import (
    ChannelCreate,
    DisjornClient,
    Event,
    MessageCreate,
    MessageDelete,
    MessageEdit,
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
    client = httpx.AsyncClient(base_url=server.base_url, timeout=10)
    resp = await client.post(
        "/auth/login", json={"username": username, "password": server.users[username]}
    )
    assert resp.status_code == 200, resp.text
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
    token = http_client.cookies.get("disjorn_session")
    assert token
    ws = await websockets.connect(
        server.base_url.replace("http://", "ws://") + "/ws",
        additional_headers={"Cookie": f"disjorn_session={token}"},
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


async def test_upload_attaches_media_to_a_bot_message(server, alice, main_id, bot, tmp_path):
    """The DEFERRED sdk gap: attaching media used to mean hand-rolling
    /upload + /attachments/claim. Both documented flows must work end to end,
    and the result must be visible to a *user* fetching history."""
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
    )
    assert sent["author_type"] == "bot" and sent["author_id"] == bot.bot_id
    assert sent["reply_to_id"] == trigger["id"]
    assert sent["emote_refs"] == [{"name": "happy"}]

    resp = await alice.get(f"/channels/{main_id}/messages", params={"limit": 10})
    resp.raise_for_status()
    fetched = next(m for m in resp.json() if m["id"] == sent["id"])
    assert fetched["content"] == "bot reply here"
    assert fetched["reply_to_id"] == trigger["id"]

    # members() works for bots (their channel-discovery primitive)
    members = await bot.members(main_id)
    names = {(m["type"], m["name"]) for m in members}
    assert {("user", "alice"), ("user", "bob"), ("bot", server.bot_name)} <= names


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
    assert event.channel == {"id": cid, "type": "text", "name": "sdk-test-channel"}

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
