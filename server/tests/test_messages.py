"""WP4 tests: message create/edit/delete + bus events, seq allocation,
reply validation, pagination/backfill semantics, bot privacy exclusion, search."""

from app import db, events
from app.routers import auth
from app.routers.messages import MAX_MESSAGE_CHARS, MAX_METADATA_CHARS

PASSWORD = "correct horse battery staple"
BOT_KEY = "bot-key-1"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def make_user(username: str, display_name: str | None = None) -> int:
    cur = await db.execute(
        "INSERT INTO users (username, password_hash, display_name) VALUES (?, ?, ?)",
        (username, auth.hash_password(PASSWORD), display_name or username.capitalize()),
    )
    return cur.lastrowid


async def make_bot(name: str = "claw", api_key: str = BOT_KEY) -> int:
    cur = await db.execute(
        "INSERT INTO bots (name, api_key_hash) VALUES (?, ?)",
        (name, auth.hash_api_key(api_key)),
    )
    return cur.lastrowid


async def add_member(channel_id: int, member_type: str, member_id: int) -> None:
    await db.execute(
        """INSERT OR IGNORE INTO channel_members (channel_id, member_type, member_id)
           VALUES (?, ?, ?)""",
        (channel_id, member_type, member_id),
    )


async def login(client, username: str) -> None:
    r = await client.post("/auth/login", json={"username": username, "password": PASSWORD})
    assert r.status_code == 200


def as_bot(client) -> dict[str, str]:
    """Drop the user cookie so get_actor resolves the API key header."""
    client.cookies.clear()
    return {"X-Api-Key": BOT_KEY}


async def main_feed_id() -> int:
    row = await db.fetch_one("SELECT id FROM channels WHERE type = 'main_feed'")
    assert row is not None
    return row["id"]


async def make_dm(client, other_user_id: int) -> int:
    r = await client.post("/dms", json={"user_id": other_user_id})
    assert r.status_code == 200
    return r.json()["id"]


def capture_events() -> list[dict]:
    """Register an in-test bus subscriber (conftest clears it after the test)."""
    captured: list[dict] = []
    events.subscribe(captured.append)
    return captured


async def post_message(client, channel_id: int, content: str, **extra) -> dict:
    r = await client.post(f"/channels/{channel_id}/messages", json={"content": content, **extra})
    assert r.status_code == 200, r.text
    return r.json()


# ---------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------

async def test_create_message_payload_and_event(client):
    uid = await make_user("alice")
    await login(client, "alice")
    main = await main_feed_id()
    captured = capture_events()

    msg = await post_message(client, main, "hello world")
    assert msg["channel_id"] == main and msg["seq"] == 1
    assert msg["author_type"] == "user" and msg["author_id"] == uid
    assert msg["author"]["name"] == "Alice" and msg["author"]["username"] == "alice"
    assert msg["content"] == "hello world"
    assert msg["created_at"] and msg["edited_at"] is None and msg["deleted_at"] is None
    assert msg["reply_to_id"] is None
    assert msg["privacy_flags"] == {} and msg["emote_refs"] == []
    assert msg["attachments"] == []  # join always present, even before WP6

    assert len(captured) == 1
    event = captured[0]
    assert event["type"] == "message_create" and event["channel_id"] == main
    assert event["message"] == msg  # full materialized payload on the bus


async def test_create_membership_and_unknown_channel(client):
    await make_user("alice")
    uid_b = await make_user("bob")
    await make_user("carol")
    bot_id = await make_bot()
    await login(client, "alice")
    dm = await make_dm(client, uid_b)
    main = await main_feed_id()

    # Non-member user -> 403; unknown channel -> 404
    await login(client, "carol")
    r = await client.post(f"/channels/{dm}/messages", json={"content": "hi"})
    assert r.status_code == 403
    assert (await client.post("/channels/999/messages", json={"content": "x"})).status_code == 404

    # Bot with no membership row: 403 even on main_feed (nothing implicit for bots)
    headers = as_bot(client)
    r = await client.post(f"/channels/{main}/messages", json={"content": "beep"}, headers=headers)
    assert r.status_code == 403

    await add_member(main, "bot", bot_id)
    r = await client.post(f"/channels/{main}/messages", json={"content": "beep"}, headers=headers)
    assert r.status_code == 200
    assert r.json()["author"] == {
        "type": "bot", "id": bot_id, "name": "claw",
        "avatar_path": None, "avatar_url": None,
    }

    # No auth at all
    assert (await client.post(f"/channels/{main}/messages", json={"content": "x"})).status_code == 401


