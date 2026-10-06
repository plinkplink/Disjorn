"""POST /backlog: the broker files a row for a resident seat.

The relay is the only caller; the label it carries is the row's author; the
text meets the slash command's own cap; and the filing is announced in
#custodian like a human's `/backlog <text>`."""

import tomllib
from pathlib import Path

import pytest

from app import db
from app.config import reset_settings_cache
from app.routers import auth, slash

PASSWORD = "correct horse battery staple"
RELAY_KEY = "broker-key"
REPO = Path(__file__).resolve().parents[2]


@pytest.fixture(autouse=True)
def fresh_settings():
    reset_settings_cache()
    yield
    reset_settings_cache()


async def make_bot(name: str = "broker", api_key: str = RELAY_KEY) -> int:
    cur = await db.execute(
        "INSERT INTO bots (name, api_key_hash) VALUES (?, ?)",
        (name, auth.hash_api_key(api_key)),
    )
    return cur.lastrowid


async def make_custodian() -> int:
    cur = await db.execute(
        "INSERT INTO channels (type, name, created_at) VALUES ('text', 'custodian', ?)",
        (db.utc_now(),),
    )
    return cur.lastrowid


async def relay(client, text="add a dark mode", on_behalf_of="res-claudette",
                api_key=RELAY_KEY):
    client.cookies.clear()
    return await client.post("/backlog",
                             json={"text": text, "on_behalf_of": on_behalf_of},
                             headers={"X-Api-Key": api_key})


async def rows() -> list[dict]:
    return await db.fetch_all("SELECT * FROM backlog ORDER BY id")


async def channel_posts(channel_id: int) -> list[dict]:
    return await db.fetch_all(
        "SELECT m.content, b.name FROM messages m JOIN bots b ON b.id = m.author_id "
        "WHERE m.channel_id = ? AND m.author_type = 'bot' ORDER BY m.seq",
        (channel_id,))


async def test_the_relay_files_a_row_authored_by_the_resident(client):
    await make_bot()
    await make_custodian()
    r = await relay(client, "let residents read the backlog", "res-gable")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["author"] == "res-gable" and body["status"] == "open"
    (row,) = await rows()
    assert row["text"] == "let residents read the backlog"
    assert row["author"] == "res-gable"


async def test_the_filing_is_announced_in_custodian_without_waking_anyone(client):
    from app.ws import _mentions_bot

    await make_bot()
    custodian = await make_custodian()
    r = await relay(client, on_behalf_of="res-claudette")
    assert r.status_code == 200, r.text
    (post,) = await channel_posts(custodian)
    assert post["name"] == "system"
    assert post["content"] == (
        f"Filed backlog #{r.json()['id']} (open) from a resident seat. "
        "Residents triage in #custodian.")
    for name in ("claudette", "Gable"):
        assert not _mentions_bot(post["content"], name)
    row = await db.fetch_one("SELECT author FROM backlog WHERE id = ?", (r.json()["id"],))
    assert row["author"] == "res-claudette"


async def test_a_human_is_refused_the_relay(client):
    await make_custodian()
    await db.execute(
        "INSERT INTO users (username, password_hash, display_name, is_admin) "
        "VALUES (?, ?, ?, 1)", ("plink", auth.hash_password(PASSWORD), "plink"))
    await client.post("/auth/login", json={"username": "plink", "password": PASSWORD})
    r = await client.post("/backlog",
                          json={"text": "x", "on_behalf_of": "res-gable"})
    assert r.status_code == 403
    assert await rows() == []


async def test_a_bot_off_the_relay_list_is_refused(client):
    await make_custodian()
    await make_bot("Gable", "gable-own-key")
    r = await relay(client, api_key="gable-own-key")
    assert r.status_code == 403
    assert "BACKLOG_RELAY_BOT_NAMES" in r.json()["detail"]
    assert await rows() == []


async def test_the_relay_list_is_config(client, monkeypatch):
    monkeypatch.setenv("BACKLOG_RELAY_BOT_NAMES", '["relay"]')
    reset_settings_cache()
    await make_custodian()
    await make_bot()
    await make_bot("relay", "relay-key")
    assert (await relay(client)).status_code == 403
    assert (await relay(client, api_key="relay-key")).status_code == 200


@pytest.mark.parametrize("label", [
    "plink", "res-", "res-Claudette", "res-1x", "res-gable/../plink",
    "claudette", "res-gable (via broker)", "", "res-gable\n",
    "res-" + "a" * 70])
async def test_a_label_that_is_not_a_resident_seat_is_refused(client, label):
    await make_bot()
    await make_custodian()
    r = await relay(client, on_behalf_of=label)
    assert r.status_code == 400, label
    assert await rows() == []


async def test_the_cap_is_the_slash_commands_cap(client):
    await make_bot()
    custodian = await make_custodian()
    at_cap = "x" * slash.MAX_BACKLOG_CHARS
    assert (await relay(client, at_cap)).status_code == 200
    r = await relay(client, at_cap + "x")
    assert r.status_code == 400
    assert f"capped at {slash.MAX_BACKLOG_CHARS}" in r.json()["detail"]
    assert len(await rows()) == 1
    assert len(await channel_posts(custodian)) == 1


async def test_the_broker_surface_states_the_same_cap():
    surface = tomllib.loads(
        (REPO / "harness/broker/verb_surface.toml").read_text(encoding="utf-8"))
    text = surface["verbs"]["backlog-file"]["args"]["text"]
    assert text["max_len"] == slash.MAX_BACKLOG_CHARS


async def test_private_text_is_refused_without_an_echo(client):
    await make_bot()
    await make_custodian()
    r = await relay(client, "off the record, the payroll importer is late")
    assert r.status_code == 400
    assert "payroll" not in r.json()["detail"]
    assert await rows() == []


async def test_blank_text_files_nothing(client):
    await make_bot()
    await make_custodian()
    assert (await relay(client, "   ")).status_code == 400
    assert await rows() == []


async def test_without_a_custodian_channel_nothing_is_filed(client):
    await make_bot()
    r = await relay(client)
    assert r.status_code == 503
    assert await rows() == []


async def test_the_relay_cannot_triage(client):
    """Filing is the only thing the relay does; a status change stays human."""
    await make_bot()
    await make_custodian()
    r = await client.post("/backlog",
                          json={"text": "x", "on_behalf_of": "res-gable",
                                "status": "built"},
                          headers={"X-Api-Key": RELAY_KEY})
    assert r.status_code == 200
    (row,) = await rows()
    assert row["status"] == "open"
