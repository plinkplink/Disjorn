"""`@bots`, and the `mentioned_bot_ids` list every message carries."""

import json
from contextlib import ExitStack

import pytest

from app import db, mentions
from app.config import reset_settings_cache
from app.routers import auth

from .test_ws import (
    BOT2_KEY,
    BOT_KEY,
    add_member,
    call,
    cookie,
    login,
    main_feed_id,
    make_bot,
    make_user,
    open_bot,
    open_user,
    post_msg,
    wsc,  # noqa: F401 — fixture
)

BOT3_KEY = "ws-bot-key-3"
BOT4_KEY = "ws-bot-key-4"


def bot_post(wsc, api_key, channel_id, content, **extra):
    r = wsc.post(
        f"/channels/{channel_id}/messages",
        json={"content": content, **extra},
        headers={"X-Api-Key": api_key},
    )
    assert r.status_code == 200, r.text
    return r.json()


def room(wsc):
    """alice, and three bots: claudette and otto in main, nemo in no channel."""
    a = make_user(wsc, "alice")
    token = login(wsc, "alice")
    claudette = make_bot(wsc, "claudette", BOT_KEY)
    otto = make_bot(wsc, "otto", BOT2_KEY)
    nemo = make_bot(wsc, "nemo", BOT3_KEY)
    main = main_feed_id(wsc)
    add_member(wsc, main, "bot", claudette)
    add_member(wsc, main, "bot", otto)
    return a, token, claudette, otto, nemo, main


def set_mode(wsc, channel_id, bot_id, mode):
    call(
        wsc, db.execute,
        "UPDATE channel_members SET reply_mode = ? WHERE channel_id = ? AND member_type = 'bot' AND member_id = ?",
        (mode, channel_id, bot_id),
    )


# ---------------------------------------------------------------------------
# A person's @bots
# ---------------------------------------------------------------------------

def test_a_persons_at_bots_addresses_every_member_bot_exactly_once(wsc):
    a, ta, claudette, otto, nemo, main = room(wsc)

    with ExitStack() as stack:
        wa = open_user(stack, wsc, ta, a)
        wcl = open_bot(stack, wsc, claudette, BOT_KEY)
        wot = open_bot(stack, wsc, otto, BOT2_KEY)
        wne = open_bot(stack, wsc, nemo, BOT3_KEY)

        posted = post_msg(wsc, ta, main, "@bots what is the state of the build?")
        assert posted["mentioned_bot_ids"] == [claudette, otto]
        human = wa.receive_json()
        assert human["message"]["mentioned_bot_ids"] == [claudette, otto]
        assert "context" not in human

        post_msg(wsc, ta, main, "never mind")
        for ws in (wcl, wot):
            frame = ws.receive_json()
            assert frame["message"]["mentioned_bot_ids"] == [claudette, otto]
            assert frame["context"]["addressed_by"] == "mention"
            after = ws.receive_json()
            assert after["message"]["content"] == "never mind"
            assert "context" not in after

        add_member(wsc, main, "bot", nemo)
        post_msg(wsc, ta, main, "nemo is here now")
        assert wne.receive_json()["message"]["content"] == "nemo is here now"


def test_both_reply_modes_answer_at_bots_and_a_mention_only_room_allows_it(wsc, monkeypatch):
    a, ta, claudette, otto, _nemo, main = room(wsc)
    set_mode(wsc, main, claudette, "always")

    with ExitStack() as stack:
        open_user(stack, wsc, ta, a)
        wcl = open_bot(stack, wsc, claudette, BOT_KEY)
        wot = open_bot(stack, wsc, otto, BOT2_KEY)

        post_msg(wsc, ta, main, "@bots roll call")
        assert wcl.receive_json()["context"]["addressed_by"] == "mention"
        assert wot.receive_json()["context"]["addressed_by"] == "mention"

        monkeypatch.setenv("MENTION_ONLY_CHANNEL_IDS", json.dumps([main]))
        reset_settings_cache()
        post_msg(wsc, ta, main, "@bots roll call")
        assert wcl.receive_json()["context"]["addressed_by"] == "mention"
        assert wot.receive_json()["context"]["addressed_by"] == "mention"


