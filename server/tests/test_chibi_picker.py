"""Admin chibi picker: alias file edits, the #custodian line, and re-pointing one message's chibi."""

import json
import re

import pytest

from app import db, events
from app.config import reset_settings_cache
from app.routers import auth
from app.routers.chibi_admin import custodian_line
from app.services import chibi, chibi_aliases

PASSWORD = "correct horse battery staple"
BOT_KEY = "bot-key-picker"
PACK = "claudette"
SUMMONS = re.compile(r"(?<!\w)@?claudette(?!\w)", re.IGNORECASE)

ALIASES = (
    "# Aliases.txt, the pack's taste knob.\n"
    "#   alias -> Target\n"
    "\n"
    "flummoxed       -> Confused\n"
    "wry             -> Curious          # here for the record\n"
    "  # an indented comment -> stays\n"
    "beaming -> Happy\n"
)


@pytest.fixture(autouse=True)
def clear_chibi_cache():
    chibi.clear_cache()
    yield
    chibi.clear_cache()


def aliases_path():
    chibi.ensure_default_pack()
    return chibi.packs_root() / PACK / chibi.ALIASES_FILENAME


def write_aliases(text: str = ALIASES):
    path = aliases_path()
    path.write_bytes(text.encode())
    return path


async def make_user(username: str, *, is_admin: bool = False) -> int:
    cur = await db.execute(
        """INSERT INTO users (username, password_hash, display_name, is_admin)
           VALUES (?, ?, ?, ?)""",
        (username, auth.hash_password(PASSWORD), username.capitalize(), int(is_admin)),
    )
    return cur.lastrowid


async def login(client, username: str) -> None:
    r = await client.post("/auth/login", json={"username": username, "password": PASSWORD})
    assert r.status_code == 200


async def make_resident() -> int:
    cur = await db.execute(
        "INSERT INTO bots (name, api_key_hash, chibi_pack) VALUES (?, ?, ?)",
        ("Claudette", auth.hash_api_key(BOT_KEY), PACK),
    )
    return cur.lastrowid


async def make_custodian(monkeypatch) -> int:
    cur = await db.execute("INSERT INTO channels (type, name) VALUES ('text', 'custodian')")
    monkeypatch.setenv("CUSTODIAN_CHANNEL_ID", str(cur.lastrowid))
    reset_settings_cache()
    return cur.lastrowid


async def custodian_lines(channel_id: int) -> list[str]:
    rows = await db.fetch_all(
        "SELECT content FROM messages WHERE channel_id = ? ORDER BY seq", (channel_id,)
    )
    return [r["content"] for r in rows]


async def bot_message(client, bot_id: int, content: str, emotion=None) -> dict:
    main = (await db.fetch_one("SELECT id FROM channels WHERE type = 'main_feed'"))["id"]
    await db.execute(
        "INSERT OR IGNORE INTO channel_members (channel_id, member_type, member_id) "
        "VALUES (?, 'bot', ?)",
        (main, bot_id),
    )
    cookies = dict(client.cookies)
    client.cookies.clear()
    r = await client.post(
        f"/channels/{main}/messages",
        json={"content": content, "emotion": emotion},
        headers={"X-Api-Key": BOT_KEY},
    )
    client.cookies.update(cookies)
    assert r.status_code == 200, r.text
    return r.json()


@pytest.fixture
async def admin(client):
    await make_user("plink", is_admin=True)
    await login(client, "plink")
    return client


# ---------------------------------------------------------------------------
# Alias file writes
# ---------------------------------------------------------------------------

async def test_setting_an_alias_replaces_its_line_and_keeps_every_other_byte(admin):
    path = write_aliases()
    r = await admin.put(f"/chibi/{PACK}/aliases/Wry", json={"target": "smug"})
    assert r.status_code == 200, r.text
    assert r.json()["target"] == "Smug"
    assert path.read_text() == ALIASES.replace(
        "wry             -> Curious          # here for the record\n",
        "wry             -> Smug\n",
    )
    assert chibi.resolve(PACK, "wry") == "chibi:claudette/Happy_and_Confident/Smug.png"


async def test_a_new_tag_is_appended_and_the_header_survives(admin):
    path = write_aliases(ALIASES.rstrip("\n"))
    r = await admin.put(f"/chibi/{PACK}/aliases/fed up", json={"target": "Heart-Eyes"})
    assert r.status_code == 200, r.text
    assert path.read_text() == ALIASES + "fed up          -> Heart-Eyes\n"
    assert chibi.resolve(PACK, "Fed-Up") == "chibi:claudette/Calm_and_Content/Heart-Eyes.png"


async def test_the_alias_file_is_created_when_the_pack_has_none(admin):
    path = aliases_path()
    path.unlink(missing_ok=True)
    r = await admin.put(f"/chibi/{PACK}/aliases/wry", json={"target": "Happy"})
    assert r.status_code == 200
    assert path.read_text() == "wry             -> Happy\n"
    assert not [p for p in path.parent.iterdir() if p.name.endswith(".tmp")]


