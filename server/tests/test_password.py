"""Password change, first-login rotation, and admin reset.

The load-bearing tests here are the eviction ones: a password change that does
not end the other sessions evicts nobody and leaves whoever handed the password
over still logged in.
"""

import os
import subprocess
import sqlite3
import sys
from pathlib import Path

import httpx
import pytest

from app import db
from app.routers import auth

SERVER_DIR = Path(__file__).resolve().parent.parent
MIGRATIONS_DIR = SERVER_DIR / "app" / "migrations"
ROTATION_MIGRATION = "007_password_rotation.sql"

PASSWORD = "correct horse battery staple"
NEW_PASSWORD = "a different long password"


async def make_user(
    username: str = "alice",
    password: str = PASSWORD,
    *,
    is_admin: bool = False,
    must_change_password: bool = False,
) -> int:
    cur = await db.execute(
        """INSERT INTO users (username, password_hash, display_name, is_admin,
                              must_change_password)
           VALUES (?, ?, ?, ?, ?)""",
        (
            username,
            auth.hash_password(password),
            username.capitalize(),
            1 if is_admin else 0,
            1 if must_change_password else 0,
        ),
    )
    return cur.lastrowid


async def make_bot(name: str = "claw", api_key: str = "bot-key-pw-1") -> int:
    cur = await db.execute(
        "INSERT INTO bots (name, api_key_hash) VALUES (?, ?)",
        (name, auth.hash_api_key(api_key)),
    )
    return cur.lastrowid


async def login(client, username: str = "alice", password: str = PASSWORD):
    return await client.post(
        "/auth/login", json={"username": username, "password": password}
    )


@pytest.fixture
async def second_client(app):
    """A second, independent cookie jar against the same app — a second device."""
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def password_hash_of(user_id: int) -> str:
    row = await db.fetch_one("SELECT password_hash FROM users WHERE id = ?", (user_id,))
    return row["password_hash"]


async def flag_of(user_id: int) -> int:
    row = await db.fetch_one(
        "SELECT must_change_password FROM users WHERE id = ?", (user_id,)
    )
    return row["must_change_password"]


async def session_tokens(user_id: int) -> set[str]:
    rows = await db.fetch_all("SELECT token FROM sessions WHERE user_id = ?", (user_id,))
    return {r["token"] for r in rows}


# ---------------------------------------------------------------------------
# POST /auth/password — the happy path
# ---------------------------------------------------------------------------

async def test_change_password_requires_authentication(client):
    r = await client.post(
        "/auth/password",
        json={"current_password": PASSWORD, "new_password": NEW_PASSWORD},
    )
    assert r.status_code == 401


async def test_change_password_swaps_the_credential(client):
    uid = await make_user("alice")
    await login(client)
    before = await password_hash_of(uid)

    r = await client.post(
        "/auth/password",
        json={"current_password": PASSWORD, "new_password": NEW_PASSWORD},
    )
    assert r.status_code == 200, r.text
    assert r.json() == {"ok": True}
    assert await password_hash_of(uid) != before

    # The old password is dead, the new one works.
    await client.post("/auth/logout")
    assert (await login(client, password=PASSWORD)).status_code == 401
    assert (await login(client, password=NEW_PASSWORD)).status_code == 200


async def test_calling_session_survives_the_change(client):
    await make_user("alice")
    await login(client)
    r = await client.post(
        "/auth/password",
        json={"current_password": PASSWORD, "new_password": NEW_PASSWORD},
    )
    assert r.status_code == 200
    # Still logged in on this cookie — no forced re-login in the tab you used.
    assert (await client.get("/me")).status_code == 200


# ---------------------------------------------------------------------------
# Session eviction — the half that matters
# ---------------------------------------------------------------------------

async def test_change_password_ends_every_other_session(client, second_client):
    """The other device holding this account's cookie is signed out; ours is not."""
    uid = await make_user("alice")
    await login(client)
    await login(second_client)

    caller_token = client.cookies["disjorn_session"]
    other_token = second_client.cookies["disjorn_session"]
    assert caller_token != other_token
    assert await session_tokens(uid) == {caller_token, other_token}
    assert (await second_client.get("/me")).status_code == 200

    r = await client.post(
        "/auth/password",
        json={"current_password": PASSWORD, "new_password": NEW_PASSWORD},
    )
    assert r.status_code == 200

    # Exactly the caller's session is left standing, in the DB and over HTTP.
    assert await session_tokens(uid) == {caller_token}
    assert (await second_client.get("/me")).status_code == 401
    assert (await client.get("/me")).status_code == 200


async def test_change_password_leaves_other_users_sessions_alone(client, second_client):
    await make_user("alice")
    bob_id = await make_user("bob")
    await login(client)
    await login(second_client, username="bob")

    await client.post(
        "/auth/password",
        json={"current_password": PASSWORD, "new_password": NEW_PASSWORD},
    )
    assert len(await session_tokens(bob_id)) == 1
    assert (await second_client.get("/me")).status_code == 200


