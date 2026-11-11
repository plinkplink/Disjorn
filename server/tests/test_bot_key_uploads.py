"""A bot's own key on /upload and /attachments/claim meets the rules a relayed file post meets."""

import base64

import pytest

from app import db
from app.config import get_settings

from .test_bot_files import (  # noqa: F401 — fixtures
    BOT_KEY, House, house, png, reset_rate_limit, row, set_env, txt,
)

NOT_SAFE = "is not a files-safe channel this bot is a member of"
BAD_TYPE = "a bot may not post a file of this type"
HIDDEN = "this file matches an upload that bots may not read"
HTML = b"<!doctype html><script>alert(1)</script>"


async def bot_says(house: House, channel: int | None = None) -> int:
    r = await house.bot.post(
        f"/channels/{channel or house.room}/messages", json={"content": "beep"})
    assert r.status_code == 200, r.text
    return r.json()["id"]


async def stage(house: House, name: str, content: bytes, mime: str, **form):
    return await house.bot.post(
        "/upload", files=[("files", (name, content, mime))],
        data={k: str(v) for k, v in form.items()})


async def claim(house: House, message_id: int, *ids: int):
    return await house.bot.post(
        "/attachments/claim", json={"attachment_ids": list(ids), "message_id": message_id})


async def staged_id(house: House, name: str, content: bytes, mime: str) -> int:
    r = await stage(house, name, content, mime)
    assert r.status_code == 200, r.text
    return r.json()["attachments"][0]["id"]


async def attachments_of(message_id: int) -> list[int]:
    rows = await db.fetch_all(
        "SELECT id FROM attachments WHERE message_id = ? ORDER BY id", (message_id,))
    return [r["id"] for r in rows]


async def test_a_bot_claims_its_upload_in_a_files_safe_channel_it_is_in(house):
    await house.flip("on")
    message = await bot_says(house)
    att = await staged_id(house, *txt())

    r = await claim(house, message, att)

    assert r.status_code == 200, r.text
    assert [a["id"] for a in r.json()["attachments"]] == [att]
    assert (await claim(house, message, att)).status_code == 200


async def test_a_bot_uploads_straight_onto_its_message_in_a_files_safe_channel(house):
    await house.flip("on")
    message = await bot_says(house)

    r = await stage(house, "chart.png", png(), "image/png", message_id=message)

    assert r.status_code == 200, r.text
    (att,) = r.json()["attachments"]
    assert att["message_id"] == message
    assert [a["id"] for a in r.json()["message"]["attachments"]] == [att["id"]]
    assert await attachments_of(message) == [att["id"]]


@pytest.mark.parametrize("why", ["never_safe", "turned_off", "bot_removed"])
async def test_a_bot_links_no_file_where_it_may_not_read_files(house, why):
    if why != "never_safe":
        await house.flip("on")
    message = await bot_says(house)
    att = await staged_id(house, *txt())
    if why == "turned_off":
        await house.flip("off")
    if why == "bot_removed":
        await db.execute(
            "DELETE FROM channel_members WHERE member_type = 'bot' AND member_id = ?",
            (house.bot_id,))
    before = (await db.fetch_one("SELECT COUNT(*) AS n FROM attachments"))["n"]

    claimed = await claim(house, message, att)
    direct = await stage(house, "chart.png", png(), "image/png", message_id=message)

    for r in (claimed, direct):
        assert (r.status_code, r.json()["detail"]) == (403, f"channel {house.room} {NOT_SAFE}")
    assert await attachments_of(message) == []
    assert (await db.fetch_one("SELECT COUNT(*) AS n FROM attachments"))["n"] == before


@pytest.mark.parametrize("name,content,mime", [
    ("page.html", HTML, "text/html"),
    ("page.html", HTML, "text/plain"),
    ("notes.txt", HTML, "text/html"),
    ("pic.svg", b"<svg/>", "image/svg+xml"),
    ("a.txt", b"<x/>", "text/x+xml"),
    ("clip.txt", b"\x00\x00\x00\x18ftypmp42", "video/mp4"),
    ("tool.exe", b"MZ", "application/octet-stream"),
    ("blob", b"x", "text/plain"),
])
async def test_a_bot_links_no_file_of_a_type_no_bot_may_post(house, name, content, mime):
    await house.flip("on")
    message = await bot_says(house)
    att = await staged_id(house, name, content, mime)

    claimed = await claim(house, message, att)
    direct = await stage(house, name, content, mime, message_id=message)

    for r in (claimed, direct):
        assert (r.status_code, r.json()["detail"]) == (403, BAD_TYPE)
    assert await attachments_of(message) == []


