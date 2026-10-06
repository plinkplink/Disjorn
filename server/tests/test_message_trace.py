"""Message trace (SPECS/2026-10-06-why-trail.md): a bot-only list of steps
beside the body, shape and size checked, on every payload a reader sees."""

import json

from app import db
from app.routers.messages import (
    MAX_METADATA_CHARS,
    MAX_TRACE_CHARS,
    MAX_TRACE_LABEL_CHARS,
    MAX_TRACE_STEPS,
)
from tests.test_messages import (
    add_member,
    as_bot,
    capture_events,
    login,
    main_feed_id,
    make_bot,
    make_user,
)

STEP = {"kind": "read", "label": "read_repo_file server/app/ws.py",
        "outcome": "ok", "reason": None, "ms": 41}
REFUSED = {"kind": "broker", "label": "broker changed-files main...loop/x",
           "outcome": "refused", "reason": "refused-by-broker", "ms": 7}
TRACE = {"steps": [STEP, REFUSED], "total": 2}


async def _bot_in_main(client) -> tuple[int, dict[str, str]]:
    bot_id = await make_bot("gable")
    main = await main_feed_id()
    await add_member(main, "bot", bot_id)
    return main, as_bot(client)


async def _post(client, main, hdrs, trace, content="the zanzibar reply"):
    return await client.post(f"/channels/{main}/messages",
                             json={"content": content, "trace": trace},
                             headers=hdrs)


async def test_a_bot_trace_round_trips_through_list_backfill_search_and_bus(client):
    main, hdrs = await _bot_in_main(client)
    captured = capture_events()

    r = await _post(client, main, hdrs, TRACE)
    assert r.status_code == 200, r.text
    assert r.json()["trace"] == TRACE
    assert [e["message"]["trace"] for e in captured] == [TRACE]

    scrollback = (await client.get(f"/channels/{main}/messages",
                                   headers=hdrs)).json()
    assert [m["trace"] for m in scrollback] == [TRACE]
    backfill = (await client.get(f"/channels/{main}/messages",
                                 params={"from_seq": 1}, headers=hdrs)).json()
    assert [m["trace"] for m in backfill] == [TRACE]
    [hit] = (await client.get("/search", params={"q": "zanzibar"},
                              headers=hdrs)).json()
    assert hit["message"]["trace"] == TRACE


async def test_an_edit_keeps_the_trace(client):
    main, hdrs = await _bot_in_main(client)
    msg = (await _post(client, main, hdrs, TRACE)).json()
    r = await client.patch(f"/messages/{msg['id']}", json={"content": "fixed"},
                           headers=hdrs)
    assert r.status_code == 200, r.text
    assert r.json()["trace"] == TRACE


async def test_a_cut_list_keeps_its_true_total(client):
    main, hdrs = await _bot_in_main(client)
    trace = {"steps": [STEP] * MAX_TRACE_STEPS, "total": 73}
    r = await _post(client, main, hdrs, trace)
    assert r.status_code == 200, r.text
    assert r.json()["trace"]["total"] == 73


async def test_a_message_without_a_trace_reads_null(client):
    main, hdrs = await _bot_in_main(client)
    r = await client.post(f"/channels/{main}/messages", json={"content": "x"},
                          headers=hdrs)
    assert r.json()["trace"] is None
    r = await _post(client, main, hdrs, {"steps": [], "total": 0})
    assert r.json()["trace"] is None


async def test_a_user_trace_is_dropped(client):
    await make_user("alice")
    await login(client, "alice")
    main = await main_feed_id()
    r = await client.post(f"/channels/{main}/messages",
                          json={"content": "hi", "trace": TRACE})
    assert r.status_code == 200
    assert r.json()["trace"] is None
    row = await db.fetch_one("SELECT trace FROM messages WHERE id = ?",
                             (r.json()["id"],))
    assert row["trace"] == "{}"


