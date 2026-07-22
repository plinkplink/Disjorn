"""Bot cosmetics admin surface (routers/bots_admin.py) + cli.py create-bot.

Closes the DEFERRED item "No admin surface for bot cosmetics": before this,
setting bots.chibi_pack or a bot avatar meant editing the DB by hand.

Covers: the public read shape (never leaks api_key_hash), the admin gate on
every mutation (anonymous / non-admin user / bot API key all refused), chibi
pack validation + clearing, the bot avatar upload reusing the human avatar
path, and `create-bot --chibi-pack`.
"""

import argparse
import io
import shutil
from pathlib import Path

import pytest
from PIL import Image

from app import db
from app.config import get_settings
from app.routers import auth
from app.services import chibi

PASSWORD = "correct horse battery staple"
BOT_KEY = "bot-key-1"

FIXTURE_PACK = Path(__file__).parent / "fixtures" / "chibi_pack_sample"


@pytest.fixture(autouse=True)
def clear_chibi_cache():
    chibi.clear_cache()
    yield
    chibi.clear_cache()


async def make_user(username: str, *, is_admin: bool = False) -> int:
    cur = await db.execute(
        """INSERT INTO users (username, password_hash, display_name, is_admin)
           VALUES (?, ?, ?, ?)""",
        (username, auth.hash_password(PASSWORD), username.capitalize(), 1 if is_admin else 0),
    )
    return cur.lastrowid


async def make_bot(name: str = "claw", api_key: str = BOT_KEY) -> int:
    cur = await db.execute(
        "INSERT INTO bots (name, api_key_hash) VALUES (?, ?)",
        (name, auth.hash_api_key(api_key)),
    )
    return cur.lastrowid


async def login(client, username: str) -> None:
    r = await client.post("/auth/login", json={"username": username, "password": PASSWORD})
    assert r.status_code == 200


def png_bytes(size=(256, 256), color=(30, 140, 210)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, format="PNG")
    return buf.getvalue()


def install_pack(name: str = "sample_pack") -> str:
    """Copy the fixture pack into DATA_DIR/assets/chibi_packs/<name>."""
    dest = chibi.packs_root() / name
    dest.parent.mkdir(parents=True, exist_ok=True)
    if not dest.exists():
        shutil.copytree(FIXTURE_PACK, dest)
    return name


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------

async def test_list_and_get_bots_public_shape(client):
    await make_user("alice")
    bot_id = await make_bot()
    await login(client, "alice")

    listed = (await client.get("/bots")).json()
    names = {b["name"] for b in listed}
    assert {"system", "claw"} <= names
    for bot in listed:
        assert set(bot) == {
            "id", "name", "avatar_path", "avatar_url", "chibi_pack", "created_at",
        }
        # No avatar -> no URL, so a member panel never fires a doomed request.
        assert bot["avatar_url"] is None
        assert "api_key_hash" not in bot

    one = await client.get(f"/bots/{bot_id}")
    assert one.status_code == 200 and one.json()["name"] == "claw"
    assert (await client.get("/bots/99999")).status_code == 404


async def test_bot_reads_require_auth_and_work_for_bots(client):
    await make_bot()
    assert (await client.get("/bots")).status_code == 401
    r = await client.get("/bots", headers={"X-Api-Key": BOT_KEY})
    assert r.status_code == 200


# ---------------------------------------------------------------------------
# Admin gate
# ---------------------------------------------------------------------------

async def test_cosmetics_mutations_are_admin_only(client):
    # Regression: bot cosmetics must not be settable by just anyone with an
    # account, and NEVER by an API key — a leaked bot key must not be able to
    # repaint another bot.
    await make_user("alice")                     # not an admin
    bot_id = await make_bot()

    body = {"chibi_pack": None}
    avatar = [("file", ("a.png", png_bytes(), "image/png"))]

    # Anonymous -> 401
    assert (await client.patch(f"/bots/{bot_id}", json=body)).status_code == 401
    assert (await client.post(f"/bots/{bot_id}/avatar", files=avatar)).status_code == 401
    assert (await client.delete(f"/bots/{bot_id}/avatar")).status_code == 401

    # Bot API key -> 401 (the admin gate is cookie-only)
    hdrs = {"X-Api-Key": BOT_KEY}
    assert (await client.patch(f"/bots/{bot_id}", json=body, headers=hdrs)).status_code == 401
    assert (
        await client.post(f"/bots/{bot_id}/avatar", files=avatar, headers=hdrs)
    ).status_code == 401

    # Logged-in non-admin -> 403
    await login(client, "alice")
    assert (await client.patch(f"/bots/{bot_id}", json=body)).status_code == 403
    assert (await client.post(f"/bots/{bot_id}/avatar", files=avatar)).status_code == 403
    assert (await client.delete(f"/bots/{bot_id}/avatar")).status_code == 403

    row = await db.fetch_one("SELECT * FROM bots WHERE id = ?", (bot_id,))
    assert row["chibi_pack"] is None and row["avatar_path"] is None