async def test_reply_validation(client):
    await make_user("alice")
    uid_b = await make_user("bob")
    await login(client, "alice")
    main = await main_feed_id()
    dm = await make_dm(client, uid_b)

    original = await post_message(client, main, "original")
    reply = await post_message(client, main, "reply", reply_to_id=original["id"])
    assert reply["reply_to_id"] == original["id"] and reply["seq"] == 2

    # reply_to in a different channel -> 400
    r = await client.post(
        f"/channels/{dm}/messages", json={"content": "x", "reply_to_id": original["id"]}
    )
    assert r.status_code == 400
    # nonexistent reply_to -> 400
    r = await client.post(
        f"/channels/{main}/messages", json={"content": "x", "reply_to_id": 9999}
    )
    assert r.status_code == 400


async def test_seq_monotonic_under_interleaved_channels(client):
    await make_user("alice")
    uid_b = await make_user("bob")
    await login(client, "alice")
    main = await main_feed_id()
    dm = await make_dm(client, uid_b)

    seqs: dict[int, list[int]] = {main: [], dm: []}
    for i in range(3):  # interleave the two channels
        for chan in (main, dm):
            msg = await post_message(client, chan, f"m{i}")
            seqs[chan].append(msg["seq"])
    assert seqs[main] == [1, 2, 3]
    assert seqs[dm] == [1, 2, 3]
    # UNIQUE(channel_id, seq) intact in the DB
    rows = await db.fetch_all(
        "SELECT channel_id, seq FROM messages ORDER BY channel_id, seq"
    )
    assert len(rows) == len({(r["channel_id"], r["seq"]) for r in rows}) == 6


async def test_bot_flags_and_emote_refs_stored(client):
    await make_user("alice")
    bot_id = await make_bot()
    main = await main_feed_id()
    await add_member(main, "bot", bot_id)
    headers = as_bot(client)

    r = await client.post(
        f"/channels/{main}/messages",
        json={
            "content": "just between us",
            "privacy_flags": {"secret": True, "off_the_record": False},
            "emote_refs": ["Smug"],
            "emotion": "Happy",  # chibi service (WP8) absent -> ignored silently
        },
        headers=headers,
    )
    assert r.status_code == 200
    msg = r.json()
    assert msg["privacy_flags"] == {"secret": True}  # falsy flags dropped, never stored
    assert msg["emote_refs"] == ["Smug"]

    # Round-trips through the DB as JSON text
    row = await db.fetch_one("SELECT privacy_flags, emote_refs FROM messages WHERE id = ?", (msg["id"],))
    assert row["privacy_flags"] == '{"secret": true}'
    assert row["emote_refs"] == '["Smug"]'


# ---------------------------------------------------------------------------
# Edit
# ---------------------------------------------------------------------------

async def test_edit_flow_events_and_flag_merge(client):
    await make_user("alice")
    await make_user("mallory")
    bot_id = await make_bot()
    main = await main_feed_id()
    await add_member(main, "bot", bot_id)

    await login(client, "alice")
    msg = await post_message(client, main, "first draft")
    captured = capture_events()

    r = await client.patch(f"/messages/{msg['id']}", json={"content": "second draft"})
    assert r.status_code == 200
    edited = r.json()
    assert edited["content"] == "second draft"
    assert edited["edited_at"] is not None
    assert edited["seq"] == msg["seq"] and edited["id"] == msg["id"]
    assert captured == [
        {"type": "message_edit", "channel_id": main, "message": edited}
    ]

    # Non-author user -> 403; unknown -> 404
    await login(client, "mallory")
    assert (await client.patch(f"/messages/{msg['id']}", json={"content": "hax"})).status_code == 403
    assert (await client.patch("/messages/999", json={"content": "x"})).status_code == 404

    # Bot edit keeps existing flags (merge never removes)
    headers = as_bot(client)
    r = await client.post(
        f"/channels/{main}/messages",
        json={"content": "psst", "privacy_flags": {"secret": True}},
        headers=headers,
    )
    bot_msg = r.json()
    r = await client.patch(f"/messages/{bot_msg['id']}", json={"content": "psst v2"}, headers=headers)
    assert r.status_code == 200
    assert r.json()["privacy_flags"] == {"secret": True}
    # Bot cannot edit alice's message
    assert (await client.patch(f"/messages/{msg['id']}", json={"content": "x"}, headers=headers)).status_code == 403