async def test_a_malformed_trace_is_422_and_stores_nothing(client):
    main, hdrs = await _bot_in_main(client)
    bad = [
        {**TRACE, "model": "x"},
        {"steps": [{**STEP, "output": "file body"}], "total": 1},
        {"steps": [{**STEP, "kind": "telepathy"}], "total": 1},
        {"steps": [{**STEP, "outcome": "maybe"}], "total": 1},
        {"steps": [{**STEP, "reason": "outside the repo"}], "total": 1},
        {"steps": [{**STEP, "ms": -1}], "total": 1},
        {"steps": [{**STEP, "ms": "41"}], "total": 1},
        {"steps": [{**STEP, "label": "x" * (MAX_TRACE_LABEL_CHARS + 1)}],
         "total": 1},
        {"steps": [STEP] * (MAX_TRACE_STEPS + 1), "total": MAX_TRACE_STEPS + 1},
        {"steps": [STEP, STEP], "total": 1},
        {"steps": [STEP]},
    ]
    for trace in bad:
        r = await _post(client, main, hdrs, trace)
        assert r.status_code == 422, trace
    assert await db.fetch_all("SELECT * FROM messages") == []


async def test_the_trace_has_its_own_budget(client):
    main, hdrs = await _bot_in_main(client)
    step = {**REFUSED, "label": "x" * MAX_TRACE_LABEL_CHARS, "ms": 10**9}
    under = {"steps": [step] * 40, "total": 40}
    assert MAX_METADATA_CHARS < len(json.dumps(under)) <= MAX_TRACE_CHARS
    r = await _post(client, main, hdrs, under)
    assert r.status_code == 200, r.text

    over = {"steps": [step] * MAX_TRACE_STEPS, "total": MAX_TRACE_STEPS}
    assert len(json.dumps(over)) > MAX_TRACE_CHARS
    r = await _post(client, main, hdrs, over)
    assert r.status_code == 200, r.text
    assert r.json()["trace"] is None and r.json()["content"]


async def test_migration_020_gives_existing_rows_no_trace(tmp_path, monkeypatch):
    from app.config import reset_settings_cache
    from app.routers.messages import message_payload

    monkeypatch.setenv("DB_PATH", str(tmp_path / "old.db"))
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    reset_settings_cache()
    await db.close()
    conn = await db.connect()
    try:
        await conn.execute("""CREATE TABLE schema_migrations (
            filename TEXT PRIMARY KEY, applied_at TEXT NOT NULL)""")
        for path in sorted(db.MIGRATIONS_DIR.glob("*.sql")):
            if path.name == "020_message_trace.sql":
                continue
            await conn.executescript(path.read_text(encoding="utf-8"))
            await conn.execute(
                "INSERT INTO schema_migrations (filename, applied_at) VALUES (?, ?)",
                (path.name, db.utc_now()))
        await conn.commit()
        await db.execute("INSERT INTO channels (type, name) VALUES ('text', 'c')")
        await db.execute(
            "INSERT INTO messages (channel_id, seq, author_type, author_id, content)"
            " VALUES (1, 1, 'bot', 2, 'old body')")

        assert await db.run_migrations() == ["020_message_trace.sql"]

        row = await db.fetch_one("SELECT * FROM messages WHERE id = 1")
        assert row["trace"] == "{}"
        assert (await message_payload(row))["trace"] is None
    finally:
        await db.close()
        reset_settings_cache()


async def test_a_reason_on_an_ok_step_and_an_absurd_total_are_refused(client):
    main, hdrs = await _bot_in_main(client)
    ok_with_reason = {"kind": "read", "label": "a", "outcome": "ok", "reason": "timeout", "ms": 1}
    for trace in ({"steps": [ok_with_reason], "total": 1},
                  {"steps": [], "total": 10**9}):
        r = await _post(client, main, hdrs, trace)
        assert r.status_code == 422, trace