async def test_removing_an_alias_deletes_only_its_line_and_the_ladder_takes_over(admin):
    path = write_aliases()
    r = await admin.delete(f"/chibi/{PACK}/aliases/wry")
    assert r.status_code == 200, r.text
    assert path.read_text() == ALIASES.replace(
        "wry             -> Curious          # here for the record\n", ""
    )
    assert r.json()["face"] == "Smug"


async def test_removing_a_tag_with_no_alias_is_a_404_and_writes_nothing(admin):
    path = write_aliases()
    before = path.stat().st_mtime_ns
    r = await admin.delete(f"/chibi/{PACK}/aliases/gleeful")
    assert r.status_code == 404
    assert path.stat().st_mtime_ns == before


async def test_a_target_that_is_not_a_face_is_a_400(admin):
    path = write_aliases()
    r = await admin.put(f"/chibi/{PACK}/aliases/wry", json={"target": "Sly"})
    assert r.status_code == 400
    assert path.read_text() == ALIASES


async def test_a_face_name_cannot_be_aliased_over(admin):
    write_aliases()
    r = await admin.put(f"/chibi/{PACK}/aliases/smug", json={"target": "Happy"})
    assert r.status_code == 400


async def test_alias_and_face_routes_refuse_a_non_admin(client):
    path = write_aliases()
    await make_user("alice")
    await login(client, "alice")
    assert (await client.get(f"/chibi/{PACK}/faces")).status_code == 403
    assert (await client.put(f"/chibi/{PACK}/aliases/wry",
                             json={"target": "Happy"})).status_code == 403
    assert (await client.delete(f"/chibi/{PACK}/aliases/wry")).status_code == 403
    assert path.read_text() == ALIASES


async def test_faces_lists_the_pack_by_category_with_served_urls(admin):
    r = await admin.get(f"/chibi/{PACK}/faces")
    assert r.status_code == 200
    faces = r.json()
    assert {"category": "Calm_and_Content", "name": "Heart-Eyes",
            "url": "/chibi/claudette/Calm_and_Content/Heart-Eyes.png"} in faces
    assert [f["category"] for f in faces] == sorted(f["category"] for f in faces)
    assert (await admin.get("/chibi/nope/faces")).status_code == 404


async def test_the_alias_lookup_says_where_a_tag_lands(admin):
    write_aliases()
    r = await admin.get(f"/chibi/{PACK}/aliases", params={"tag": "wry"})
    assert r.json() == {"tag": "wry", "face": "Curious", "source": "alias",
                        "alias": ["Curious"]}
    r = await admin.get(f"/chibi/{PACK}/aliases", params={"tag": "gleeful"})
    assert r.json()["source"] == "ladder" and r.json()["alias"] is None


# ---------------------------------------------------------------------------
# The #custodian line
# ---------------------------------------------------------------------------

async def test_every_alias_change_posts_one_custodian_line_as_the_system_bot(admin, monkeypatch):
    write_aliases()
    channel = await make_custodian(monkeypatch)
    await make_resident()
    await admin.put(f"/chibi/{PACK}/aliases/gleeful", json={"target": "Happy"})
    await admin.put(f"/chibi/{PACK}/aliases/gleeful", json={"target": "Happy"})
    await admin.delete(f"/chibi/{PACK}/aliases/wry")

    rows = await db.fetch_all(
        "SELECT m.content, b.name FROM messages m JOIN bots b ON b.id = m.author_id "
        "WHERE m.channel_id = ? AND m.author_type = 'bot' ORDER BY m.seq", (channel,)
    )
    assert [r["name"] for r in rows] == ["system", "system"]
    lines = [r["content"] for r in rows]
    resident = (await db.fetch_one("SELECT id FROM bots WHERE name = 'Claudette'"))["id"]
    assert lines == [
        f'chibi alias, bot {resident} pack: "gleeful" ladder (no face) → Happy by plink',
        f'chibi alias, bot {resident} pack: "wry" Curious → ladder (Smug) by plink',
    ]
    for line in lines:
        assert "@" not in line
        assert not SUMMONS.search(line)


def test_the_custodian_line_names_no_resident_and_carries_no_at():
    bots = [(1, "system"), (2, "Claudette"), (3, "Gable")]
    line = custodian_line("claudette", "@claudette gable", "Sly", "ladder (no face)",
                          "@plink", bots)
    assert line == 'chibi alias, bot 2 pack: "bot 2 bot 3" Sly → ladder (no face) by plink'
    assert not SUMMONS.search(line)
    assert "@" not in line
    plain = custodian_line("sample", "wry", "Sly", "Smug", "plink", bots)
    assert plain == 'chibi alias, sample: "wry" Sly → Smug by plink'


async def test_an_alias_change_with_no_custodian_channel_still_lands(admin, monkeypatch):
    path = write_aliases()
    monkeypatch.setenv("CUSTODIAN_CHANNEL_ID", "9999")
    reset_settings_cache()
    r = await admin.put(f"/chibi/{PACK}/aliases/gleeful", json={"target": "Happy"})
    assert r.status_code == 200
    assert "gleeful" in path.read_text()


