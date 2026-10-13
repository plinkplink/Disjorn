"""Asking an admin to reset a password from the login page."""

import asyncio

import httpx
import pytest

from app import db
from app.routers import reset_requests
from app.services import push

from .test_password import NEW_PASSWORD, login, make_user

GENERIC = {"detail": "If that account exists, an admin has been asked."}


@pytest.fixture
def pushes(monkeypatch):
    sent: list[tuple[list[int], dict]] = []

    async def fake_notify(user_ids, payload):
        sent.append((list(user_ids), payload))

    monkeypatch.setattr(push, "notify_users", fake_notify)
    return sent


def client_from(app, house_origin, ip):
    transport = httpx.ASGITransport(app=app, client=(ip, 4242))
    return httpx.AsyncClient(
        transport=transport, base_url=house_origin, headers={"Origin": house_origin}
    )


async def ask(client, username, note=None):
    body = {"username": username}
    if note is not None:
        body["note"] = note
    r = await client.post("/auth/reset-requests", json=body)
    await reset_requests.wait_pending()
    return r


async def open_requests():
    return await db.fetch_all(
        "SELECT * FROM password_reset_requests WHERE status = 'open'"
    )


async def test_known_and_unknown_usernames_get_the_same_answer(client, pushes):
    await make_user("alice")
    known = await ask(client, "alice")
    unknown = await ask(client, "nobody")
    assert known.status_code == unknown.status_code == 202
    assert known.json() == unknown.json() == GENERIC
    rows = await open_requests()
    assert [r["note"] for r in rows] == [None]


async def test_a_repeated_ask_keeps_one_open_request_and_notifies_once(
    client, pushes
):
    root_id = await make_user("root", is_admin=True)
    alice_id = await make_user("alice")
    await ask(client, "alice", note="lost my phone")
    await ask(client, "alice", note="still locked out")
    await reset_requests.wait_pending()

    rows = await open_requests()
    assert len(rows) == 1
    assert rows[0]["user_id"] == alice_id and rows[0]["note"] == "lost my phone"
    assert len(pushes) == 1
    recipients, payload = pushes[0]
    assert recipients == [root_id]
    assert payload["body"] == "alice asked for a password reset"
    assert payload["kind"] == "message"


async def test_an_admin_asking_is_not_notified_about_themself(client, pushes):
    await make_user("root", is_admin=True)
    other_id = await make_user("root2", is_admin=True)
    await ask(client, "root")
    await reset_requests.wait_pending()
    assert pushes[0][0] == [other_id]


async def test_the_answer_does_not_wait_for_the_lookup(client, pushes, monkeypatch):
    await make_user("alice")
    looked_up = []
    gate = asyncio.Event()
    real_fetch_one = db.fetch_one

    async def held_fetch_one(sql, params=()):
        await gate.wait()
        looked_up.append(params)
        return await real_fetch_one(sql, params)

    monkeypatch.setattr(db, "fetch_one", held_fetch_one)
    r = await client.post("/auth/reset-requests", json={"username": "alice"})
    assert r.status_code == 202 and looked_up == []
    gate.set()
    await reset_requests.wait_pending()
    assert looked_up == [("alice",)]


async def test_a_note_over_two_hundred_characters_is_refused(client, pushes):
    await make_user("alice")
    assert (await ask(client, "alice", note="x" * 201)).status_code == 422
    assert await open_requests() == []


async def test_one_address_is_limited_to_three_asks_an_hour(app, house_origin, pushes):
    async with client_from(app, house_origin, "10.0.0.1") as c:
        for name in ("a", "b", "c"):
            assert (await ask(c, name)).status_code == 202
        r = await ask(c, "d")
    assert r.status_code == 429
    assert int(r.headers["retry-after"]) > 0
    async with client_from(app, house_origin, "10.0.0.2") as other:
        assert (await ask(other, "d")).status_code == 202


async def test_one_username_is_limited_across_addresses(app, house_origin, pushes):
    for n in range(3):
        async with client_from(app, house_origin, f"10.0.1.{n}") as c:
            assert (await ask(c, "alice")).status_code == 202
    async with client_from(app, house_origin, "10.0.1.9") as c:
        assert (await ask(c, "ALICE")).status_code == 429


async def test_only_admins_see_the_open_requests(client, pushes):
    await make_user("root", is_admin=True)
    await make_user("alice")
    await ask(client, "alice", note="new phone")

    assert (await client.get("/auth/reset-requests")).status_code == 401
    await login(client, "alice")
    assert (await client.get("/auth/reset-requests")).status_code == 403
    await login(client, "root")
    r = await client.get("/auth/reset-requests")
    assert r.status_code == 200
    [row] = r.json()
    assert row["username"] == "alice" and row["note"] == "new phone"
    assert row["requested_at"]


async def test_dismissing_closes_the_request_once(client, pushes):
    root_id = await make_user("root", is_admin=True)
    await make_user("alice")
    await ask(client, "alice")
    [row] = await open_requests()
    await login(client, "root")

    r = await client.post(f"/auth/reset-requests/{row['id']}/dismiss")
    assert r.status_code == 200
    closed = await db.fetch_one(
        "SELECT * FROM password_reset_requests WHERE id = ?", (row["id"],)
    )
    assert closed["status"] == "dismissed" and closed["resolved_by"] == root_id
    again = await client.post(f"/auth/reset-requests/{row['id']}/dismiss")
    assert again.status_code == 404
    assert (await client.get("/auth/reset-requests")).json() == []


async def test_an_admin_reset_resolves_the_open_request(client, pushes):
    root_id = await make_user("root", is_admin=True)
    alice_id = await make_user("alice")
    await ask(client, "alice")
    await login(client, "root")

    r = await client.post(
        f"/auth/users/{alice_id}/password", json={"new_password": NEW_PASSWORD}
    )
    assert r.status_code == 200
    row = await db.fetch_one("SELECT * FROM password_reset_requests")
    assert row["status"] == "resolved" and row["resolved_by"] == root_id
    assert (await client.get("/auth/reset-requests")).json() == []