def test_at_bots_in_a_private_channel_reaches_only_the_bots_in_it(wsc):
    a, ta, claudette, otto, _nemo, main = room(wsc)
    r = wsc.post("/channels", json={"name": "den", "visibility": "private"}, headers=cookie(ta))
    den = r.json()["id"]
    add_member(wsc, den, "bot", claudette)

    with ExitStack() as stack:
        open_user(stack, wsc, ta, a)
        wcl = open_bot(stack, wsc, claudette, BOT_KEY)
        wot = open_bot(stack, wsc, otto, BOT2_KEY)

        assert post_msg(wsc, ta, den, "@bots a private word")["mentioned_bot_ids"] == [claudette]
        frame = wcl.receive_json()
        assert frame["channel_id"] == den and frame["context"]["addressed_by"] == "mention"

        post_msg(wsc, ta, main, "hello main")
        assert wot.receive_json()["message"]["content"] == "hello main"


def test_an_account_that_only_posts_is_left_out_of_at_bots_but_not_of_its_own_name(wsc):
    a, ta, claudette, otto, _nemo, main = room(wsc)
    keyboard = make_bot(wsc, "BuildGable", BOT4_KEY)
    add_member(wsc, main, "bot", keyboard)

    with ExitStack() as stack:
        open_user(stack, wsc, ta, a)
        wkb = open_bot(stack, wsc, keyboard, BOT4_KEY)

        assert post_msg(wsc, ta, main, "@bots roll call")["mentioned_bot_ids"] == [claudette, otto]
        assert "context" not in wkb.receive_json()

        named = post_msg(wsc, ta, main, "@bots and @BuildGable too")
        assert named["mentioned_bot_ids"] == [claudette, otto, keyboard]
        assert wkb.receive_json()["context"]["addressed_by"] == "mention"


def test_a_secret_at_bots_lists_nobody_and_reaches_no_bot(wsc):
    a, ta, claudette, _otto, _nemo, main = room(wsc)

    with ExitStack() as stack:
        open_user(stack, wsc, ta, a)
        wcl = open_bot(stack, wsc, claudette, BOT_KEY)
        secret = post_msg(wsc, ta, main, "don't tell anyone, @bots")
        assert secret["privacy_flags"] == {"secret": True}
        assert secret["mentioned_bot_ids"] == []
        post_msg(wsc, ta, main, "all clear")
        assert wcl.receive_json()["message"]["content"] == "all clear"


# ---------------------------------------------------------------------------
# A bot's @bots
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("content", [
    "@bots wake up",
    "`@bots` is how a person calls us",
    'plink said "@bots status"',
    "> @bots status\nquoting plink",
])
def test_a_bots_at_bots_addresses_nobody(wsc, content):
    a, ta, claudette, otto, _nemo, main = room(wsc)
    set_mode(wsc, main, claudette, "always")

    with ExitStack() as stack:
        open_user(stack, wsc, ta, a)
        wcl = open_bot(stack, wsc, claudette, BOT_KEY)
        wot = open_bot(stack, wsc, otto, BOT2_KEY)

        posted = bot_post(wsc, BOT2_KEY, main, content)
        assert posted["mentioned_bot_ids"] == []
        for ws in (wcl, wot):
            frame = ws.receive_json()
            assert frame["message"]["mentioned_bot_ids"] == []
            assert "context" not in frame


def test_the_list_is_never_taken_from_the_request(wsc):
    _a, ta, claudette, otto, _nemo, main = room(wsc)
    forged = bot_post(wsc, BOT2_KEY, main, "hello", mentioned_bot_ids=[claudette, otto])
    assert forged["mentioned_bot_ids"] == []
    r = wsc.post(
        f"/channels/{main}/messages",
        json={"content": "hello", "mentioned_bot_ids": [claudette]},
        headers=cookie(ta),
    )
    assert r.json()["mentioned_bot_ids"] == []


# ---------------------------------------------------------------------------
# @name fills the same list
# ---------------------------------------------------------------------------

def test_a_named_at_lists_that_bot_for_a_person_and_for_a_bot(wsc):
    a, ta, claudette, otto, _nemo, main = room(wsc)

    with ExitStack() as stack:
        open_user(stack, wsc, ta, a)
        wcl = open_bot(stack, wsc, claudette, BOT_KEY)

        assert post_msg(wsc, ta, main, "hey @Claudette, a word")["mentioned_bot_ids"] == [claudette]
        assert wcl.receive_json()["context"]["addressed_by"] == "mention"

        assert bot_post(wsc, BOT2_KEY, main, "@claudette look")["mentioned_bot_ids"] == [claudette]
        assert wcl.receive_json()["context"]["addressed_by"] == "mention"

        bare = post_msg(wsc, ta, main, "maybe claudette knows")
        assert bare["mentioned_bot_ids"] == []
        assert wcl.receive_json()["context"]["addressed_by"] == "mention"

        assert post_msg(wsc, ta, main, "@nemo are you there")["mentioned_bot_ids"] == []
        assert "context" not in wcl.receive_json()


