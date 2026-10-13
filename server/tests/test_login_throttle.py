"""Failed logins are throttled per client address, per address and username,
and per username with a much higher ceiling."""

import asyncio
import time

import httpx
import pytest

from app.routers import auth
from app.services import throttle

from .test_password import PASSWORD, login, make_user


@pytest.fixture
def clock(monkeypatch):
    now = [time.monotonic()]
    monkeypatch.setattr(throttle, "_clock", lambda: now[0])
    return now


def client_from(app, house_origin, ip):
    transport = httpx.ASGITransport(app=app, client=(ip, 4242))
    return httpx.AsyncClient(
        transport=transport, base_url=house_origin, headers={"Origin": house_origin}
    )


async def fail(client, username="alice", times=1):
    for _ in range(times):
        r = await login(client, username, "not the password")
        assert r.status_code == 401


async def test_five_failures_lock_the_username_from_that_address_even_for_the_right_password(
    client,
):
    await make_user("alice")
    await fail(client, times=5)
    r = await login(client, "alice", PASSWORD)
    assert r.status_code == 429
    assert int(r.headers["retry-after"]) > 0


async def test_failures_from_one_address_do_not_lock_the_owner_out_elsewhere(
    app, house_origin
):
    await make_user("alice")
    async with client_from(app, house_origin, "10.1.0.1") as attacker:
        await fail(attacker, times=5)
        assert (await login(attacker, "alice", PASSWORD)).status_code == 429
    async with client_from(app, house_origin, "10.1.0.2") as owner:
        assert (await login(owner, "alice", PASSWORD)).status_code == 200


async def test_fifty_failures_across_addresses_lock_the_username(app, house_origin):
    await make_user("alice")
    for n in range(10):
        async with client_from(app, house_origin, f"10.1.1.{n}") as c:
            await fail(c, times=5)
    async with client_from(app, house_origin, "10.1.1.99") as c:
        assert (await login(c, "Alice", PASSWORD)).status_code == 429


async def test_a_concurrent_burst_reaches_verify_no_more_than_the_limit(
    client, monkeypatch
):
    await make_user("alice")
    verified = []
    monkeypatch.setattr(auth, "verify_password", lambda h, p: verified.append(p) or False)
    results = await asyncio.gather(
        *(login(client, "alice", "not the password") for _ in range(20))
    )
    codes = sorted(r.status_code for r in results)
    assert len(verified) <= 5
    assert codes == [401] * len(verified) + [429] * (20 - len(verified))


async def test_successful_logins_never_count_against_the_address(client):
    await make_user("alice")
    for _ in range(12):
        assert (await login(client, "alice", PASSWORD)).status_code == 200


async def test_unknown_usernames_are_counted_like_real_ones(client):
    await fail(client, "ghost", times=5)
    assert (await login(client, "ghost", "anything")).status_code == 429


async def test_ten_failures_lock_the_address_but_not_another(app, house_origin):
    await make_user("alice")
    async with client_from(app, house_origin, "10.2.0.1") as c:
        for n in range(10):
            await fail(c, f"guess{n}")
        assert (await login(c, "alice", PASSWORD)).status_code == 429
    async with client_from(app, house_origin, "10.2.0.2") as other:
        assert (await login(other, "alice", PASSWORD)).status_code == 200


async def test_a_success_resets_the_username_count(client):
    await make_user("alice")
    await fail(client, times=4)
    assert (await login(client, "alice", PASSWORD)).status_code == 200
    await fail(client, times=4)
    assert (await login(client, "alice", PASSWORD)).status_code == 200


async def test_the_lock_lifts_after_fifteen_minutes(client, clock):
    await make_user("alice")
    await fail(client, times=5)
    assert (await login(client, "alice", PASSWORD)).status_code == 429
    clock[0] += 15 * 60
    assert (await login(client, "alice", PASSWORD)).status_code == 200


def test_ipv6_clients_are_keyed_by_their_slash_64():
    from types import SimpleNamespace
    from app.services.throttle import client_ip
    req = lambda host: SimpleNamespace(client=SimpleNamespace(host=host))
    assert client_ip(req("2600:1900:0:2d10::1b01")) == client_ip(req("2600:1900:0:2d10:ffff::9"))
    assert client_ip(req("2600:1900:0:2d10::1")) != client_ip(req("2600:1900:0:2d11::1"))
    assert client_ip(req("100.101.54.21")) == "100.101.54.21"
    assert client_ip(req("::ffff:10.0.0.1")) == "10.0.0.1"