# ---------------------------------------------------------------------------
# chibi_pack
# ---------------------------------------------------------------------------

async def test_admin_sets_and_clears_chibi_pack(client):
    await make_user("root", is_admin=True)
    bot_id = await make_bot()
    await login(client, "root")
    pack = install_pack()

    r = await client.patch(f"/bots/{bot_id}", json={"chibi_pack": pack})
    assert r.status_code == 200, r.text
    assert r.json()["chibi_pack"] == pack
    row = await db.fetch_one("SELECT chibi_pack FROM bots WHERE id = ?", (bot_id,))
    assert row["chibi_pack"] == pack

    # Explicit null clears it (the one way to unset without a DB edit).
    r = await client.patch(f"/bots/{bot_id}", json={"chibi_pack": None})
    assert r.status_code == 200 and r.json()["chibi_pack"] is None

    # Omitting the field leaves it alone.
    await client.patch(f"/bots/{bot_id}", json={"chibi_pack": pack})
    r = await client.patch(f"/bots/{bot_id}", json={})
    assert r.json()["chibi_pack"] == pack


async def test_chibi_pack_is_validated(client):
    # A pack that does not resolve silently disables every `emotion` the bot
    # sends, which is exactly the "cosmetic set by hand, quietly broken"
    # failure the admin surface exists to prevent.
    await make_user("root", is_admin=True)
    bot_id = await make_bot()
    await login(client, "root")

    for bad in ("no_such_pack", "/nonexistent/path/pack", "   "):
        r = await client.patch(f"/bots/{bot_id}", json={"chibi_pack": bad})
        assert r.status_code == 400, f"{bad!r} -> {r.status_code}"
    row = await db.fetch_one("SELECT chibi_pack FROM bots WHERE id = ?", (bot_id,))
    assert row["chibi_pack"] is None


async def test_configured_pack_actually_resolves_emotions(client):
    """End-to-end: the admin-set pack is the one messages.py resolves against."""
    await make_user("root", is_admin=True)
    bot_id = await make_bot()
    await login(client, "root")
    await client.patch(f"/bots/{bot_id}", json={"chibi_pack": install_pack()})

    main = (await db.fetch_one("SELECT id FROM channels WHERE type = 'main_feed'"))["id"]
    await db.execute(
        "INSERT INTO channel_members (channel_id, member_type, member_id) VALUES (?, 'bot', ?)",
        (main, bot_id),
    )
    client.cookies.clear()
    r = await client.post(
        f"/channels/{main}/messages",
        json={"content": "hah", "emotion": "Smug"},
        headers={"X-Api-Key": BOT_KEY},
    )
    assert r.status_code == 200
    assert r.json()["emote_refs"] == ["chibi:sample_pack/Happy_and_Confident/Smug.png"]


async def test_system_author_is_not_configurable(client):
    await make_user("root", is_admin=True)
    await login(client, "root")
    sys_id = (await db.fetch_one("SELECT id FROM bots WHERE name = 'system'"))["id"]

    assert (await client.patch(f"/bots/{sys_id}", json={"chibi_pack": None})).status_code == 400
    r = await client.post(
        f"/bots/{sys_id}/avatar", files=[("file", ("a.png", png_bytes(), "image/png"))]
    )
    assert r.status_code == 400


# ---------------------------------------------------------------------------
# Bot avatars
# ---------------------------------------------------------------------------

async def test_bot_avatar_upload_serve_and_clear(client):
    await make_user("root", is_admin=True)
    bot_id = await make_bot()
    await login(client, "root")

    r = await client.post(
        f"/bots/{bot_id}/avatar",
        files=[("file", ("logo.png", png_bytes((800, 600)), "image/png"))],
    )
    assert r.status_code == 200, r.text
    # Same conversion path as POST /me/avatar: 256px WebP under avatars/.
    assert r.json()["avatar_path"] == f"avatars/bot_{bot_id}.webp"
    assert r.json()["url"].startswith(f"/bots/{bot_id}/avatar?v=")
    assert (get_settings().data_dir / f"avatars/bot_{bot_id}.webp").is_file()
    row = await db.fetch_one("SELECT avatar_path FROM bots WHERE id = ?", (bot_id,))
    assert row["avatar_path"] == f"avatars/bot_{bot_id}.webp"

    # Serving is unsigned, exactly like /avatars/{user_id}.
    client.cookies.clear()
    r = await client.get(f"/bots/{bot_id}/avatar")
    assert r.status_code == 200 and r.headers["content-type"] == "image/webp"
    with Image.open(io.BytesIO(r.content)) as im:
        assert max(im.size) <= 256

    await login(client, "root")
    assert (await client.delete(f"/bots/{bot_id}/avatar")).status_code == 200
    assert (await client.get(f"/bots/{bot_id}/avatar")).status_code == 404