async def test_edit_deleted_message_conflicts(client):
    await make_user("alice")
    await login(client, "alice")
    main = await main_feed_id()
    msg = await post_message(client, main, "doomed")
    assert (await client.delete(f"/messages/{msg['id']}")).status_code == 200
    assert (await client.patch(f"/messages/{msg['id']}", json={"content": "zombie"})).status_code == 409


# ---------------------------------------------------------------------------
# Delete
# ---------------------------------------------------------------------------

async def test_delete_flow_and_event(client):
    await make_user("alice")
    await make_user("mallory")
    await login(client, "alice")
    main = await main_feed_id()
    msg = await post_message(client, main, "to be removed")
    keep = await post_message(client, main, "kept")

    # Non-author -> 403
    await login(client, "mallory")
    assert (await client.delete(f"/messages/{msg['id']}")).status_code == 403

    await login(client, "alice")
    captured = capture_events()
    r = await client.delete(f"/messages/{msg['id']}")
    assert r.status_code == 200 and r.json() == {"ok": True}
    assert captured == [
        {"type": "message_delete", "channel_id": main, "id": msg["id"], "seq": msg["seq"]}
    ]

    # Soft delete: row retained with deleted_at, hidden from scrollback
    row = await db.fetch_one("SELECT deleted_at FROM messages WHERE id = ?", (msg["id"],))
    assert row["deleted_at"] is not None
    listed = (await client.get(f"/channels/{main}/messages")).json()
    assert [m["id"] for m in listed] == [keep["id"]]

    # Idempotent second delete: ok, no duplicate event
    r = await client.delete(f"/messages/{msg['id']}")
    assert r.status_code == 200 and len(captured) == 1

    assert (await client.delete("/messages/999")).status_code == 404


# ---------------------------------------------------------------------------
# GET /channels/{id}/messages — scrollback + backfill
# ---------------------------------------------------------------------------

async def test_scrollback_pagination_newest_first(client):
    await make_user("alice")
    await login(client, "alice")
    main = await main_feed_id()
    for i in range(1, 11):
        await post_message(client, main, f"msg {i}")

    all_msgs = (await client.get(f"/channels/{main}/messages")).json()
    assert [m["seq"] for m in all_msgs] == list(range(10, 0, -1))  # newest first

    page1 = (await client.get(f"/channels/{main}/messages", params={"limit": 3})).json()
    assert [m["seq"] for m in page1] == [10, 9, 8]
    page2 = (
        await client.get(f"/channels/{main}/messages", params={"limit": 3, "before_seq": 8})
    ).json()
    assert [m["seq"] for m in page2] == [7, 6, 5]

    # limit capped at 200; mixing modes is a 400
    assert (
        await client.get(f"/channels/{main}/messages", params={"limit": 500})
    ).status_code == 422
    assert (
        await client.get(f"/channels/{main}/messages", params={"before_seq": 5, "from_seq": 1})
    ).status_code == 400

    # Membership gate on reads too
    await make_user("carol")
    uid_b = await make_user("bob")
    dm = await make_dm(client, uid_b)
    await login(client, "carol")
    assert (await client.get(f"/channels/{dm}/messages")).status_code == 403
    assert (await client.get("/channels/999/messages")).status_code == 404


