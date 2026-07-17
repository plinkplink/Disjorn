"""WP2 auth tests: login, cookie sessions, sliding expiry, bot API keys, /me, CLI."""

import base64
import datetime
import os
import subprocess
import sys
from pathlib import Path
from typing import Annotated

from fastapi import Depends

from app import db
from app.models import Bot, User
from app.routers import auth

SERVER_DIR = Path(__file__).resolve().parent.parent

PASSWORD = "correct horse battery staple"


async def make_user(username: str = "alice", password: str = PASSWORD, **kw) -> int:
    cur = await db.execute(
        """INSERT INTO users (username, password_hash, display_name, is_admin)
           VALUES (?, ?, ?, ?)""",
        (
            username,
            auth.hash_password(password),
            kw.get("display_name", username.capitalize()),
            1 if kw.get("is_admin") else 0,
        ),
    )
    return cur.lastrowid


async def make_bot(name: str = "claw", api_key: str = "test-bot-key-123") -> int:
    cur = await db.execute(
        "INSERT INTO bots (name, api_key_hash) VALUES (?, ?)",
        (name, auth.hash_api_key(api_key)),
    )
    return cur.lastrowid


def _parse_ts(ts: str) -> datetime.datetime:
    return datetime.datetime.fromisoformat(ts.replace("Z", "+00:00"))


# ---------------------------------------------------------------------------
# Login
# ---------------------------------------------------------------------------

async def test_login_success_sets_cookie_and_returns_profile(client):
    await make_user("alice")
    r = await client.post("/auth/login", json={"username": "alice", "password": PASSWORD})
    assert r.status_code == 200
    body = r.json()
    assert body["username"] == "alice"
    assert body["display_name"] == "Alice"
    assert "password_hash" not in body
    set_cookie = r.headers["set-cookie"]
    assert "disjorn_session=" in set_cookie
    assert "HttpOnly" in set_cookie
    assert "samesite=lax" in set_cookie.lower()
    assert "Secure" not in set_cookie  # COOKIE_SECURE off in dev
    # Session row exists
    row = await db.fetch_one("SELECT * FROM sessions WHERE user_id = ?", (body["id"],))
    assert row is not None
    assert row["expires_at"] > db.utc_now()


async def test_login_wrong_password(client):
    await make_user("alice")
    r = await client.post("/auth/login", json={"username": "alice", "password": "nope"})
    assert r.status_code == 401
    assert "set-cookie" not in r.headers


async def test_login_unknown_user(client):
    r = await client.post("/auth/login", json={"username": "ghost", "password": "x"})
    assert r.status_code == 401


# ---------------------------------------------------------------------------
# Cookie session flow: /me, logout
# ---------------------------------------------------------------------------

async def test_me_requires_auth(client):
    r = await client.get("/me")
    assert r.status_code == 401


async def test_me_with_cookie_session(client):
    await make_user("alice")
    await client.post("/auth/login", json={"username": "alice", "password": PASSWORD})
    r = await client.get("/me")
    assert r.status_code == 200
    assert r.json()["username"] == "alice"


async def test_logout_invalidates_session(client):
    await make_user("alice")
    login = await client.post("/auth/login", json={"username": "alice", "password": PASSWORD})
    uid = login.json()["id"]
    r = await client.post("/auth/logout")
    assert r.status_code == 200
    # Session row gone, cookie cleared, /me now 401
    assert await db.fetch_one("SELECT * FROM sessions WHERE user_id = ?", (uid,)) is None
    assert 'disjorn_session=""' in r.headers["set-cookie"]
    assert (await client.get("/me")).status_code == 401


async def test_expired_session_rejected_and_deleted(client):
    uid = await make_user("alice")
    token = "expired-token-abc"
    past = "2020-01-01T00:00:00.000Z"
    await db.execute(
        "INSERT INTO sessions (token, user_id, created_at, expires_at) VALUES (?, ?, ?, ?)",
        (token, uid, past, past),
    )
    client.cookies.set("disjorn_session", token)
    r = await client.get("/me")
    assert r.status_code == 401
    assert await db.fetch_one("SELECT * FROM sessions WHERE token = ?", (token,)) is None


async def test_sliding_expiry_refreshed_on_use(client):
    await make_user("alice")
    login = await client.post("/auth/login", json={"username": "alice", "password": PASSWORD})
    uid = login.json()["id"]
    # Age the session down to 1 day remaining
    near = (
        datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=1)
    ).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
    await db.execute("UPDATE sessions SET expires_at = ? WHERE user_id = ?", (near, uid))
    assert (await client.get("/me")).status_code == 200
    row = await db.fetch_one("SELECT expires_at FROM sessions WHERE user_id = ?", (uid,))
    refreshed = _parse_ts(row["expires_at"])
    now = datetime.datetime.now(datetime.timezone.utc)
    assert refreshed > now + datetime.timedelta(days=29)
    assert refreshed < now + datetime.timedelta(days=31)


# ---------------------------------------------------------------------------
# PATCH /me
# ---------------------------------------------------------------------------