@pytest.mark.parametrize("hide", ["secret", "other_channel", "staged_by_a_user"])
async def test_a_bot_links_no_bytes_of_an_upload_bots_may_not_read(house, hide):
    await house.flip("on")
    body = b"the hidden bytes"
    if hide == "secret":
        await house.say("x", txt(body=body), flags={"secret": True})
    elif hide == "other_channel":
        other = (await house.client.post("/channels", json={"name": "closed"})).json()["id"]
        await house.say("x", txt(body=body), channel=other)
    else:
        r = await house.client.post("/upload", files=[("files", txt(body=body))])
        assert r.status_code == 200
    message = await bot_says(house)
    att = await staged_id(house, "innocent.txt", body, "text/plain")

    claimed = await claim(house, message, att)
    direct = await stage(house, "innocent.txt", body, "text/plain", message_id=message)

    for r in (claimed, direct):
        assert (r.status_code, r.json()["detail"]) == (403, HIDDEN)
    assert await attachments_of(message) == []
    other_bytes = await staged_id(house, "innocent.txt", body + b"!", "text/plain")
    assert (await claim(house, message, other_bytes)).status_code == 200


async def test_a_bots_own_unclaimed_upload_does_not_hide_its_bytes_from_it(house):
    await house.flip("on")
    message = await bot_says(house)
    first = await staged_id(house, *txt())
    second = await staged_id(house, *txt())

    assert (await claim(house, message, second)).status_code == 200
    assert (await claim(house, message, first)).status_code == 200


async def test_a_bots_uploads_stop_at_its_daily_quota(house, monkeypatch):
    set_env(monkeypatch, "BOT_DAILY_UPLOAD_BYTES", 1000)
    assert (await stage(house, "a.txt", b"x" * 600, "text/plain")).status_code == 200

    r = await stage(house, "b.txt", b"x" * 401, "text/plain")

    assert (r.status_code, r.json()["detail"]) == (
        403, "daily upload quota is 1000 bytes; 600 are used today and this file is 401")
    assert (await db.fetch_one("SELECT COUNT(*) AS n FROM attachments"))["n"] == 1
    originals = get_settings().data_dir / "uploads" / "originals"
    assert [p.name for p in originals.iterdir() if p.name.startswith("tmp_")] == []
    assert (await stage(house, "c.txt", b"x" * 400, "text/plain")).status_code == 200


async def test_the_quota_counts_one_bot_and_one_day_and_no_user(house, monkeypatch):
    set_env(monkeypatch, "BOT_DAILY_UPLOAD_BYTES", 1000)
    await house.add_bot("other", "other-key")
    old = await staged_id(house, "old.txt", b"x" * 900, "text/plain")
    await db.execute(
        "UPDATE attachments SET created_at = '2001-01-01T00:00:00.000Z' WHERE id = ?", (old,))

    mine = await stage(house, "a.txt", b"x" * 900, "text/plain")
    theirs = await house.bot.post(
        "/upload", files=[("files", ("b.txt", b"x" * 900, "text/plain"))],
        headers={"X-Api-Key": "other-key"})
    users = await house.client.post(
        "/upload", files=[("files", ("c.txt", b"x" * 5000, "text/plain"))])

    assert [r.status_code for r in (mine, theirs, users)] == [200, 200, 200]


async def test_a_user_still_attaches_any_file_anywhere(house):
    _, (att,) = await house.say("x", ("page.html", HTML, "text/html"))

    assert (await row(att))["message_id"] is not None


async def test_a_relayed_post_cannot_pass_the_servers_own_quota(house, monkeypatch):
    set_env(monkeypatch, "BOT_DAILY_UPLOAD_BYTES", 10)
    await house.flip("on")
    await house.add_bot("broker", "relay-key")

    r = await house.bot.post(
        "/bot/attachments", headers={"X-Api-Key": "relay-key"},
        json={"on_behalf_of": "res-claw", "channel_id": house.room, "filename": "report.md",
              "data": base64.b64encode(b"x" * 11).decode(), "text": "",
              "daily_quota_bytes": 1000})

    assert r.status_code == 403
    assert "daily upload quota is 10 bytes" in r.json()["detail"]
    counts = await db.fetch_one(
        "SELECT (SELECT COUNT(*) FROM attachments) AS a, (SELECT COUNT(*) FROM messages"
        " WHERE author_type = 'bot' AND author_id = ?) AS m", (house.bot_id,))
    assert (counts["a"], counts["m"]) == (0, 0)