async def test_backfill_ascending_and_after_edit(client):
    await make_user("alice")
    await login(client, "alice")
    main = await main_feed_id()
    msgs = [await post_message(client, main, f"v1 msg {i}") for i in range(1, 6)]

    await client.patch(f"/messages/{msgs[2]['id']}", json={"content": "v2 edited"})

    backfill = (
        await client.get(f"/channels/{main}/messages", params={"from_seq": 2})
    ).json()
    assert [m["seq"] for m in backfill] == [2, 3, 4, 5]  # ascending
    edited = next(m for m in backfill if m["seq"] == 3)
    # Current-state semantics: the edit is applied, not replayed
    assert edited["content"] == "v2 edited" and edited["edited_at"] is not None


async def test_backfill_tombstones_for_deleted(client):
    await make_user("alice")
    await login(client, "alice")
    main = await main_feed_id()
    m1 = await post_message(client, main, "one")
    m2 = await post_message(client, main, "two")
    m3 = await post_message(client, main, "three")
    await client.delete(f"/messages/{m2['id']}")

    backfill = (
        await client.get(f"/channels/{main}/messages", params={"from_seq": 1})
    ).json()
    assert backfill[0]["id"] == m1["id"]
    assert backfill[1] == {"id": m2["id"], "seq": 2, "deleted": True}  # tombstone
    assert backfill[2]["id"] == m3["id"] and backfill[2]["content"] == "three"

    # Scrollback mode omits deleted entirely (no tombstone)
    listed = (await client.get(f"/channels/{main}/messages")).json()
    assert [m["id"] for m in listed] == [m3["id"], m1["id"]]


async def test_bot_reads_exclude_privacy_flagged(client):
    await make_user("alice")
    bot_id = await make_bot()
    main = await main_feed_id()
    await add_member(main, "bot", bot_id)

    await login(client, "alice")
    plain = await post_message(client, main, "public hello")
    secret = await post_message(client, main, "sekrit", privacy_flags={"secret": True})
    otr = await post_message(client, main, "otr", privacy_flags={"off_the_record": True})
    gone = await post_message(client, main, "deleted later")
    await client.delete(f"/messages/{gone['id']}")

    # User backfill: sees flagged messages + tombstone
    user_backfill = (
        await client.get(f"/channels/{main}/messages", params={"from_seq": 1})
    ).json()
    assert [m["seq"] for m in user_backfill] == [1, 2, 3, 4]
    assert user_backfill[1]["content"] == "sekrit"

    # Bot backfill: flagged messages entirely absent (no tombstone), deleted
    # unflagged message still tombstoned
    headers = as_bot(client)
    bot_backfill = (
        await client.get(f"/channels/{main}/messages", params={"from_seq": 1}, headers=headers)
    ).json()
    assert [m["seq"] for m in bot_backfill] == [plain["seq"], gone["seq"]]
    assert bot_backfill[1] == {"id": gone["id"], "seq": gone["seq"], "deleted": True}
    assert all(m["id"] not in (secret["id"], otr["id"]) for m in bot_backfill)

    # Scrollback mode filters for bots too
    bot_listed = (
        await client.get(f"/channels/{main}/messages", headers=headers)
    ).json()
    assert [m["id"] for m in bot_listed] == [plain["id"]]


# ---------------------------------------------------------------------------
# GET /search
# ---------------------------------------------------------------------------

async def test_search_basic_and_excludes_deleted(client):
    await make_user("alice")
    await login(client, "alice")
    main = await main_feed_id()
    hit = await post_message(client, main, "the zanzibar shipment arrives tuesday")
    await post_message(client, main, "unrelated chatter")

    r = await client.get("/search", params={"q": "zanzibar"})
    assert r.status_code == 200
    [result] = r.json()
    assert result["message"]["id"] == hit["id"]
    assert result["message"]["content"] == "the zanzibar shipment arrives tuesday"
    assert result["channel"] == {"id": main, "type": "main_feed", "name": "main"}

    # Deleted messages drop out of search results
    await client.delete(f"/messages/{hit['id']}")
    assert (await client.get("/search", params={"q": "zanzibar"})).json() == []


