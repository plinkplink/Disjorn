"""Every admin password reset leaves a record, and the account holder is told once."""

import os
import sqlite3
import subprocess
import sys

from app import db
from app.services.accounts import CONSOLE_ACTOR

from .test_password import NEW_PASSWORD, PASSWORD, SERVER_DIR, login, make_user


async def events():
    return await db.fetch_all("SELECT * FROM account_events ORDER BY id")


async def reset_bob(client):
    root_id = await make_user("root", is_admin=True)
    bob_id = await make_user("bob")
    await login(client, "root")
    r = await client.post(
        f"/auth/users/{bob_id}/password", json={"new_password": NEW_PASSWORD}
    )
    assert r.status_code == 200
    client.cookies.clear()
    return root_id, bob_id


async def test_an_admin_reset_records_who_reset_whom_and_never_the_password(client):
    root_id, bob_id = await reset_bob(client)
    [row] = await events()
    assert row["user_id"] == bob_id and row["kind"] == "password_reset"
    assert row["actor_user_id"] == root_id and row["actor_name"] == "Root"
    assert row["created_at"] and row["seen_at"] is None
    assert not any(NEW_PASSWORD in str(v) for v in row.values())


async def test_a_refused_reset_records_nothing(client):
    root_id = await make_user("root", is_admin=True)
    await login(client, "root")
    await client.post(f"/auth/users/{root_id}/password", json={"new_password": NEW_PASSWORD})
    await client.post("/auth/users/999/password", json={"new_password": NEW_PASSWORD})
    assert await events() == []


async def test_the_next_login_shows_the_reset_once(client):
    _, _ = await reset_bob(client)
    first = (await login(client, "bob", NEW_PASSWORD)).json()
    assert first["password_reset"]["by"] == "Root"
    assert first["password_reset"]["at"] == (await events())[0]["created_at"]
    second = (await login(client, "bob", NEW_PASSWORD)).json()
    assert second["password_reset"] is None
    assert (await events())[0]["seen_at"] is not None


async def test_a_login_with_no_reset_carries_no_notice(client):
    await make_user("alice")
    body = (await login(client, "alice", PASSWORD)).json()
    assert body["username"] == "alice" and body["password_reset"] is None


def test_a_console_reset_is_recorded_and_resolves_the_request(tmp_path):
    dbfile = tmp_path / "cli.db"
    env = os.environ | {"DB_PATH": str(dbfile), "DATA_DIR": str(tmp_path)}

    def cli(*argv: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, "cli.py", *argv, "--password-stdin"],
            cwd=SERVER_DIR, env=env, input="console-handover-pw\n",
            capture_output=True, text=True,
        )

    assert cli("create-user", "erin").returncode == 0
    conn = sqlite3.connect(dbfile)
    conn.row_factory = sqlite3.Row
    with conn:
        conn.execute(
            "INSERT INTO password_reset_requests (user_id) "
            "SELECT id FROM users WHERE username = 'erin'"
        )
    assert cli("reset-password", "erin").returncode == 0

    event = conn.execute("SELECT * FROM account_events").fetchone()
    assert event["actor_user_id"] is None and event["actor_name"] == CONSOLE_ACTOR
    request = conn.execute("SELECT * FROM password_reset_requests").fetchone()
    assert request["status"] == "resolved" and request["resolved_by"] is None
    conn.close()