# ---------------------------------------------------------------------------
# Re-pointing one message
# ---------------------------------------------------------------------------

async def test_repointing_a_message_swaps_its_chibi_and_leaves_the_words(admin):
    write_aliases()
    bot_id = await make_resident()
    sent = await bot_message(admin, bot_id, "that landed [emotion: wry] mostly", "wry")
    assert sent["emote_refs"] == ["chibi:claudette/Curiosity_and_Cunning/Curious.png"]
    captured: list[dict] = []
    events.subscribe(captured.append)

    r = await admin.patch(f"/messages/{sent['id']}/emote",
                          json={"tag": "Wry", "target": "heart eyes", "index": 0})
    assert r.status_code == 200, r.text
    row = await db.fetch_one("SELECT * FROM messages WHERE id = ?", (sent["id"],))
    assert json.loads(row["emote_refs"]) == ["chibi:claudette/Calm_and_Content/Heart-Eyes.png"]
    assert row["content"] == sent["content"]
    assert row["edited_at"] is None
    edits = [e for e in captured if e["type"] == "message_edit"]
    assert len(edits) == 1
    assert edits[0]["message"]["emote_refs"] == json.loads(row["emote_refs"])


async def test_a_tag_that_resolved_to_nothing_gets_its_first_chibi(admin):
    bot_id = await make_resident()
    sent = await bot_message(admin, bot_id, "[emotion: zzzq] hm", "zzzq")
    assert sent["emote_refs"] == []
    r = await admin.patch(f"/messages/{sent['id']}/emote",
                          json={"tag": "zzzq", "target": "Happy"})
    assert r.status_code == 200, r.text
    assert r.json()["emote_refs"] == ["chibi:claudette/Happy_and_Confident/Happy.png"]


async def test_a_second_tag_repoints_the_second_chibi(admin):
    bot_id = await make_resident()
    sent = await bot_message(admin, bot_id, "[emotion: happy] then [emotion: smug]", "happy")
    await db.execute(
        "UPDATE messages SET emote_refs = ? WHERE id = ?",
        (json.dumps(["chibi:claudette/Happy_and_Confident/Happy.png",
                     "chibi:claudette/Happy_and_Confident/Smug.png"]), sent["id"]),
    )
    r = await admin.patch(f"/messages/{sent['id']}/emote",
                          json={"tag": "smug", "target": "Curious", "index": 1})
    assert r.json()["emote_refs"] == [
        "chibi:claudette/Happy_and_Confident/Happy.png",
        "chibi:claudette/Curiosity_and_Cunning/Curious.png",
    ]


async def test_repointing_refuses_a_non_admin_a_user_message_and_a_missing_tag(client):
    await make_user("alice")
    await make_user("plink", is_admin=True)
    bot_id = await make_resident()
    sent = await bot_message(client, bot_id, "[emotion: wry] ok", "wry")
    await login(client, "alice")
    r = await client.patch(f"/messages/{sent['id']}/emote", json={"tag": "wry", "target": "Happy"})
    assert r.status_code == 403

    main = (await db.fetch_one("SELECT id FROM channels WHERE type = 'main_feed'"))["id"]
    mine = (await client.post(f"/channels/{main}/messages",
                              json={"content": "[emotion: wry]"})).json()
    await login(client, "plink")
    r = await client.patch(f"/messages/{mine['id']}/emote", json={"tag": "wry", "target": "Happy"})
    assert r.status_code == 400
    r = await client.patch(f"/messages/{sent['id']}/emote", json={"tag": "glum", "target": "Happy"})
    assert r.status_code == 400
    r = await client.patch(f"/messages/{sent['id']}/emote", json={"tag": "wry", "target": "Sly"})
    assert r.status_code == 400


def test_a_tag_inside_code_is_not_a_tag():
    assert chibi_aliases.tags_in("`[emotion: no]` [emotion: Yes ] ```\n[emotion: no]\n```") == ["Yes"]


async def test_a_multi_tag_message_with_a_faceless_tag_refuses_rather_than_guess(admin):
    bot_id = await make_resident()
    sent = await bot_message(admin, bot_id, "[emotion: zzzq] then [emotion: happy]", "happy")
    await db.execute(
        "UPDATE messages SET emote_refs = ? WHERE id = ?",
        (json.dumps(["chibi:claudette/Happy_and_Confident/Happy.png"]), sent["id"]),
    )
    for tag, index in (("zzzq", 0), ("happy", 1)):
        r = await admin.patch(f"/messages/{sent['id']}/emote",
                              json={"tag": tag, "target": "Curious", "index": index})
        assert r.status_code == 409, r.text
    row = await db.fetch_one("SELECT emote_refs FROM messages WHERE id = ?", (sent["id"],))
    assert json.loads(row["emote_refs"]) == ["chibi:claudette/Happy_and_Confident/Happy.png"]