async def test_search_scoped_to_own_channels(client):
    await make_user("alice")
    uid_b = await make_user("bob")
    await make_user("carol")
    await login(client, "alice")
    dm = await make_dm(client, uid_b)
    await post_message(client, dm, "xylophone plans are secret")

    # DM participant finds it
    r = await client.get("/search", params={"q": "xylophone"})
    assert [x["message"]["content"] for x in r.json()] == ["xylophone plans are secret"]

    # Outsider does not — search is scoped to the caller's channels
    await login(client, "carol")
    assert (await client.get("/search", params={"q": "xylophone"})).json() == []

    # Bot with no channel membership: search works but sees nothing
    await make_bot()
    headers = as_bot(client)
    r = await client.get("/search", params={"q": "xylophone"}, headers=headers)
    assert r.status_code == 200 and r.json() == []

    # No auth at all -> 401
    client.cookies.clear()
    assert (await client.get("/search", params={"q": "x"})).status_code == 401


async def test_text_channel_messaging_and_search_scoping(client):
    """Text channels: users post/read/search implicitly; bots need explicit
    membership for all three. DM behavior unchanged alongside."""
    await make_user("alice")
    uid_b = await make_user("bob")
    bot_id = await make_bot()
    await login(client, "alice")
    r = await client.post("/channels", json={"name": "custodian"})
    assert r.status_code == 200, r.text
    cid = r.json()["id"]
    dm = await make_dm(client, uid_b)

    msg = await post_message(client, cid, "the flamingo mop is in bay 3")
    assert msg["channel_id"] == cid and msg["seq"] == 1
    await post_message(client, dm, "flamingo secrets, dm only")

    # Any user (not just the creator) posts and reads implicitly.
    await login(client, "bob")
    await post_message(client, cid, "acknowledged, fetching the flamingo mop")
    r = await client.get(f"/channels/{cid}/messages")
    assert r.status_code == 200 and len(r.json()) == 2

    # User search spans text channels AND their DMs.
    r = await client.get("/search", params={"q": "flamingo"})
    assert {x["channel"]["id"] for x in r.json()} == {cid, dm}
    hit = next(x for x in r.json() if x["channel"]["id"] == cid)
    assert hit["channel"] == {"id": cid, "type": "text", "name": "custodian"}

    # Bot: no implicit access — post/read/search all excluded until added.
    headers = as_bot(client)
    r = await client.post(f"/channels/{cid}/messages", json={"content": "beep"}, headers=headers)
    assert r.status_code == 403
    assert (await client.get(f"/channels/{cid}/messages", headers=headers)).status_code == 403
    r = await client.get("/search", params={"q": "flamingo"}, headers=headers)
    assert r.status_code == 200 and r.json() == []

    await add_member(cid, "bot", bot_id)
    r = await client.post(f"/channels/{cid}/messages", json={"content": "beep"}, headers=headers)
    assert r.status_code == 200
    r = await client.get("/search", params={"q": "flamingo"}, headers=headers)
    # Member of the text channel now — but the DM stays invisible.
    assert {x["channel"]["id"] for x in r.json()} == {cid}


async def _set_created_at(message_id: int, created_at: str) -> None:
    """Pin a message's created_at for deterministic date-range assertions."""
    await db.execute(
        "UPDATE messages SET created_at = ? WHERE id = ?", (created_at, message_id)
    )


async def test_bot_search_scoped_to_member_channels(client):
    await make_user("alice")
    uid_b = await make_user("bob")
    bot_id = await make_bot()
    main = await main_feed_id()
    await add_member(main, "bot", bot_id)

    await login(client, "alice")
    dm = await make_dm(client, uid_b)
    main_msg = await post_message(client, main, "quokka sighting in the main feed")
    await post_message(client, dm, "quokka plans, just for us two")

    # Bot is a member of main only: DM content invisible
    headers = as_bot(client)
    r = await client.get("/search", params={"q": "quokka"}, headers=headers)
    assert r.status_code == 200
    [result] = r.json()
    assert result["message"]["id"] == main_msg["id"]
    assert result["channel"] == {"id": main, "type": "main_feed", "name": "main"}

    # Explicitly add the bot to the DM: now both hits appear
    await add_member(dm, "bot", bot_id)
    r = await client.get("/search", params={"q": "quokka"}, headers=headers)
    assert {x["channel"]["id"] for x in r.json()} == {main, dm}