# ---------------------------------------------------------------------------
# What counts as writing it
# ---------------------------------------------------------------------------

@pytest.fixture
async def main_with_two_bots(app):
    main = (await db.fetch_one("SELECT id FROM channels WHERE type = 'main_feed'"))["id"]
    ids = []
    for name in ("claudette", "otto"):
        cur = await db.execute(
            "INSERT INTO bots (name, api_key_hash) VALUES (?, ?)", (name, auth.hash_api_key(name))
        )
        ids.append(cur.lastrowid)
        await db.execute(
            "INSERT INTO channel_members (channel_id, member_type, member_id) VALUES (?, 'bot', ?)",
            (main, cur.lastrowid),
        )
    return main, ids


@pytest.mark.parametrize("content", [
    "@bots",
    "@Bots, status?",
    "ok (@BOTS) go",
    "> @bots status\nasking again",
    'he typed "@bots" and so do I',
    "first line\n@bots!",
])
async def test_a_person_writing_it_in_prose_or_a_quote_calls_everyone(main_with_two_bots, content):
    main, ids = main_with_two_bots
    assert await mentions.mentioned_bot_ids(main, "user", content) == ids


@pytest.mark.parametrize("content", [
    "type `@bots` to call them all",
    "```\n@bots\n```",
    "```text\nsay @bots here\n``` and `@claudette` there",
    "@botswana",
    "bots, status?",
    "mail me at plink@bots",
    "@ bots",
])
async def test_code_and_near_misses_call_nobody(main_with_two_bots, content):
    main, _ids = main_with_two_bots
    assert await mentions.mentioned_bot_ids(main, "user", content) == []


async def test_who_at_bots_leaves_out_is_a_setting(main_with_two_bots, monkeypatch):
    main, ids = main_with_two_bots
    monkeypatch.setenv("GROUP_MENTION_SKIP_BOT_NAMES", json.dumps(["OTTO"]))
    reset_settings_cache()
    assert await mentions.mentioned_bot_ids(main, "user", "@bots") == [ids[0]]
    assert await mentions.mentioned_bot_ids(main, "user", "@bots and @otto") == ids


async def test_an_unclosed_code_span_hides_nothing(main_with_two_bots):
    main, ids = main_with_two_bots
    assert await mentions.mentioned_bot_ids(main, "user", "a stray ` and then @bots") == ids
    assert await mentions.mentioned_bot_ids(main, "user", "``` never closed\n@otto") == [ids[1]]


# ---------------------------------------------------------------------------
# The stored fact
# ---------------------------------------------------------------------------

def test_history_carries_the_list_and_an_edit_never_changes_it(wsc):
    _a, ta, claudette, otto, _nemo, main = room(wsc)
    called = post_msg(wsc, ta, main, "@bots hello")
    plain = post_msg(wsc, ta, main, "hello")

    r = wsc.patch(f"/messages/{plain['id']}", json={"content": "@bots hello"}, headers=cookie(ta))
    assert r.status_code == 200 and r.json()["mentioned_bot_ids"] == []
    r = wsc.patch(f"/messages/{called['id']}", json={"content": "hello"}, headers=cookie(ta))
    assert r.json()["mentioned_bot_ids"] == [claudette, otto]

    history = wsc.get(f"/channels/{main}/messages", headers=cookie(ta)).json()
    assert {m["id"]: m["mentioned_bot_ids"] for m in history} == {
        called["id"]: [claudette, otto],
        plain["id"]: [],
    }


async def test_the_migration_gives_existing_rows_an_empty_list(tmp_path, monkeypatch):
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
            if path.name == "024_message_mentions.sql":
                continue
            await conn.executescript(path.read_text(encoding="utf-8"))
            await conn.execute(
                "INSERT INTO schema_migrations (filename, applied_at) VALUES (?, ?)",
                (path.name, db.utc_now()))
        await conn.commit()
        await db.execute("INSERT INTO channels (type, name) VALUES ('text', 'c')")
        await db.execute(
            "INSERT INTO messages (channel_id, seq, author_type, author_id, content)"
            " VALUES (1, 1, 'user', 1, '@bots from before')")

        assert await db.run_migrations() == ["024_message_mentions.sql"]

        row = await db.fetch_one("SELECT * FROM messages WHERE id = 1")
        assert (await message_payload(row))["mentioned_bot_ids"] == []
    finally:
        await db.close()
        reset_settings_cache()
