"""Channel files for bots, slice A: the files_safe flag, the one visibility
filter, the bot listing on every message read path, and the read shapes."""

import asyncio
import base64
import io
import json
import logging

import httpx
import pytest
from PIL import Image
from pypdf import PdfWriter

from app import db
from app import ws as ws_module
from app.config import get_settings, reset_settings_cache
from app.routers import auth, slash
from app.services import bot_files

PASSWORD = "correct horse battery staple"
BOT_KEY = "bot-key-1"
CANARY = "hunter2"
MIB = 1024 * 1024


@pytest.fixture(autouse=True)
def reset_rate_limit():
    slash.init()
    yield
    slash.init()


class Wire:
    """Stands in for a bot's socket: keeps every frame the hub sends it."""

    def __init__(self) -> None:
        self.frames: list[dict] = []

    async def send_json(self, frame: dict) -> None:
        self.frames.append(frame)


class House:
    def __init__(self, client, bot, wire, bot_id, room) -> None:
        self.client, self.bot, self.wire = client, bot, wire
        self.bot_id, self.room = bot_id, room

    async def say(self, content: str, *files, flags=None, channel=None) -> tuple[dict, list[int]]:
        channel = channel or self.room
        r = await self.client.post(
            f"/channels/{channel}/messages",
            json={"content": content, "privacy_flags": flags or {}},
        )
        assert r.status_code == 200, r.text
        message = r.json()
        return message, [await self.attach(message["id"], *f) for f in files]

    async def attach(self, message_id: int, name: str, content: bytes, mime: str) -> int:
        r = await self.client.post(
            "/upload", files=[("files", (name, content, mime))],
            data={"message_id": str(message_id)},
        )
        assert r.status_code == 200, r.text
        return r.json()["attachments"][0]["id"]

    async def flip(self, *words: str, channel: str = "files") -> None:
        await asyncio.sleep(0.005)
        await self.say(f"/files-safe #{channel} {' '.join(words)}")
        await asyncio.sleep(0.005)

    async def read(self, attachment_id: int, key: str = BOT_KEY, **params) -> httpx.Response:
        return await self.bot.get(
            f"/bot/attachments/{attachment_id}", params=params, headers={"X-Api-Key": key})

    async def add_bot(self, name: str, key: str, *channels: int) -> int:
        cur = await db.execute(
            "INSERT INTO bots (name, api_key_hash) VALUES (?, ?)",
            (name, auth.hash_api_key(key)))
        for channel_id in channels:
            await self.join(cur.lastrowid, channel_id)
        return cur.lastrowid

    async def join(self, bot_id: int, channel_id: int) -> None:
        await db.execute(
            "INSERT INTO channel_members (channel_id, member_type, member_id)"
            " VALUES (?, 'bot', ?)", (channel_id, bot_id))

    async def listed(self, channel=None, **params) -> list[dict]:
        r = await self.bot.get(f"/channels/{channel or self.room}/messages", params=params)
        assert r.status_code == 200, r.text
        return r.json()

    async def listing(self, message_id: int):
        (message,) = [m for m in await self.listed() if m["id"] == message_id]
        return message.get("attachments", "absent")

    async def seen_by_user(self, message_id: int) -> dict:
        r = await self.client.get(f"/channels/{self.room}/messages")
        return next(m for m in r.json() if m["id"] == message_id)


@pytest.fixture
async def house(app, client):
    await db.execute(
        "INSERT INTO users (username, password_hash, display_name, is_admin)"
        " VALUES ('ada', ?, 'Ada', 1)", (auth.hash_password(PASSWORD),))
    cur = await db.execute(
        "INSERT INTO bots (name, api_key_hash) VALUES ('claw', ?)",
        (auth.hash_api_key(BOT_KEY),))
    bot_id = cur.lastrowid
    r = await client.post("/auth/login", json={"username": "ada", "password": PASSWORD})
    assert r.status_code == 200
    room = (await client.post("/channels", json={"name": "files"})).json()["id"]
    await db.execute(
        "INSERT INTO channel_members (channel_id, member_type, member_id)"
        " VALUES (?, 'bot', ?)", (room, bot_id))
    wire = Wire()
    ws_module.manager.connect_bot(wire, bot_id)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="https://test", headers={"X-Api-Key": BOT_KEY}
    ) as bot:
        yield House(client, bot, wire, bot_id, room)
    ws_module.manager.disconnect_bot(wire, bot_id)


def txt(name: str = "notes.txt", body: bytes = b"plain notes") -> tuple[str, bytes, str]:
    return name, body, "text/plain"


def png(size=(64, 64)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, (200, 30, 30)).save(buf, format="PNG")
    return buf.getvalue()


def noisy_png(size) -> bytes:
    buf = io.BytesIO()
    Image.effect_noise(size, 12).convert("RGB").save(buf, format="PNG")
    return buf.getvalue()