async def test_bot_search_excludes_privacy_flagged(client):
    await make_user("alice")
    bot_id = await make_bot()
    main = await main_feed_id()
    await add_member(main, "bot", bot_id)

    await login(client, "alice")
    plain = await post_message(client, main, "wombat facts, plain and public")
    await post_message(client, main, "wombat launch codes", privacy_flags={"secret": True})
    await post_message(
        client, main, "wombat gossip", privacy_flags={"off_the_record": True}
    )

    # User search sees all three
    r = await client.get("/search", params={"q": "wombat"})
    assert len(r.json()) == 3

    # Bot search sees only the unflagged one — no secret / off_the_record
    headers = as_bot(client)
    r = await client.get("/search", params={"q": "wombat"}, headers=headers)
    [result] = r.json()
    assert result["message"]["id"] == plain["id"]
    assert result["message"]["content"] == "wombat facts, plain and public"


async def test_search_date_range_filtering_both_actors(client):
    await make_user("alice")
    bot_id = await make_bot()
    main = await main_feed_id()
    await add_member(main, "bot", bot_id)

    await login(client, "alice")
    m1 = await post_message(client, main, "ocelot report one")
    m2 = await post_message(client, main, "ocelot report two")
    m3 = await post_message(client, main, "ocelot report three")
    await _set_created_at(m1["id"], "2026-01-01T10:00:00.000Z")
    await _set_created_at(m2["id"], "2026-01-02T10:00:00.000Z")
    await _set_created_at(m3["id"], "2026-01-03T10:00:00.000Z")

    async def ids(params, headers=None):
        r = await client.get("/search", params=params, headers=headers or {})
        assert r.status_code == 200, r.text
        return [x["message"]["id"] for x in r.json()]

    # User: after is inclusive on the raw string bound, before is exclusive
    assert set(await ids({"q": "ocelot", "after": "2026-01-02"})) == {m2["id"], m3["id"]}
    assert await ids({"q": "ocelot", "before": "2026-01-02"}) == [m1["id"]]
    assert await ids({"q": "ocelot", "after": "2026-01-02", "before": "2026-01-03"}) == [m2["id"]]
    # Full timestamps work too
    assert await ids({"q": "ocelot", "after": "2026-01-03T09:00:00"}) == [m3["id"]]

    # Bot: same semantics through the API-key path
    headers = as_bot(client)
    assert set(await ids({"q": "ocelot", "after": "2026-01-02"}, headers)) == {m2["id"], m3["id"]}
    assert await ids({"q": "ocelot", "before": "2026-01-02"}, headers) == [m1["id"]]


async def test_search_garbage_dates_400(client):
    await make_user("alice")
    bot_id = await make_bot()
    main = await main_feed_id()
    await add_member(main, "bot", bot_id)
    await login(client, "alice")

    for params in (
        {"q": "x", "after": "not-a-date"},
        {"q": "x", "before": "yesterday-ish"},
        {"q": "x", "after": "2026-13-99"},
        {"q": "", "after": "garbage"},  # validated even when q is empty
    ):
        r = await client.get("/search", params=params)
        assert r.status_code == 400, f"{params} -> {r.status_code}"

    # Bot path gets the same validation
    headers = as_bot(client)
    r = await client.get("/search", params={"q": "x", "after": "junk"}, headers=headers)
    assert r.status_code == 400


async def test_search_survives_weird_fts_input(client):
    await make_user("alice")
    await login(client, "alice")
    main = await main_feed_id()
    await post_message(client, main, 'she said "hello there" (loudly) AND left')

    weird = [
        '"unbalanced',
        "foo AND (bar",
        "(((",
        "NEAR NEAR",
        "*",
        'a"b',
        "hello OR",
        "-",
        '""',
    ]
    for q in weird:
        r = await client.get("/search", params={"q": q})
        assert r.status_code == 200, f"q={q!r} -> {r.status_code}: {r.text}"

    # Operators are treated as literal terms, quoting still finds real words
    r = await client.get("/search", params={"q": "hello (loudly)"})
    assert len(r.json()) == 1
    # Empty / whitespace-only queries return nothing, no error
    assert (await client.get("/search", params={"q": ""})).json() == []
    assert (await client.get("/search", params={"q": "   "})).json() == []