# ---------------------------------------------------------------------------
# Refusals — nothing is written on any of these paths
# ---------------------------------------------------------------------------

async def test_wrong_current_password_is_403_and_changes_nothing(client, second_client):
    uid = await make_user("alice")
    await login(client)
    await login(second_client)
    before = await password_hash_of(uid)
    tokens_before = await session_tokens(uid)

    r = await client.post(
        "/auth/password",
        json={"current_password": "not the password", "new_password": NEW_PASSWORD},
    )
    assert r.status_code == 403
    assert r.json()["detail"] == "Current password is incorrect"
    assert await password_hash_of(uid) == before
    assert await session_tokens(uid) == tokens_before  # no eviction either
    assert (await second_client.get("/me")).status_code == 200


async def test_new_password_must_differ_from_current(client):
    uid = await make_user("alice")
    await login(client)
    before = await password_hash_of(uid)
    r = await client.post(
        "/auth/password",
        json={"current_password": PASSWORD, "new_password": PASSWORD},
    )
    assert r.status_code == 400
    assert await password_hash_of(uid) == before


async def test_new_password_minimum_length(client):
    uid = await make_user("alice")
    await login(client)
    before = await password_hash_of(uid)
    short = "x" * (auth.PASSWORD_MIN_LENGTH - 1)
    r = await client.post(
        "/auth/password", json={"current_password": PASSWORD, "new_password": short}
    )
    assert r.status_code == 422
    assert short not in r.text  # refusals never echo the submitted value
    assert await password_hash_of(uid) == before

    ok = "y" * auth.PASSWORD_MIN_LENGTH
    r2 = await client.post(
        "/auth/password", json={"current_password": PASSWORD, "new_password": ok}
    )
    assert r2.status_code == 200


# ---------------------------------------------------------------------------
# First-login rotation gate
# ---------------------------------------------------------------------------

async def test_rotation_pending_user_can_still_log_in(client):
    await make_user("alice", must_change_password=True)
    r = await login(client)
    assert r.status_code == 200
    assert (await client.get("/me")).status_code == 200


async def test_rotation_pending_user_is_403_everywhere_else(client):
    await make_user("alice", must_change_password=True)
    await login(client)

    for method, path in (("GET", "/channels"), ("PATCH", "/me")):
        r = await client.request(method, path, json={} if method == "PATCH" else None)
        assert r.status_code == 403, f"{method} {path} -> {r.status_code}"
        assert r.json()["detail"] == auth.PASSWORD_CHANGE_REQUIRED


async def test_rotation_gate_covers_actor_authenticated_routes(client):
    """GET /bots authenticates an Actor, not a User — still walled off."""
    await make_user("alice", must_change_password=True)
    await login(client)
    r = await client.get("/bots")
    assert r.status_code == 403
    assert r.json()["detail"] == auth.PASSWORD_CHANGE_REQUIRED


async def test_rotation_gate_covers_admin_routes(client):
    await make_user("root", is_admin=True, must_change_password=True)
    bot_id = await make_bot("claw")
    await login(client, username="root")
    r = await client.patch(f"/bots/{bot_id}", json={"chibi_pack": None})
    assert r.status_code == 403
    assert r.json()["detail"] == auth.PASSWORD_CHANGE_REQUIRED


async def test_rotation_gate_does_not_touch_bots(client):
    """Bots have no password to rotate; their API keys are a separate ticket."""
    await make_bot("claw", api_key="bot-key-pw-2")
    r = await client.get("/bots", headers={"X-Api-Key": "bot-key-pw-2"})
    assert r.status_code == 200


async def test_rotation_pending_user_may_log_out(client):
    await make_user("alice", must_change_password=True)
    await login(client)
    assert (await client.post("/auth/logout")).status_code == 200


async def test_changing_password_clears_the_gate(client):
    uid = await make_user("alice", must_change_password=True)
    await login(client)
    assert await flag_of(uid) == 1

    r = await client.post(
        "/auth/password",
        json={"current_password": PASSWORD, "new_password": NEW_PASSWORD},
    )
    assert r.status_code == 200
    assert await flag_of(uid) == 0
    assert (await client.get("/channels")).status_code == 200


async def test_failed_change_leaves_the_gate_up(client):
    uid = await make_user("alice", must_change_password=True)
    await login(client)
    r = await client.post(
        "/auth/password",
        json={"current_password": "wrong", "new_password": NEW_PASSWORD},
    )
    assert r.status_code == 403
    assert await flag_of(uid) == 1
    assert (await client.get("/channels")).status_code == 403