def pdf(*pages: str) -> bytes:
    kids = " ".join(f"{4 + 2 * i} 0 R" for i in range(len(pages)))
    objs = [
        "<< /Type /Catalog /Pages 2 0 R >>",
        f"<< /Type /Pages /Kids [{kids}] /Count {len(pages)} >>",
        "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    for i, text in enumerate(pages):
        stream = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET"
        objs.append(
            "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792]"
            f" /Resources << /Font << /F1 3 0 R >> >> /Contents {5 + 2 * i} 0 R >>")
        objs.append(f"<< /Length {len(stream)} >>\nstream\n{stream}\nendstream")
    out, offsets = b"%PDF-1.4\n", []
    for number, body in enumerate(objs, 1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n{body}\nendobj\n".encode()
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode()
    out += b"".join(f"{off:010d} 00000 n \n".encode() for off in offsets)
    out += f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\n".encode()
    return out + f"startxref\n{xref}\n%%EOF\n".encode()


def blank_pdf(pages: int) -> bytes:
    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=200, height=200)
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


def attachment_ids(payloads) -> set[int]:
    return {a["id"] for m in payloads for a in m.get("attachments", [])}


def set_env(monkeypatch, key: str, value: int) -> None:
    monkeypatch.setenv(key, str(value))
    reset_settings_cache()


async def row(attachment_id: int) -> dict:
    return await db.fetch_one("SELECT * FROM attachments WHERE id = ?", (attachment_id,))


async def channel(channel_id: int) -> dict:
    return await db.fetch_one("SELECT * FROM channels WHERE id = ?", (channel_id,))


# ---------------------------------------------------------------- migration

async def test_migration_023_lands_on_an_earlier_database_with_everything_off(
    tmp_path, tmp_db_path, monkeypatch
):
    earlier = tmp_path / "migrations"
    earlier.mkdir()
    for path in db.MIGRATIONS_DIR.glob("*.sql"):
        if path.name != "023_channel_files_for_bots.sql":
            (earlier / path.name).write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
    real = db.MIGRATIONS_DIR
    await db.close()
    await db.connect()
    try:
        monkeypatch.setattr(db, "MIGRATIONS_DIR", earlier)
        assert "021_password_reset_requests.sql" in await db.run_migrations()
        await db.execute("INSERT INTO channels (type, name) VALUES ('text', 'old')")
        cur = await db.execute(
            "INSERT INTO messages (channel_id, seq, author_type, author_id, content)"
            " VALUES ((SELECT id FROM channels WHERE name = 'old'), 1, 'bot', 1, 'x')")
        await db.execute(
            "INSERT INTO attachments (message_id, file_path, original_filename,"
            " mime_type, size_bytes) VALUES (?, 'uploads/originals/1_a.txt', 'a.txt',"
            " 'text/plain', 3)", (cur.lastrowid,))

        monkeypatch.setattr(db, "MIGRATIONS_DIR", real)
        assert await db.run_migrations() == ["023_channel_files_for_bots.sql"]

        old = await db.fetch_one("SELECT * FROM channels WHERE name = 'old'")
        assert (old["files_safe"], old["files_safe_since"]) == (0, None)
        att = await db.fetch_one("SELECT * FROM attachments")
        assert att["bot_history_ok"] == 0
        assert await bot_files.visible_to_bots(att) is False
    finally:
        await db.close()


# ----------------------------------------------------------- the admin verb

async def test_a_channel_is_closed_to_bots_until_an_admin_opens_it(house):
    message, (att,) = await house.say("here", txt())

    assert await house.listing(message["id"]) == "absent"
    assert (await house.read(att)).status_code == 404
    assert all("attachments" not in f["message"] for f in house.wire.frames
               if "message" in f)
    human = await house.seen_by_user(message["id"])
    assert human["attachments"][0]["orig_url"].startswith(f"/media/{att}?")


async def test_only_an_admin_may_flip_the_flag(house, client):
    await db.execute(
        "INSERT INTO users (username, password_hash, display_name) VALUES ('bo', ?, 'Bo')",
        (auth.hash_password(PASSWORD),))
    await client.post("/auth/login", json={"username": "bo", "password": PASSWORD})
    await house.say("/files-safe #files on")
    r = await house.bot.post(f"/channels/{house.room}/messages",
                             json={"content": "/files-safe #files on"})
    assert r.status_code == 200

    assert (await channel(house.room))["files_safe"] == 0
    refusals = [m["content"] for m in await house.listed()
                if m["content"] == slash.FILES_SAFE_NOT_ADMIN]
    assert len(refusals) == 2


@pytest.mark.parametrize("args", [
    "", "#files", "#files maybe", "#files off --include-history",
    "#files on --include-history extra", "#files on --all",
])
async def test_a_malformed_command_changes_nothing(house, args):
    await house.say(f"/files-safe {args}")
    assert (await channel(house.room))["files_safe"] == 0
    assert (await house.listed())[0]["content"] == slash.FILES_SAFE_USAGE


async def test_only_main_and_named_text_channels_can_be_named(house):
    await db.execute("INSERT INTO channels (type, name, visibility)"
                     " VALUES ('app_build', 'appy', 'private')")
    for name in ("nowhere", "appy"):
        await house.say(f"/files-safe #{name} on")
        assert (await house.listed())[0]["content"] == slash.FILES_SAFE_NO_CHANNEL
    assert not await db.fetch_all("SELECT id FROM channels WHERE files_safe = 1")

    await house.flip("on", channel="main")
    main = await db.fetch_one("SELECT * FROM channels WHERE type = 'main_feed'")
    assert main["files_safe"] == 1


async def test_each_change_posts_a_system_line_in_the_channel_it_exposes(house):
    main = (await db.fetch_one("SELECT id FROM channels WHERE type = 'main_feed'"))["id"]
    system = (await db.fetch_one("SELECT id FROM bots WHERE name = 'system'"))["id"]

    async def lines() -> list[str]:
        return [m["content"] for m in await house.listed(from_seq=0)
                if m.get("author_id") == system and m.get("author_type") == "bot"]

    await house.say("/files-safe #files on --include-history", channel=main)
    assert await lines() == [f"{slash.FILES_SAFE_ON} Set by ada.",
                             f"{slash.FILES_SAFE_HISTORY} Set by ada."]
    await house.say("/files-safe #files on", channel=main)
    await house.say("/files-safe #files off", channel=main)
    assert (await lines())[2:] == [f"{slash.FILES_SAFE_OFF} Set by ada."]
    await house.say("/files-safe #files off", channel=main)
    assert len(await lines()) == 3

    r = await house.client.get(f"/channels/{main}/messages")
    acks = [m["content"] for m in r.json() if m["author_type"] == "bot"]
    assert acks == ["#files already has files-safe off. Nothing was changed.",
                    "#files: files-safe off.",
                    "#files already has files-safe on. Nothing was changed.",
                    "#files: files-safe on --include-history."]


# ------------------------------------------------------------ retroactivity

async def test_opening_a_channel_exposes_uploads_from_that_moment_only(house):
    before, (old,) = await house.say("before", txt("old.txt"))
    await house.flip("on")
    after, (new,) = await house.say("after", txt("new.txt", b"fresh"))

    assert await house.listing(before["id"]) == []
    assert await house.listing(after["id"]) == [
        {"id": new, "mime_type": "text/plain", "size_bytes": 5}]
    assert (await house.read(old)).status_code == 404
    assert (await house.read(new)).json() == {
        "id": new, "mime_type": "text/plain", "size_bytes": 5,
        "kind": "text", "text": "fresh"}
    assert (await row(old))["bot_history_ok"] == 0


async def test_include_history_exposes_what_was_already_there(house):
    before, (old,) = await house.say("before", txt("old.txt"))
    await house.flip("on", "--include-history")

    assert attachment_ids([{"attachments": await house.listing(before["id"])}]) == {old}
    assert (await house.read(old)).status_code == 200


async def test_include_history_on_an_open_channel_leaves_its_since_alone(house):
    _, (old,) = await house.say("before", txt("old.txt"))
    await house.flip("on")
    since = (await channel(house.room))["files_safe_since"]
    _, (new,) = await house.say("after", txt("new.txt"))
    await house.flip("on", "--include-history")

    assert (await channel(house.room))["files_safe_since"] == since
    assert attachment_ids(await house.listed()) == {old, new}
    assert (await house.listed())[0]["content"] == f"{slash.FILES_SAFE_HISTORY} Set by ada."


async def test_off_then_on_hides_whatever_carries_no_history_mark(house):
    await house.flip("on")
    _, (first_window,) = await house.say("while on", txt("a.txt"))
    await house.flip("off")
    assert (await house.read(first_window)).status_code == 404
    assert (await channel(house.room))["files_safe_since"] is None
    _, (off_window,) = await house.say("while off", txt("b.txt"))
    await house.flip("on")
    _, (second_window,) = await house.say("on again", txt("c.txt"))

    assert (await house.read(off_window)).status_code == 404
    assert (await house.read(first_window)).status_code == 404
    assert (await house.read(second_window)).status_code == 200
    assert attachment_ids(await house.listed()) == {second_window}


async def test_a_history_mark_survives_off_and_on(house):
    _, (marked,) = await house.say("old", txt("old.txt"))
    await house.flip("on", "--include-history")
    await house.flip("off")
    _, (off_window,) = await house.say("while off", txt("b.txt"))
    assert (await house.read(marked)).status_code == 404
    await house.flip("on")

    assert (await row(marked))["bot_history_ok"] == 1
    assert (await house.read(marked)).status_code == 200
    assert (await house.read(off_window)).status_code == 404


# ------------------------------------------------------------------ privacy

async def _flagged(house, flags):
    return (await house.say("findme", txt(f"{CANARY}.txt", CANARY.encode()), flags=flags))[1][0]


async def _said_in_words(house):
    return (await house.say(
        "findme, keep this secret", txt(f"{CANARY}.txt", CANARY.encode())))[1][0]


async def _flagged_by_edit(house):
    message, (att,) = await house.say("findme", txt(f"{CANARY}.txt", CANARY.encode()))
    r = await house.client.patch(
        f"/messages/{message['id']}", json={"content": "findme, off the record"})
    assert r.status_code == 200
    return att


async def _deleted(house):
    message, (att,) = await house.say("findme", txt(f"{CANARY}.txt", CANARY.encode()))
    assert (await house.client.delete(f"/messages/{message['id']}")).status_code == 200
    return att


async def _before_the_flag(house):
    await house.flip("off")
    att = (await house.say("findme", txt(f"{CANARY}.txt", CANARY.encode())))[1][0]
    await house.flip("on")
    return att


async def _channel_closed_after(house):
    att = (await house.say("findme", txt(f"{CANARY}.txt", CANARY.encode())))[1][0]
    await house.flip("off")
    return att


HIDDEN = {
    "secret": lambda h: _flagged(h, {"secret": True}),
    "off_the_record": lambda h: _flagged(h, {"off_the_record": True}),
    "said_in_words": _said_in_words,
    "flagged_by_edit": _flagged_by_edit,
    "deleted": _deleted,
    "before_the_flag": _before_the_flag,
}
SURFACES = ("read", "scrollback", "backfill", "search")


@pytest.mark.parametrize("surface", SURFACES)
@pytest.mark.parametrize("hide", HIDDEN)
async def test_a_hidden_file_reaches_no_bot_surface(house, hide, surface):
    await house.flip("on")
    hidden = await HIDDEN[hide](house)
    _, (plain,) = await house.say("findme too", txt("plain.txt"))

    if surface == "read":
        answer = await house.read(hidden)
        assert answer.status_code == 404
        assert answer.json() == (await house.read(10_000)).json()
        assert (await house.read(plain)).status_code == 200
        return
    if surface == "search":
        r = await house.bot.get("/search", params={"q": "findme"})
        payloads = [hit["message"] for hit in r.json()]
        raw = r.text + (await house.bot.get("/search", params={"q": CANARY})).text
    else:
        params = {"from_seq": 0} if surface == "backfill" else {}
        payloads = await house.listed(**params)
        raw = json.dumps(payloads)
    assert attachment_ids(payloads) == {plain}
    assert CANARY not in raw


@pytest.mark.parametrize("hide", [*HIDDEN, "channel_closed_after"])
async def test_a_hidden_file_reaches_no_bot_socket(house, hide):
    await house.flip("on")
    hide_it = _channel_closed_after if hide == "channel_closed_after" else HIDDEN[hide]
    hidden = await hide_it(house)

    frames = [f for f in house.wire.frames if "message" in f]
    assert CANARY not in json.dumps(frames)
    if hide in ("flagged_by_edit", "deleted", "channel_closed_after"):
        frames = [f for f in frames if hidden in attachment_ids([f["message"]])]
        assert [f["type"] for f in frames] == ["message_edit"]
        assert frames[0]["message"]["attachments"] == [
            {"id": hidden, "mime_type": "text/plain", "size_bytes": len(CANARY)}]
        assert (await house.read(hidden)).status_code == 404
    else:
        assert hidden not in attachment_ids(f["message"] for f in frames)


async def test_a_message_lists_only_the_attachments_a_bot_may_read(house):
    old, (marked,) = await house.say("old", txt("marked.txt"))
    await house.flip("on", "--include-history")
    unmarked = await house.attach(old["id"], *txt("unmarked.txt"))
    new, (text, video) = await house.say(
        "new", txt("ok.txt"), ("clip.mp4", b"\x00\x00\x00\x18ftyp", "video/mp4"))

    assert attachment_ids([{"attachments": await house.listing(old["id"])}]) == {marked}
    assert attachment_ids([{"attachments": await house.listing(new["id"])}]) == {text}
    for hidden in (unmarked, video):
        assert (await house.read(hidden)).status_code == 404
    edits = [f["message"] for f in house.wire.frames
             if f["type"] == "message_edit" and f["message"]["id"] == new["id"]]
    assert attachment_ids(edits) == {text}
    assert len((await house.seen_by_user(new["id"]))["attachments"]) == 2


async def test_a_bot_listing_carries_three_keys_and_no_url(house):
    await house.flip("on")
    message, (att,) = await house.say("findme", txt("notes.txt", b"abc"))
    entry = {"id": att, "mime_type": "text/plain", "size_bytes": 3}

    assert await house.listing(message["id"]) == [entry]
    hits = (await house.bot.get("/search", params={"q": "findme"})).json()
    assert hits[0]["message"]["attachments"] == [entry]
    assert house.wire.frames[-1]["message"]["attachments"] == [entry]
    human = await house.seen_by_user(message["id"])
    assert {"url", "thumb_url", "orig_url", "original_filename"} <= set(human["attachments"][0])


async def test_an_attachment_name_is_not_searchable(house):
    await house.flip("on")
    await house.say("the quarterly numbers", txt("zebrafish.txt", b"zebrafish"))

    for asker in (house.bot, house.client):
        assert (await asker.get("/search", params={"q": "zebrafish"})).json() == []
        assert len((await asker.get("/search", params={"q": "quarterly"})).json()) == 1


async def test_an_upload_over_the_size_cap_is_hidden(house, monkeypatch):
    await house.flip("on")
    _, (att,) = await house.say("big", txt("big.txt", b"x" * 100))
    assert (await house.read(att)).status_code == 200
    set_env(monkeypatch, "MAX_UPLOAD_BYTES", 99)
    assert (await house.read(att)).status_code == 404
    assert attachment_ids(await house.listed()) == set()


async def test_any_error_deciding_visibility_means_not_visible(house, monkeypatch):
    await house.flip("on")
    message, (att,) = await house.say("x", txt())
    good = await row(att)
    assert await bot_files.visible_to_bots(good) is True

    assert await bot_files.visible_to_bots({}) is False
    assert await bot_files.visible_to_bots({**good, "message_id": None}) is False
    assert await bot_files.visible_to_bots({**good, "size_bytes": None}) is False
    assert await bot_files.visible_to_bots({**good, "mime_type": None}) is False
    await db.execute("UPDATE messages SET privacy_flags = 'not json' WHERE id = ?",
                     (message["id"],))
    assert await bot_files.visible_to_bots(good) is False
    assert (await house.read(att)).status_code == 404

    async def broken(*_args, **_kwargs):
        raise RuntimeError("database is gone")

    monkeypatch.setattr(db, "fetch_one", broken)
    assert await bot_files.visible_to_bots(good) is False
    assert "attachments" not in await bot_files.for_bot(
        {"id": message["id"], "channel_id": house.room, "attachments": [{"id": att}]})


async def test_no_stored_file_is_opened_for_a_hidden_attachment(house, monkeypatch):
    await house.flip("on")
    _, (att,) = await house.say("x", txt(), flags={"secret": True})
    opened = []
    monkeypatch.setattr(bot_files, "_stored", lambda *a: opened.append(a))

    assert (await house.read(att)).status_code == 404
    assert opened == []


# --------------------------------------------------------------- membership

async def test_a_bot_outside_a_private_channel_cannot_read_its_files(house, monkeypatch):
    den = (await house.client.post(
        "/channels", json={"name": "den", "visibility": "private"})).json()["id"]
    await house.flip("on", channel="den")
    _, (att,) = await house.say("in the den", txt(), channel=den)
    opened = []
    real = bot_files._stored
    monkeypatch.setattr(bot_files, "_stored", lambda *a: opened.append(a) or real(*a))

    assert (await house.read(att)).status_code == 404
    assert opened == []
    await house.join(house.bot_id, den)
    assert (await house.read(att)).status_code == 200


async def test_a_bot_that_has_not_joined_a_public_channel_cannot_read_its_files(house):
    main = (await db.fetch_one("SELECT id FROM channels WHERE type = 'main_feed'"))["id"]
    await house.flip("on", channel="main")
    _, (att,) = await house.say("in main", txt(), channel=main)

    assert (await house.bot.get(f"/channels/{main}/messages")).status_code == 403
    answer = await house.read(att)
    assert answer.status_code == 404
    assert answer.json() == (await house.read(999_999)).json()
    await house.join(house.bot_id, main)
    assert (await house.read(att)).status_code == 200


async def test_the_relay_reads_as_the_resident_whose_seat_it_names(house):
    await house.flip("on")
    _, (att,) = await house.say("x", txt())
    await house.add_bot("broker", "relay-key")
    await house.add_bot("otto", "otto-key")
    await house.add_bot("Gamma", "gamma-key", house.room)

    assert (await house.read(att, "relay-key", on_behalf_of="res-claw")).status_code == 200
    assert (await house.read(att, "relay-key", on_behalf_of="res-gamma")).status_code == 200
    assert (await house.read(att, "relay-key", on_behalf_of="res-otto")).status_code == 404
    assert (await house.read(att, "relay-key")).status_code == 404


@pytest.mark.parametrize("label", [
    "res-nobody", "claw", "res-", "", "RES-CLAW", "res-claw ", "res-claw%", "res-" + "a" * 80,
    "res-dup",
])
async def test_a_seat_that_maps_to_no_one_bot_reads_nothing(house, label):
    await house.flip("on")
    _, (att,) = await house.say("x", txt())
    await house.add_bot("broker", "relay-key", house.room)
    await house.add_bot("dup", "k1", house.room)
    await house.add_bot("Dup", "k2", house.room)

    assert (await house.read(att, "relay-key")).status_code == 200
    assert (await house.read(att, "relay-key", on_behalf_of=label)).status_code == 404


async def test_a_bot_that_is_not_the_relay_gains_nothing_by_naming_a_seat(house):
    await house.flip("on")
    _, (att,) = await house.say("x", txt())
    await house.add_bot("otto", "otto-key")

    assert (await house.read(att, "otto-key", on_behalf_of="res-claw")).status_code == 404
    assert (await house.read(att, on_behalf_of="res-claw")).status_code == 404
    assert (await house.read(att)).status_code == 200


async def test_every_read_attempt_is_logged_without_the_file(house, caplog):
    await house.flip("on")
    _, (att,) = await house.say("x", txt(f"{CANARY}.txt", CANARY.encode()))
    relay = await house.add_bot("broker", "relay-key")
    caplog.set_level(logging.INFO, logger="app.routers.bot_files")

    await house.read(att)
    await house.read(att, "relay-key", on_behalf_of="res-claw")
    await house.read(att, "relay-key")
    await house.read(999_999)

    lines = [r.getMessage() for r in caplog.records if r.name == "app.routers.bot_files"]
    assert lines == [
        f"bot attachment read: caller=bot:{house.bot_id} reader_bot={house.bot_id}"
        f" attachment={att} allowed",
        f"bot attachment read: caller=bot:{relay} reader_bot={house.bot_id}"
        f" attachment={att} allowed",
        f"bot attachment read: caller=bot:{relay} reader_bot={relay}"
        f" attachment={att} refused",
        f"bot attachment read: caller=bot:{house.bot_id} reader_bot={house.bot_id}"
        " attachment=999999 refused",
    ]
    assert CANARY not in caplog.text


async def test_a_private_channel_is_opened_only_by_an_admin_who_is_in_it(house, client):
    den = (await client.post(
        "/channels", json={"name": "den", "visibility": "private"})).json()["id"]
    await db.execute(
        "INSERT INTO users (username, password_hash, display_name, is_admin)"
        " VALUES ('eve', ?, 'Eve', 1)", (auth.hash_password(PASSWORD),))
    await client.post("/auth/login", json={"username": "eve", "password": PASSWORD})
    main = (await db.fetch_one("SELECT id FROM channels WHERE type = 'main_feed'"))["id"]

    await house.say("/files-safe #den on --include-history", channel=main)

    assert (await channel(den))["files_safe"] == 0
    replies = [m["content"] for m in (await client.get(f"/channels/{main}/messages")).json()
               if m["author_type"] == "bot"]
    assert replies == [slash.FILES_SAFE_NO_CHANNEL]
    assert "den" not in slash.FILES_SAFE_NO_CHANNEL

    await client.post("/auth/login", json={"username": "ada", "password": PASSWORD})
    await house.say("/files-safe #den on", channel=main)
    assert (await channel(den))["files_safe"] == 1


# -------------------------------------------------------------- enumeration

async def test_unknown_and_hidden_ids_answer_alike(house):
    await house.flip("on")
    _, (hidden,) = await house.say("x", txt(), flags={"secret": True})
    staged = (await house.client.post(
        "/upload", files=[("files", txt())])).json()["attachments"][0]["id"]

    answers = [await house.read(i) for i in (hidden, staged, 999_999, 0, -1, 2 ** 70)]
    assert {a.status_code for a in answers} == {404}
    assert len({a.text for a in answers}) == 1


async def test_the_read_route_is_for_a_bot_key_only(house, client):
    await house.flip("on")
    _, (att,) = await house.say("x", txt())

    assert (await client.get(f"/bot/attachments/{att}")).status_code == 404
    client.cookies.clear()
    assert (await client.get(f"/bot/attachments/{att}")).status_code == 401
    assert (await house.read(att)).status_code == 200


# -------------------------------------------------------------- read shapes

async def test_a_long_text_file_is_cut_at_the_cap_with_a_marker(house):
    await house.flip("on")
    body = b"0123456789abcdef" * (5 * MIB // 16)
    _, (att,) = await house.say("log", txt("big.log", body))

    text = (await house.read(att)).json()["text"]
    marker = f"\n[truncated: {MIB} of {5 * MIB} bytes]"
    assert get_settings().BOT_READ_BYTES == MIB
    assert text.endswith(marker)
    assert text[:-len(marker)] == body[:MIB].decode()


async def test_bytes_that_are_not_text_still_come_back_as_text(house):
    await house.flip("on")
    _, (att,) = await house.say("odd", txt("odd.txt", b"caf\xe9 \xff ok"))
    assert (await house.read(att)).json()["text"] == "caf� � ok"


async def test_an_image_comes_back_as_its_largest_variant_under_the_cap(house, monkeypatch):
    await house.flip("on")
    original = noisy_png((2400, 1200))
    _, (att,) = await house.say("photo", ("photo.png", original, "image/png"))
    stored = await row(att)
    data_dir = get_settings().data_dir
    display = (data_dir / stored["display_path"]).read_bytes()
    thumb = (data_dir / stored["thumb_path"]).read_bytes()
    assert len(thumb) < len(display) < MIB

    body = (await house.read(att)).json()
    assert (body["kind"], body["media_type"]) == ("image", "image/webp")
    assert base64.b64decode(body["data"]) == display

    assert not bot_variants()

    set_env(monkeypatch, "BOT_READ_BYTES", len(display) - 1)
    sent = base64.b64decode((await house.read(att)).json()["data"])
    assert len(thumb) < len(sent) < len(display)
    assert Image.open(io.BytesIO(sent)).size == (1568, 784)
    set_env(monkeypatch, "BOT_READ_BYTES", len(thumb) - 1)
    assert (await house.read(att)).status_code == 404


async def test_an_image_never_comes_back_as_its_original(house):
    await house.flip("on")
    original = png()
    _, (att,) = await house.say("small", ("small.png", original, "image/png"))
    stored = await row(att)
    assert stored["display_path"] is None

    sent = base64.b64decode((await house.read(att)).json()["data"])
    assert sent != original
    assert Image.open(io.BytesIO(sent)).format == "WEBP"

    await db.execute("UPDATE attachments SET thumb_path = file_path WHERE id = ?", (att,))
    assert (await house.read(att)).status_code == 404
    await db.execute("UPDATE attachments SET thumb_path = NULL WHERE id = ?", (att,))
    assert (await house.read(att)).status_code == 404


def bot_variants() -> list:
    return sorted((get_settings().data_dir / "uploads" / "web").glob("*_bot.webp"))


async def sent_image(house, att) -> Image.Image:
    return Image.open(io.BytesIO(base64.b64decode((await house.read(att)).json()["data"])))


async def test_an_upload_the_browser_shows_as_it_is_comes_back_big_enough_to_read(house):
    await house.flip("on")
    _, (att,) = await house.say("shot", ("shot.png", noisy_png((1900, 950)), "image/png"))
    assert (await row(att))["display_path"] is None

    sent = await sent_image(house, att)
    assert (sent.format, sent.size) == ("WEBP", (1568, 784))

    [made] = bot_variants()
    stamp = made.stat().st_mtime_ns
    assert (await sent_image(house, att)).size == (1568, 784)
    assert made.stat().st_mtime_ns == stamp
    assert [p.name for p in made.parent.iterdir() if p.name.startswith(".tmp-")] == []


async def test_a_small_image_is_not_made_larger(house):
    await house.flip("on")
    _, (att,) = await house.say("small", ("small.png", png((300, 200)), "image/png"))
    assert (await sent_image(house, att)).size == (300, 200)


async def test_the_bot_variant_carries_no_exif(house):
    await house.flip("on")
    exif = Image.Exif()
    exif[0x010F] = "a camera maker"
    buf = io.BytesIO()
    Image.new("RGB", (800, 600), (10, 90, 160)).save(buf, format="JPEG", exif=exif)
    _, (att,) = await house.say("photo", ("photo.jpg", buf.getvalue(), "image/jpeg"))
    assert (await row(att))["display_path"] is None

    sent = await sent_image(house, att)
    assert sent.size == (800, 600)
    assert dict(sent.getexif()) == {}


async def test_an_animation_comes_back_as_its_first_frame(house):
    await house.flip("on")
    frames = [Image.new("RGB", (600, 400), colour) for colour in ((250, 0, 0), (0, 0, 250))]
    buf = io.BytesIO()
    frames[0].save(buf, format="GIF", save_all=True, append_images=frames[1:], duration=80)
    _, (att,) = await house.say("gif", ("spin.gif", buf.getvalue(), "image/gif"))

    sent = await sent_image(house, att)
    assert (sent.size, getattr(sent, "n_frames", 1)) == ((600, 400), 1)
    red, green, blue = sent.convert("RGB").getpixel((300, 200))
    assert red > 200 and blue < 50


async def test_a_hidden_image_makes_no_variant(house):
    _, (att,) = await house.say("closed", ("shot.png", png((900, 600)), "image/png"))
    assert (await house.read(att)).status_code == 404
    assert not bot_variants()


async def test_discarding_an_upload_removes_its_bot_variant_too(house):
    await house.flip("on")
    _, (att,) = await house.say("shot", ("shot.png", png((900, 600)), "image/png"))
    await house.read(att)
    [made] = bot_variants()
    stem = made.name.removesuffix("_bot.webp")

    await db.execute("UPDATE attachments SET message_id = NULL WHERE id = ?", (att,))
    await bot_files.discard_staged(att)
    uploads = get_settings().data_dir / "uploads"
    assert [p.name for p in uploads.rglob("*") if p.name.startswith(stem)] == []


async def test_an_original_that_will_not_decode_falls_back_to_the_thumbnail(house, caplog):
    await house.flip("on")
    _, (att,) = await house.say("shot", ("shot.png", png((900, 600)), "image/png"))
    stored = await row(att)
    data_dir = get_settings().data_dir
    (data_dir / stored["file_path"]).write_bytes(b"not an image any more")

    body = (await house.read(att)).json()
    assert base64.b64decode(body["data"]) == (data_dir / stored["thumb_path"]).read_bytes()
    assert not bot_variants()
    assert f"no bot variant for attachment {att} " in caplog.text
    assert "shot.png" not in caplog.text


async def test_a_pdf_comes_back_as_its_text_page_by_page(house, monkeypatch):
    await house.flip("on")
    raw = pdf("first page words", "second page words")
    _, (att,) = await house.say("doc", ("doc.pdf", raw, "application/pdf"))

    answer = await house.read(att)
    body = answer.json()
    assert body["kind"] == "text"
    assert body["text"] == "[page 1]\nfirst page words\n[page 2]\nsecond page words\n"
    assert set(body) == {"id", "mime_type", "size_bytes", "kind", "text"}
    assert b"%PDF" not in answer.content
    assert base64.b64encode(raw[:60]) not in answer.content

    set_env(monkeypatch, "BOT_READ_BYTES", 20)
    assert (await house.read(att)).json()["text"] == (
        "[page 1]\nfirst page \n[truncated: page 1 of 2]")


async def test_a_long_pdf_stops_at_the_page_cap(house, monkeypatch):
    await house.flip("on")
    monkeypatch.setattr(bot_files, "PDF_MAX_PAGES", 2)
    _, (words, scan, exact) = await house.say(
        "docs",
        ("words.pdf", pdf("one", "two", "three"), "application/pdf"),
        ("scan.pdf", blank_pdf(3), "application/pdf"),
        ("exact.pdf", pdf("one", "two"), "application/pdf"))

    assert (await house.read(words)).json()["text"] == (
        "[page 1]\none\n[page 2]\ntwo\n[truncated: page 2 of 3]")
    assert (await house.read(scan)).json()["text"] == (
        "[page 1]\n\n[page 2]\n[truncated: page 2 of 3]")
    assert (await house.read(exact)).json()["text"] == "[page 1]\none\n[page 2]\ntwo\n"


async def test_a_pdf_with_no_text_says_so(house):
    await house.flip("on")
    _, (att,) = await house.say("scan", ("scan.pdf", blank_pdf(3), "application/pdf"))
    assert (await house.read(att)).json()["text"] == "[no text layer: 3 pages]"


async def test_a_file_that_is_not_a_pdf_is_not_served_as_one(house):
    await house.flip("on")
    _, (att,) = await house.say("fake", ("fake.pdf", b"not a pdf at all", "application/pdf"))
    assert (await house.read(att)).status_code == 404


# ------------------------------------------------------------ the picker shelf

def two_frame_gif(size=(600, 400)) -> bytes:
    frames = [Image.new("RGB", size, colour) for colour in ((250, 0, 0), (0, 0, 250))]
    buf = io.BytesIO()
    frames[0].save(buf, format="GIF", save_all=True, append_images=frames[1:], duration=80)
    return buf.getvalue()


async def shelve(house, tab: str, name: str, content: bytes, mime: str) -> str:
    r = await house.client.post(
        "/picker/add", files=[("file", (name, content, mime))], data={"tab": tab})
    assert r.status_code == 200, r.text
    return r.json()["url"]


async def test_a_bot_reads_a_picker_gif_as_its_first_frame(house):
    url = await shelve(house, "gif", "spin.gif", two_frame_gif(), "image/gif")
    assert url.startswith("/picker/file/gif/")

    body = (await house.bot.get(url.replace("/picker/file/", "/bot/picker/"))).json()

    assert (body["path"], body["kind"], body["media_type"]) == (url, "image", "image/webp")
    sent = Image.open(io.BytesIO(base64.b64decode(body["data"])))
    assert (sent.format, sent.size, getattr(sent, "n_frames", 1)) == ("WEBP", (600, 400), 1)
    red, _, blue = sent.convert("RGB").getpixel((300, 200))
    assert red > 200 and blue < 50


async def test_a_picker_image_comes_back_no_larger_than_a_bot_reads(house):
    exif = Image.Exif()
    exif[0x010F] = "a camera maker"
    buf = io.BytesIO()
    Image.new("RGB", (3200, 1600), (10, 90, 160)).save(buf, format="JPEG", exif=exif)
    url = await shelve(house, "image", "wide.jpg", buf.getvalue(), "image/jpeg")

    body = (await house.bot.get(url.replace("/picker/file/", "/bot/picker/"))).json()

    sent = Image.open(io.BytesIO(base64.b64decode(body["data"])))
    assert sent.size == (1568, 784) and dict(sent.getexif()) == {}


async def test_a_picker_read_needs_no_files_safe_channel_and_no_membership(house):
    url = await shelve(house, "image", "open.png", png((90, 60)), "image/png")
    await house.add_bot("stranger", "stranger-key")

    r = await house.bot.get(
        url.replace("/picker/file/", "/bot/picker/"), headers={"X-Api-Key": "stranger-key"})

    assert r.status_code == 200 and r.json()["kind"] == "image"


async def test_a_picker_read_keeps_nothing_and_changes_nothing_on_the_shelf(house):
    url = await shelve(house, "image", "kept.png", png((90, 60)), "image/png")
    data_dir = get_settings().data_dir
    before = sorted(str(p.relative_to(data_dir)) for p in data_dir.rglob("*"))

    assert (await house.bot.get(url.replace("/picker/file/", "/bot/picker/"))).status_code == 200

    assert sorted(str(p.relative_to(data_dir)) for p in data_dir.rglob("*")) == before


@pytest.mark.parametrize("path", [
    "/bot/picker/gif/missing.gif", "/bot/picker/video/open.png", "/bot/picker/image/.hidden",
    "/bot/picker/image/..%2F..%2Fdisjorn.db", "/bot/picker/image/%2E%2E",
    "/bot/picker/image/sub%5Copen.png",
])
async def test_a_path_that_names_no_picker_file_is_not_found(house, path):
    await shelve(house, "image", "open.png", png((90, 60)), "image/png")
    assert (await house.bot.get(path)).status_code == 404


async def test_a_picker_file_over_the_read_cap_is_not_found(house, monkeypatch):
    url = await shelve(house, "image", "noise.png", noisy_png((800, 600)), "image/png")
    set_env(monkeypatch, "BOT_READ_BYTES", 2000)
    assert (await house.bot.get(url.replace("/picker/file/", "/bot/picker/"))).status_code == 404


async def test_a_person_gets_nothing_from_the_bot_picker_route(house):
    url = await shelve(house, "image", "open.png", png((90, 60)), "image/png")
    r = await house.client.get(url.replace("/picker/file/", "/bot/picker/"))
    assert r.status_code == 404


async def test_a_picker_file_that_will_not_decode_is_not_found(house):
    url = await shelve(house, "image", "open.png", png((90, 60)), "image/png")
    name = url.rsplit("/", 1)[1]
    (get_settings().data_dir / "assets" / "picker" / "images" / name).write_bytes(b"not an image")
    assert (await house.bot.get(url.replace("/picker/file/", "/bot/picker/"))).status_code == 404
