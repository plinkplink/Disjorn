"""The channel_deletions paper trail and its admin-only read, GET /channels/deletions."""

from app import db
from app.routers.channels import _record_deletion

from .test_channel_delete import cookie, login, make_channel, make_user, post_msg

RECORD_COLUMNS = {
    "id", "channel_id", "channel_type", "channel_name", "visibility",
    "created_by", "channel_created_at", "deleted_by_type", "deleted_by_id",
    "deleted_at", "message_count", "member_count",
}


async def records() -> list[dict]:
    return await db.fetch_all("SELECT * FROM channel_deletions ORDER BY id")


async def test_deleting_a_channel_writes_one_record_with_its_counts_and_actor(client):
    alice = await make_user("alice")
    ta = await login(client, "alice")
    bob = await make_user("bob")
    root = await make_user("root", is_admin=True)
    tadmin = await login(client, "root")

    cid = await make_channel(client, ta, "backroom", "private")
    r = await client.post(
        f"/channels/{cid}/invite", json={"user_id": bob},
        headers=cookie(ta),
    )
    assert r.status_code == 200, r.text
    for n in range(3):
        await post_msg(client, ta, cid, f"message {n}")
    created_at = (await db.fetch_one(
        "SELECT created_at FROM channels WHERE id = ?", (cid,)))["created_at"]

    r = await client.delete(f"/channels/{cid}", headers=cookie(tadmin))
    assert r.status_code == 200, r.text

    [rec] = await records()
    assert rec["channel_id"] == cid
    assert rec["channel_type"] == "text"
    assert rec["channel_name"] == "backroom"
    assert rec["visibility"] == "private"
    assert rec["created_by"] == alice
    assert rec["channel_created_at"] == created_at
    assert (rec["deleted_by_type"], rec["deleted_by_id"]) == ("user", root)
    assert rec["message_count"] == 3
    assert rec["member_count"] == 2
    assert rec["deleted_at"]


async def test_a_public_channel_record_counts_the_whole_house_as_members(client):
    await make_user("alice")
    ta = await login(client, "alice")
    await make_user("bob")
    await make_user("carol")

    cid = await make_channel(client, ta, "lobby")
    r = await client.delete(f"/channels/{cid}", headers=cookie(ta))
    assert r.status_code == 200, r.text

    [rec] = await records()
    assert rec["member_count"] == 3
    assert rec["message_count"] == 0


async def test_a_deletion_record_holds_no_message_contents(client):
    await make_user("alice")
    ta = await login(client, "alice")
    cid = await make_channel(client, ta, "secrets")
    await post_msg(client, ta, cid, "the vault code is zebra-4471")

    r = await client.delete(f"/channels/{cid}", headers=cookie(ta))
    assert r.status_code == 200, r.text

    [rec] = await records()
    assert set(rec) == RECORD_COLUMNS
    assert not any("zebra-4471" in str(v) for v in rec.values())


async def test_a_refused_delete_leaves_no_record(client):
    await make_user("alice")
    ta = await login(client, "alice")
    await make_user("bob")
    tb = await login(client, "bob")
    cid = await make_channel(client, ta, "alices-room")

    r = await client.delete(f"/channels/{cid}", headers=cookie(tb))
    assert r.status_code == 403, r.text
    assert await records() == []


async def test_a_dm_record_keeps_no_name(client):
    await make_user("alice")
    ta = await login(client, "alice")
    bob = await make_user("bob")
    dm = (await client.post("/dms", json={"user_id": bob}, headers=cookie(ta))).json()
    await db.execute("UPDATE channels SET name = 'alice-and-bob' WHERE id = ?", (dm["id"],))
    channel = await db.fetch_one("SELECT * FROM channels WHERE id = ?", (dm["id"],))

    async with db.transaction() as conn:
        await _record_deletion(conn, channel, "user", bob)

    [rec] = await records()
    assert rec["channel_type"] == "dm_1to1"
    assert rec["channel_name"] is None
    assert rec["member_count"] == 2


async def test_only_admins_may_read_deletion_records(client):
    await make_user("alice")
    ta = await login(client, "alice")

    r = await client.get("/channels/deletions", headers=cookie(ta))
    assert r.status_code == 403, r.text
    r = await client.get("/channels/deletions")
    assert r.status_code == 401, r.text


async def test_deletion_records_come_newest_first_and_page_by_before_id(client):
    await make_user("root", is_admin=True)
    tadmin = await login(client, "root")
    for name in ("first", "second", "third"):
        cid = await make_channel(client, tadmin, name)
        r = await client.delete(f"/channels/{cid}", headers=cookie(tadmin))
        assert r.status_code == 200, r.text

    r = await client.get("/channels/deletions?limit=2", headers=cookie(tadmin))
    assert r.status_code == 200, r.text
    page = r.json()
    assert [d["channel_name"] for d in page] == ["third", "second"]
    assert page[0]["deleted_by_name"] == "Root"

    r = await client.get(
        f"/channels/deletions?limit=2&before_id={page[-1]['id']}", headers=cookie(tadmin)
    )
    assert [d["channel_name"] for d in r.json()] == ["first"]