# ---------------------------------------------------------------------------
# BL-D6 — message content length cap
# ---------------------------------------------------------------------------

async def test_message_content_length_cap(client):
    # Regression (BL-D6): MessageCreate.content had no max_length, so a 2MB
    # body was stored verbatim (and FTS-indexed, and fanned out on the bus).
    await make_user("alice")
    await login(client, "alice")
    ch = await main_feed_id()

    over = await client.post(
        f"/channels/{ch}/messages", json={"content": "x" * (MAX_MESSAGE_CHARS + 1)}
    )
    assert over.status_code == 422
    assert await db.fetch_all("SELECT * FROM messages") == []  # nothing persisted

    # Two megabytes — the shape the finding called out.
    huge = await client.post(f"/channels/{ch}/messages", json={"content": "x" * 2_000_000})
    assert huge.status_code == 422
    assert await db.fetch_all("SELECT * FROM messages") == []


async def test_message_content_at_cap_is_accepted(client):
    await make_user("alice")
    await login(client, "alice")
    ch = await main_feed_id()

    r = await client.post(
        f"/channels/{ch}/messages", json={"content": "x" * MAX_MESSAGE_CHARS}
    )
    assert r.status_code == 200
    assert len(r.json()["content"]) == MAX_MESSAGE_CHARS


async def test_cap_does_not_break_long_bot_posts(client):
    """A resident's build report is legitimately long — 4x the harness's
    file-proposal cap (3900 chars) must still go through."""
    bot_id = await make_bot()
    ch = await main_feed_id()
    await add_member(ch, "bot", bot_id)

    report = "## build report\n" + ("a long paragraph of narration. " * 400)
    assert 12_000 < len(report) < MAX_MESSAGE_CHARS
    r = await client.post(
        f"/channels/{ch}/messages", json={"content": report}, headers=as_bot(client)
    )
    assert r.status_code == 200
    assert r.json()["content"] == report


async def test_message_edit_content_length_cap(client):
    """An edit must not be a way around the create cap."""
    await make_user("alice")
    await login(client, "alice")
    ch = await main_feed_id()
    msg = (await client.post(f"/channels/{ch}/messages", json={"content": "hi"})).json()

    r = await client.patch(
        f"/messages/{msg['id']}", json={"content": "x" * (MAX_MESSAGE_CHARS + 1)}
    )
    assert r.status_code == 422
    row = await db.fetch_one("SELECT content, edited_at FROM messages WHERE id = ?", (msg["id"],))
    assert row["content"] == "hi" and row["edited_at"] is None


async def test_message_metadata_json_is_bounded(client):
    """The content cap must not leave emote_refs/privacy_flags as a side channel."""
    bot_id = await make_bot()
    ch = await main_feed_id()
    await add_member(ch, "bot", bot_id)
    hdrs = as_bot(client)

    over = await client.post(
        f"/channels/{ch}/messages",
        json={"content": "hi", "emote_refs": ["x" * (MAX_METADATA_CHARS + 10)]},
        headers=hdrs,
    )
    assert over.status_code == 422
    over = await client.post(
        f"/channels/{ch}/messages",
        json={"content": "hi", "privacy_flags": {"secret": "y" * (MAX_METADATA_CHARS + 10)}},
        headers=hdrs,
    )
    assert over.status_code == 422
    assert await db.fetch_all("SELECT * FROM messages") == []

    # Realistic metadata still goes through untouched.
    ok = await client.post(
        f"/channels/{ch}/messages",
        json={
            "content": "hi",
            "emote_refs": ["chibi:claudette/Happy_and_Confident/Smug.png"],
            "privacy_flags": {"secret": True},
        },
        headers=hdrs,
    )
    assert ok.status_code == 200
    assert ok.json()["emote_refs"] == ["chibi:claudette/Happy_and_Confident/Smug.png"]