async def test_patch_me_updates_profile(client):
    await make_user("alice")
    await client.post("/auth/login", json={"username": "alice", "password": PASSWORD})
    r = await client.patch("/me", json={"display_name": "Allie", "status": "idle"})
    assert r.status_code == 200
    assert r.json()["display_name"] == "Allie"
    assert r.json()["status"] == "idle"
    row = await db.fetch_one("SELECT display_name, status FROM users WHERE username = 'alice'")
    assert (row["display_name"], row["status"]) == ("Allie", "idle")


async def test_patch_me_partial_and_invalid(client):
    await make_user("alice")
    await client.post("/auth/login", json={"username": "alice", "password": PASSWORD})
    r = await client.patch("/me", json={"status": "dnd"})
    assert r.status_code == 200
    assert r.json()["display_name"] == "Alice"  # unchanged
    assert r.json()["status"] == "dnd"
    assert (await client.patch("/me", json={"status": "invisible"})).status_code == 422
    assert (await client.patch("/me", json={"display_name": ""})).status_code == 422
    assert (await client.patch("/me", json={})).status_code == 200  # no-op ok


# ---------------------------------------------------------------------------
# Bot API-key auth + get_actor
# ---------------------------------------------------------------------------

async def test_bot_api_key_auth(app, client):
    @app.get("/_test/bot")
    async def whoami_bot(bot: Annotated[Bot, Depends(auth.get_current_bot)]) -> Bot:
        return bot

    await make_bot("claw", api_key="sekrit-key-1")
    r = await client.get("/_test/bot", headers={"X-Api-Key": "sekrit-key-1"})
    assert r.status_code == 200
    assert r.json()["name"] == "claw"
    assert "api_key_hash" not in r.json()
    assert (await client.get("/_test/bot", headers={"X-Api-Key": "wrong"})).status_code == 401
    assert (await client.get("/_test/bot")).status_code == 401


async def test_get_actor_user_bot_or_401(app, client):
    @app.get("/_test/actor")
    async def whoami(actor: Annotated[auth.Actor, Depends(auth.get_actor)]) -> dict:
        return {"type": actor.type, "id": actor.id}

    assert (await client.get("/_test/actor")).status_code == 401

    bot_id = await make_bot("claw", api_key="sekrit-key-2")
    r = await client.get("/_test/actor", headers={"X-Api-Key": "sekrit-key-2"})
    assert r.json() == {"type": "bot", "id": bot_id}

    uid = await make_user("alice")
    await client.post("/auth/login", json={"username": "alice", "password": PASSWORD})
    r = await client.get("/_test/actor")
    assert r.json() == {"type": "user", "id": uid}


# ---------------------------------------------------------------------------
# CLI (subprocess against a tmp DB)
# ---------------------------------------------------------------------------

def _run_cli(args: list[str], db_path: Path, input_text: str | None = None):
    env = os.environ | {"DB_PATH": str(db_path), "DATA_DIR": str(db_path.parent)}
    return subprocess.run(
        [sys.executable, "cli.py", *args],
        cwd=SERVER_DIR,
        env=env,
        input=input_text,
        capture_output=True,
        text=True,
    )


def test_cli_create_user_password_stdin_and_create_bot(tmp_path):
    import sqlite3

    dbfile = tmp_path / "cli.db"
    r = _run_cli(
        ["create-user", "bob", "--display-name", "Bob T.", "--admin", "--password-stdin"],
        dbfile,
        input_text="hunter2\n",
    )
    assert r.returncode == 0, r.stderr
    assert "Created user 'bob'" in r.stdout and "[admin]" in r.stdout

    # Duplicate username fails cleanly
    r2 = _run_cli(["create-user", "bob", "--password-stdin"], dbfile, input_text="x\n")
    assert r2.returncode != 0 and "already exists" in r2.stderr

    r3 = _run_cli(["create-bot", "claudette"], dbfile)
    assert r3.returncode == 0, r3.stderr
    api_key = r3.stdout.strip().splitlines()[-1].strip()
    assert len(api_key) > 30

    conn = sqlite3.connect(dbfile)
    conn.row_factory = sqlite3.Row
    user = conn.execute("SELECT * FROM users WHERE username = 'bob'").fetchone()
    assert user["display_name"] == "Bob T." and user["is_admin"] == 1
    assert auth.verify_password(user["password_hash"], "hunter2")
    bot = conn.execute("SELECT * FROM bots WHERE name = 'claudette'").fetchone()
    assert bot["api_key_hash"] == auth.hash_api_key(api_key)
    member = conn.execute(
        """SELECT cm.* FROM channel_members cm
           JOIN channels c ON c.id = cm.channel_id
           WHERE c.type = 'main_feed' AND cm.member_type = 'bot' AND cm.member_id = ?""",
        (bot["id"],),
    ).fetchone()
    assert member is not None
    conn.close()


def test_cli_gen_vapid(tmp_path):
    r = _run_cli(["gen-vapid"], tmp_path / "unused.db")
    assert r.returncode == 0, r.stderr
    lines = dict(
        line.split("=", 1) for line in r.stdout.splitlines() if "=" in line and not line.startswith("#")
    )
    pub = lines["VAPID_PUBLIC_KEY"]
    priv = lines["VAPID_PRIVATE_KEY"]

    def unb64url(s: str) -> bytes:
        return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))

    pub_bytes = unb64url(pub)
    assert len(pub_bytes) == 65 and pub_bytes[0] == 0x04  # uncompressed P-256 point
    assert len(unb64url(priv)) == 32