async def test_bot_avatar_rejects_non_images_and_unknown_bots(client):
    await make_user("root", is_admin=True)
    await login(client, "root")
    bot_id = await make_bot()

    r = await client.post(
        f"/bots/{bot_id}/avatar", files=[("file", ("x.txt", b"not an image", "text/plain"))]
    )
    assert r.status_code == 400
    r = await client.post(
        f"/bots/99999/avatar", files=[("file", ("a.png", png_bytes(), "image/png"))]
    )
    assert r.status_code == 404
    assert (await client.get(f"/bots/{bot_id}/avatar")).status_code == 404


async def test_bot_avatar_appears_in_message_payload(client):
    """The reason this exists: message authors carry avatar_path."""
    await make_user("root", is_admin=True)
    bot_id = await make_bot()
    await login(client, "root")
    await client.post(
        f"/bots/{bot_id}/avatar", files=[("file", ("a.png", png_bytes(), "image/png"))]
    )

    main = (await db.fetch_one("SELECT id FROM channels WHERE type = 'main_feed'"))["id"]
    await db.execute(
        "INSERT INTO channel_members (channel_id, member_type, member_id) VALUES (?, 'bot', ?)",
        (main, bot_id),
    )
    client.cookies.clear()
    r = await client.post(
        f"/channels/{main}/messages", json={"content": "hi"}, headers={"X-Api-Key": BOT_KEY}
    )
    assert r.json()["author"]["avatar_path"] == f"avatars/bot_{bot_id}.webp"
    assert r.json()["author"]["avatar_url"].startswith(f"/bots/{bot_id}/avatar?v=")


async def test_bot_avatar_reupload_changes_the_cache_key(client):
    """Regression: avatars are served `public, max-age=300` from a path that is
    stable across re-uploads (avatars/bot_{id}.webp), so a repainted bot kept
    showing its old face for up to 5 minutes. Every surface that hands out an
    avatar URL must carry a version that moves when the bytes do."""
    await make_user("root", is_admin=True)
    bot_id = await make_bot()
    await login(client, "root")

    async def urls() -> set[str]:
        listed = next(b for b in (await client.get("/bots")).json() if b["id"] == bot_id)
        one = (await client.get(f"/bots/{bot_id}")).json()
        assert listed["avatar_url"] == one["avatar_url"]
        return {listed["avatar_url"]}

    first = (
        await client.post(
            f"/bots/{bot_id}/avatar",
            files=[("file", ("a.png", png_bytes((64, 64)), "image/png"))],
        )
    ).json()["url"]
    assert await urls() == {first}

    second = (
        await client.post(
            f"/bots/{bot_id}/avatar",
            files=[("file", ("b.png", png_bytes((128, 128)), "image/png"))],
        )
    ).json()["url"]
    assert second != first, "re-upload reused the cache key — stale avatar"
    assert await urls() == {second}

    # The versioned URL is a real, servable URL — the ?v= is inert to the route.
    assert (await client.get(second)).status_code == 200

    # Clearing drops the URL entirely (no request to 404 on).
    assert (await client.delete(f"/bots/{bot_id}/avatar")).status_code == 200
    assert await urls() == {None}


# ---------------------------------------------------------------------------
# cli.py create-bot --chibi-pack
# ---------------------------------------------------------------------------

async def test_cli_create_bot_with_chibi_pack(app, capsys):
    import cli

    pack = install_pack()
    await cli.cmd_create_bot(argparse.Namespace(name="chibibot", chibi_pack=pack))

    row = await db.fetch_one("SELECT * FROM bots WHERE name = 'chibibot'")
    assert row is not None and row["chibi_pack"] == pack
    out = capsys.readouterr().out
    assert f"Chibi pack: {pack}" in out
    assert "API key (shown ONCE" in out


async def test_cli_create_bot_refuses_unresolvable_pack(app, capsys):
    import cli

    with pytest.raises(SystemExit):
        await cli.cmd_create_bot(
            argparse.Namespace(name="ghostbot", chibi_pack="no_such_pack_anywhere")
        )
    assert await db.fetch_one("SELECT * FROM bots WHERE name = 'ghostbot'") is None


async def test_cli_create_bot_without_pack_still_works(app):
    import cli

    await cli.cmd_create_bot(argparse.Namespace(name="plainbot", chibi_pack=None))
    row = await db.fetch_one("SELECT * FROM bots WHERE name = 'plainbot'")
    assert row is not None and row["chibi_pack"] is None