# ---------------------------------------------------------------------------
# Admin reset
# ---------------------------------------------------------------------------

async def test_admin_reset_marks_rotation_and_kills_every_session(
    client, second_client
):
    await make_user("root", is_admin=True)
    bob_id = await make_user("bob")
    await login(second_client, username="bob")
    assert len(await session_tokens(bob_id)) == 1

    await login(client, username="root")
    r = await client.post(
        f"/auth/users/{bob_id}/password", json={"new_password": NEW_PASSWORD}
    )
    assert r.status_code == 200, r.text
    assert await session_tokens(bob_id) == set()  # all of them, not all-but-one
    assert (await second_client.get("/me")).status_code == 401
    assert await flag_of(bob_id) == 1

    # Bob logs in with what the admin handed over, and can do nothing but rotate.
    assert (await login(second_client, "bob", NEW_PASSWORD)).status_code == 200
    assert (await second_client.get("/channels")).status_code == 403
    assert (
        await second_client.post(
            "/auth/password",
            json={"current_password": NEW_PASSWORD, "new_password": "bobs own secret pw"},
        )
    ).status_code == 200
    assert await flag_of(bob_id) == 0
    assert (await second_client.get("/channels")).status_code == 200


async def test_admin_reset_requires_admin(client):
    await make_user("alice")
    bob_id = await make_user("bob")
    before = await password_hash_of(bob_id)
    await login(client)
    r = await client.post(
        f"/auth/users/{bob_id}/password", json={"new_password": NEW_PASSWORD}
    )
    assert r.status_code == 403
    assert await password_hash_of(bob_id) == before


async def test_admin_reset_unknown_user_and_self(client):
    root_id = await make_user("root", is_admin=True)
    await login(client, username="root")

    r = await client.post(
        "/auth/users/9999/password", json={"new_password": NEW_PASSWORD}
    )
    assert r.status_code == 404

    # An admin resets their own password through the self-service route, which
    # keeps their session and does not make them rotate twice.
    before = await password_hash_of(root_id)
    r2 = await client.post(
        f"/auth/users/{root_id}/password", json={"new_password": NEW_PASSWORD}
    )
    assert r2.status_code == 400
    assert await password_hash_of(root_id) == before
    assert await flag_of(root_id) == 0


async def test_admin_reset_minimum_length(client):
    await make_user("root", is_admin=True)
    bob_id = await make_user("bob")
    before = await password_hash_of(bob_id)
    await login(client, username="root")
    r = await client.post(
        f"/auth/users/{bob_id}/password",
        json={"new_password": "x" * (auth.PASSWORD_MIN_LENGTH - 1)},
    )
    assert r.status_code == 422
    assert await password_hash_of(bob_id) == before


# ---------------------------------------------------------------------------
# Migration + CLI
# ---------------------------------------------------------------------------

async def test_migration_marks_existing_accounts_for_rotation(tmp_db_path):
    """Rows that predate 007 come out of it owing a rotation.

    Applies 001..006 by hand, plants a user the way the old schema would have,
    then lets run_migrations() apply 007 on top — the upgrade path a live DB
    takes, which a fresh-schema test would never exercise.
    """
    await db.close()
    conn = await db.connect(tmp_db_path)
    try:
        await conn.execute(
            """CREATE TABLE IF NOT EXISTS schema_migrations (
                   filename TEXT PRIMARY KEY,
                   applied_at TEXT NOT NULL
               )"""
        )
        for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
            if path.name >= ROTATION_MIGRATION:  # zero-padded prefixes sort
                continue
            await conn.executescript(path.read_text(encoding="utf-8"))
            await conn.execute(
                "INSERT INTO schema_migrations (filename, applied_at) VALUES (?, ?)",
                (path.name, db.utc_now()),
            )
        await conn.commit()

        cur = await db.execute(
            """INSERT INTO users (username, password_hash, display_name, is_admin)
               VALUES ('legacy', 'x', 'Legacy', 0)""",
        )
        legacy_id = cur.lastrowid

        applied = await db.run_migrations()
        assert ROTATION_MIGRATION in applied
        assert await flag_of(legacy_id) == 1
    finally:
        await db.close()


def test_cli_created_accounts_owe_a_rotation(tmp_path):
    dbfile = tmp_path / "cli.db"
    env = os.environ | {"DB_PATH": str(dbfile), "DATA_DIR": str(tmp_path)}
    r = subprocess.run(
        [sys.executable, "cli.py", "create-user", "carol", "--password-stdin"],
        cwd=SERVER_DIR,
        env=env,
        input="handed-over-password\n",
        capture_output=True,
        text=True,
    )
    assert r.returncode == 0, r.stderr

    conn = sqlite3.connect(dbfile)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM users WHERE username = 'carol'").fetchone()
    conn.close()
    assert row["must_change_password"] == 1
