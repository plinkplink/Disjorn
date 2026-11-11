"""DELETE /apps/{id} (SPECS/2026-11-11-apps-delete.md): the tombstone, the
files through the host helper, the build chats through the channel delete."""

import shutil
from contextlib import ExitStack

import pytest

from app import db, events
from app.routers import apps

from tests.test_apps import (  # noqa: F401 — fixtures are used by name
    BROKER_KEY,
    BUILDER_KEY,
    as_bot,
    build_fixture,
    call,
    cookie,
    login,
    make_session,
    make_user,
    open_bot,
    open_user,
    post_msg,
    post_stage,
    seat_toml,
    settings_env,
    start_session,
    sync_bot,
    sync_login,
    sync_user,
    wsc,
)
from tests.test_apps_keep_building import SEAT, seat_module, served
from tests.test_apps_serving import (  # noqa: F401 — fixtures are used by name
    HELPER,
    FakeHelper,
    gate_env,
    helper,
    make_live,
    open_app,
    share_with,
)

needs_the_launcher = pytest.mark.skipif(
    not (SEAT / "disjorn-apps-launch").is_file(),
    reason="no harness tree beside the server",
)


async def app_row(app_id: str) -> dict:
    return await db.fetch_one("SELECT * FROM apps WHERE id = ?", (app_id,))


async def listed(client) -> list[str]:
    return [a["id"] for a in (await client.get("/apps")).json()["apps"]]


async def deletion_records() -> list[dict]:
    return await db.fetch_all("SELECT * FROM channel_deletions ORDER BY id")


async def a_live_app_shared_with_bob(client, settings_env, seat_toml, gate_env):
    session = await build_fixture(client, settings_env, seat_toml)
    gate_env()
    await client.post(f"/apps/sessions/{session['id']}/end")
    await make_live(session["app"]["id"])
    bob = await make_user("bob", "Bob")
    await share_with(session["app"]["id"], bob)
    return session, bob


# ---- who ---------------------------------------------------------------------

async def test_only_the_owner_deletes_and_a_bot_never_does(
    client, app, settings_env, seat_toml, gate_env, helper
):
    session, _bob = await a_live_app_shared_with_bob(
        client, settings_env, seat_toml, gate_env)
    app_id = session["app"]["id"]
    await make_user("carol", "Carol")

    for username in ("bob", "carol"):
        await login(client, username)
        refused = await client.delete(f"/apps/{app_id}")
        assert refused.status_code == 403, refused.text
        assert refused.json()["detail"] == "Only the app's owner can change it"
    for key in (BUILDER_KEY, BROKER_KEY):
        r = await client.delete(f"/apps/{app_id}", headers=as_bot(client, key))
        assert r.status_code == 401, r.text

    await login(client, "alice")
    for unknown in ("aaaaaaaaaaaa", "not-an-app-id"):
        assert (await client.delete(f"/apps/{unknown}")).status_code == 404
    assert helper.calls == []
    assert (await app_row(app_id))["deleted_at"] is None

    done = await client.delete(f"/apps/{app_id}")
    assert done.status_code == 200, done.text
    assert done.json() == {"ok": True}
    assert helper.argv == [*HELPER, "delete", app_id]
    assert (await app_row(app_id))["deleted_at"] is not None
    assert app_id not in await listed(client)


async def test_the_platforms_build_record_is_not_deletable(
    client, app, settings_env, seat_toml, helper
):
    alice = await make_user("alice", "Alice")
    await db.execute("INSERT INTO bots (name, api_key_hash) VALUES ('BuildGable', 'x')")
    room = (await db.execute(
        "INSERT INTO channels (type, name) VALUES ('text', 'shed')")).lastrowid
    await apps.create_repo_session(alice, room)
    await login(client, "alice")

    r = await client.delete(f"/apps/{apps.PLATFORM_APP_ID}")

    assert r.status_code == 400, r.text
    assert helper.calls == []
    assert await db.fetch_one("SELECT 1 FROM channels WHERE id = ?", (room,))
    assert (await app_row(apps.PLATFORM_APP_ID))["deleted_at"] is None


# ---- what the house stops answering ---------------------------------------------

async def test_a_deleted_app_is_gone_from_every_list_card_and_grant(
    client, app, settings_env, seat_toml, gate_env, helper
):
    session, bob = await a_live_app_shared_with_bob(
        client, settings_env, seat_toml, gate_env)
    app_id, builder = session["app"]["id"], session["builder"]["bot_id"]
    carol = await make_user("carol", "Carol")
    await db.execute("UPDATE apps SET visibility = 'public' WHERE id = ?", (app_id,))

    await login(client, "bob")
    assert (await client.post(f"/apps/{app_id}/menu")).status_code == 200
    assert app_id in await listed(client)
    assert (await open_app(client, app_id)).status_code == 200
    await login(client, "carol")
    discover = (await client.get("/apps/discover")).json()["apps"]
    assert [a["id"] for a in discover] == [app_id]

    seen = []
    events.subscribe(lambda e: seen.append(e) if e["type"] == "app_delete" else None)
    await login(client, "alice")
    assert (await client.delete(f"/apps/{app_id}")).status_code == 200
    assert seen == [{"type": "app_delete", "app_id": app_id,
                     "user_ids": sorted([session["app"]["owner_user_id"], bob])}]
    assert carol not in seen[0]["user_ids"]

    for username in ("alice", "bob", "carol"):
        await login(client, username)
        assert app_id not in await listed(client)
        assert (await client.get("/apps/discover")).json()["apps"] == []
        for r in (
            await open_app(client, app_id),
            await open_app(client, app_id, "preview"),
            await client.get(f"/apps/{app_id}/card"),
            await client.post(f"/apps/{app_id}/menu"),
            await client.delete(f"/apps/{app_id}/menu"),
            await client.post(f"/apps/{app_id}/remix"),
            await client.patch(f"/apps/{app_id}", json={"name": "Back"}),
            await client.post(f"/apps/{app_id}/revert"),
            await client.post(f"/apps/{app_id}/share", json={"channel_id": 1}),
            await start_session(client, builder, app_id=app_id),
        ):
            assert r.status_code == 404, (username, r.request.url.path, r.text)
            assert r.json()["detail"] == "App not found"
    for table in ("app_shares", "user_apps"):
        assert await db.fetch_all(
            f"SELECT 1 FROM {table} WHERE app_id = ?", (app_id,)) == []
    builders = (await client.get("/apps/builders")).json()
    assert (builders[0]["builds_total"], builders[0]["builds_live"]) == (0, 0)
    assert [c for c in helper.calls if c[-2] != "delete"] == []


class SeatDelete(FakeHelper):
    """The house's spawn, answered by the launcher's own delete on scratch roots."""

    def __init__(self, www, scratch):
        super().__init__()
        self.roots = {name: scratch / name for name in ("apps", "git", "turns")}
        for path in self.roots.values():
            path.mkdir()
        self.www = www
        self.launcher = seat_module("disjorn-apps-launch")

    def plant(self, app_id: str) -> None:
        for root in ("live", "preview"):
            tree = self.www / app_id / root
            tree.mkdir(parents=True)
            (tree / "index.html").write_text(
                f"<html><body>{root} of {app_id}</body></html>")
        (self.roots["apps"] / app_id).mkdir()
        (self.roots["git"] / f"{app_id}.git").mkdir()

    def __call__(self, argv):
        assert argv[-2] == "delete", argv
        self.launcher.delete_app(
            self.roots["apps"], self.www, argv[-1], git_root=self.roots["git"],
            turns_root=self.roots["turns"], systemctl_bin=shutil.which("true"))
        return super().__call__(argv)


@needs_the_launcher
async def test_a_grant_minted_before_the_delete_opens_nothing_after_it(
    client, app, settings_env, seat_toml, gate_env, monkeypatch, tmp_path
):
    session, _bob = await a_live_app_shared_with_bob(
        client, settings_env, seat_toml, gate_env)
    app_id, www = session["app"]["id"], gate_env.www
    seat = SeatDelete(www, tmp_path)
    monkeypatch.setattr(apps, "_run_launch_helper", seat)
    seat.plant(app_id)
    seat.plant("zzz234567abc")

    await login(client, "bob")
    bobs = (await open_app(client, app_id)).json()["url"]
    await login(client, "alice")
    preview = (await open_app(client, app_id, "preview")).json()["url"]
    assert "live of" in (await served(www, bobs)).text
    assert "preview of" in (await served(www, preview)).text

    assert (await client.delete(f"/apps/{app_id}")).status_code == 200

    for url in (bobs, preview):
        gone = await served(www, url)
        assert gone.status_code == 404, gone.text
    assert not (www / app_id).exists()
    assert not (seat.roots["apps"] / app_id).exists()
    assert not (seat.roots["git"] / f"{app_id}.git").exists()
    assert (www / "zzz234567abc" / "live" / "index.html").is_file()


# ---- a build in progress -----------------------------------------------------

async def test_a_running_turn_refuses_the_delete_and_an_idle_session_is_closed(
    client, app, settings_env, seat_toml, gate_env, helper
):
    session = await build_fixture(client, settings_env, seat_toml)
    gate_env()
    app_id, sid = session["app"]["id"], session["id"]
    await post_stage(client, sid, "scoped", {"turn": 1})

    await login(client, "alice")
    busy = await client.delete(f"/apps/{app_id}")
    assert busy.status_code == 409, busy.text
    assert busy.json()["detail"] == apps.DELETE_WHILE_BUILDING
    assert helper.calls == [] and app_id in await listed(client)

    # Ending the session asks the turn to stop; until it has, it is running.
    assert (await client.post(f"/apps/sessions/{sid}/end")).status_code == 200
    assert (await client.delete(f"/apps/{app_id}")).status_code == 409
    landed = await post_stage(client, sid, "files_written",
                              {"turn": 1, "halted": "stopped"})
    assert landed.status_code == 200, landed.text

    await login(client, "alice")
    again = (await start_session(client, session["builder"]["bot_id"],
                                 app_id=app_id)).json()
    assert (await client.delete(f"/apps/{app_id}")).status_code == 200
    assert helper.calls == [[*HELPER, "delete", app_id]]
    assert await db.fetch_all(
        "SELECT 1 FROM app_sessions WHERE app_id = ?", (app_id,)) == []
    assert (await post_stage(client, again["id"], "scoped", {"turn": 1})
            ).status_code == 404


async def test_a_publish_in_flight_refuses_the_delete(
    client, app, settings_env, seat_toml, gate_env, helper
):
    session = await build_fixture(client, settings_env, seat_toml)
    gate_env()
    app_id = session["app"]["id"]
    await db.execute(
        "UPDATE app_sessions SET live_publishing_at = ? WHERE id = ?",
        (db.utc_now(), session["id"]))

    r = await client.delete(f"/apps/{app_id}")

    assert r.status_code == 409 and r.json()["detail"] == apps.DELETE_WHILE_PUBLISHING
    assert helper.calls == [] and (await app_row(app_id))["deleted_at"] is None


async def test_a_delete_landing_inside_a_publish_or_a_new_session_wins(
    client, app, settings_env, seat_toml, gate_env, helper, monkeypatch
):
    session = await build_fixture(client, settings_env, seat_toml)
    gate_env()
    app_id, sid = session["app"]["id"], session["id"]
    for stage in ("scoped", "files_written", "deployed"):
        await post_stage(client, sid, stage, {"turn": 1})
    await login(client, "alice")
    helper.returncode = 1
    real = apps._require_app

    async def then_deleted(asked):
        row = await real(asked)
        assert (await client.delete(f"/apps/{app_id}")).status_code == 502
        return row

    monkeypatch.setattr(apps, "_require_app", then_deleted)
    late = await client.post(f"/apps/sessions/{sid}/live")
    assert late.status_code == 410, late.text
    assert [c[-2] for c in helper.calls] == ["delete"]

    await db.execute(
        "UPDATE apps SET deleted_at = NULL, deleted_builds = 0 WHERE id = ?",
        (app_id,))
    opened = await start_session(client, session["builder"]["bot_id"], app_id=app_id)
    assert opened.status_code == 404, opened.text
    assert len(await db.fetch_all(
        "SELECT 1 FROM app_sessions WHERE app_id = ?", (app_id,))) == 1


# ---- failure, and finishing -------------------------------------------------

async def test_a_failed_helper_leaves_a_deleted_app_and_a_second_call_finishes(
    client, app, settings_env, seat_toml, gate_env, helper
):
    session, _bob = await a_live_app_shared_with_bob(
        client, settings_env, seat_toml, gate_env)
    app_id, sid, room = session["app"]["id"], session["id"], session["channel_id"]
    await make_user("root", "Root", admin=True)
    helper.returncode = 1
    helper.stderr = "disjorn-apps-launch: FAILED: rmtree"

    failed = await client.delete(f"/apps/{app_id}")
    assert failed.status_code == 502, failed.text
    assert (await app_row(app_id))["deleted_at"] is not None
    assert app_id not in await listed(client)
    assert (await open_app(client, app_id)).status_code == 404
    assert await deletion_records() == []
    assert await db.fetch_one("SELECT 1 FROM channels WHERE id = ?", (room,))

    # The build chat is still there, and nothing can be built or read in it.
    assert (await client.get(f"/apps/sessions/{sid}")).status_code == 404
    assert (await client.post(f"/apps/sessions/{sid}/heartbeat")).status_code == 410
    assert (await post_stage(client, sid, "scoped", {"turn": 1})).status_code == 410
    view = await client.get(f"/apps/sessions/{sid}/harness-view",
                            headers=as_bot(client, BROKER_KEY))
    assert view.json()["open"] is False
    await login(client, "root")
    by_hand = await client.post(f"/apps/sessions/{sid}/stage", json={"stage": "live"})
    assert by_hand.status_code == 410, by_hand.text
    assert (await client.delete(f"/apps/{app_id}")).status_code == 404
    await login(client, "bob")
    assert (await client.delete(f"/apps/{app_id}")).status_code == 404

    await login(client, "alice")
    helper.returncode, helper.stderr = 64, (
        f"disjorn-apps-launch: REFUSED: {app_id} has a turn running")
    refused = await client.delete(f"/apps/{app_id}")
    assert refused.status_code == 409 and "turn running" in refused.json()["detail"]

    helper.returncode = 0
    for _ in range(2):
        done = await client.delete(f"/apps/{app_id}")
        assert done.status_code == 200, done.text
    assert helper.calls == [[*HELPER, "delete", app_id]] * 4
    assert await db.fetch_one("SELECT 1 FROM channels WHERE id = ?", (room,)) is None
    assert len(await deletion_records()) == 1


async def test_each_build_chat_goes_through_the_channel_delete_and_is_recorded(
    client, app, settings_env, seat_toml, gate_env, helper
):
    first = await build_fixture(client, settings_env, seat_toml)
    gate_env()
    app_id, alice = first["app"]["id"], first["app"]["owner_user_id"]
    await client.patch(f"/apps/{app_id}", json={"name": "Tips"})
    await client.post(f"/apps/sessions/{first['id']}/end")
    second = (await start_session(client, first["builder"]["bot_id"],
                                  app_id=app_id)).json()
    r = await client.post(f"/channels/{second['channel_id']}/messages",
                          json={"content": "make it blue"})
    assert r.status_code == 200, r.text
    other = (await start_session(client, first["builder"]["bot_id"])).json()
    rooms = [first["channel_id"], second["channel_id"]]
    seen = []
    events.subscribe(lambda e: seen.append(e) if e["type"] == "channel_delete" else None)

    assert (await client.delete(f"/apps/{app_id}")).status_code == 200

    records = await deletion_records()
    assert [rec["channel_id"] for rec in records] == rooms
    for rec, name, messages in zip(records, ("Untitled app", "Tips"), (1, 2)):
        assert rec["channel_type"] == "app_build" and rec["visibility"] == "private"
        assert (rec["deleted_by_type"], rec["deleted_by_id"]) == ("user", alice)
        assert (rec["channel_name"], rec["created_by"]) == (name, alice)
        assert (rec["message_count"], rec["member_count"]) == (messages, 2)
    assert [e["channel_id"] for e in seen] == rooms
    assert all(e["by_user_id"] == alice for e in seen)
    placeholders = ",".join("?" * len(rooms))
    for table in ("channels WHERE id", "messages WHERE channel_id",
                  "channel_members WHERE channel_id"):
        assert await db.fetch_all(
            f"SELECT 1 FROM {table} IN ({placeholders})", rooms) == []
    assert await db.fetch_one(
        "SELECT 1 FROM channels WHERE id = ?", (other["channel_id"],))
    assert (await client.get(f"/apps/sessions/{other['id']}")).status_code == 200


# ---- remixes -----------------------------------------------------------------

async def test_a_remix_outlives_the_app_it_was_made_from(
    client, app, settings_env, seat_toml, gate_env, helper
):
    session, _bob = await a_live_app_shared_with_bob(
        client, settings_env, seat_toml, gate_env)
    parent_id, builder = session["app"]["id"], session["builder"]["bot_id"]
    await login(client, "bob")
    child = (await client.post(f"/apps/{parent_id}/remix")).json()
    child_id = child["app"]["id"]
    assert child["app"]["parent_app_id"] == parent_id

    await login(client, "alice")
    assert (await client.delete(f"/apps/{parent_id}")).status_code == 200
    assert [c[-2:] for c in helper.calls if c[-2] == "delete"] == [
        ["delete", parent_id]]

    await login(client, "bob")
    assert await listed(client) == [child_id]
    row = (await client.get("/apps")).json()["apps"][0]
    assert row["parent_app_id"] == parent_id
    assert (await client.get(f"/apps/sessions/{child['id']}")).status_code == 200
    card = await client.get(f"/apps/{child_id}/card")
    assert card.status_code == 200 and card.json()["parent_app_id"] == parent_id
    assert (await client.get(f"/apps/{parent_id}/card")).status_code == 404

    for stage in ("scoped", "files_written", "deployed"):
        await post_stage(client, child["id"], stage, {"turn": 1})
    await login(client, "bob")
    assert (await client.post(f"/apps/sessions/{child['id']}/live")).status_code == 200
    await client.post(f"/apps/sessions/{child['id']}/end")
    assert (await open_app(client, child_id)).status_code == 200
    kept = await start_session(client, builder, app_id=child_id)
    assert kept.status_code == 200, kept.text
    await client.post(f"/apps/sessions/{kept.json()['id']}/end")
    grandchild = await client.post(f"/apps/{child_id}/remix")
    assert grandchild.status_code == 200, grandchild.text
    assert (await client.delete(f"/apps/{child_id}")).status_code == 200
    assert (await app_row(grandchild.json()["app"]["id"]))["parent_app_id"] == child_id


# ---- the meter ---------------------------------------------------------------

async def test_deleting_gives_back_no_build_that_ending_would_not(
    client, app, settings_env, seat_toml, gate_env, helper
):
    built = await build_fixture(client, settings_env, seat_toml)
    gate_env()
    builder = built["builder"]["bot_id"]
    await post_stage(client, built["id"], "scoped", {"turn": 1})
    await post_stage(client, built["id"], "files_written", {"turn": 1, "tokens": 5})
    await login(client, "alice")
    idle = (await start_session(client, builder)).json()

    async def used() -> int:
        return (await client.get("/apps/quota")).json()["used"]

    assert await used() == 2
    assert (await client.delete(f"/apps/{idle['app']['id']}")).status_code == 200
    assert await used() == 1
    helper.returncode = 1
    assert (await client.delete(f"/apps/{built['app']['id']}")).status_code == 502
    assert await used() == 1
    helper.returncode = 0
    assert (await client.delete(f"/apps/{built['app']['id']}")).status_code == 200
    assert await used() == 1
    assert (await app_row(built["app"]["id"]))["deleted_builds"] == 1
    assert (await app_row(idle["app"]["id"]))["deleted_builds"] == 0

    await db.execute(
        "UPDATE apps SET deleted_at = '2000-01-01T00:00:00.000Z' WHERE id = ?",
        (built["app"]["id"],))
    assert await used() == 0


# ---- who hears about it --------------------------------------------------------

def test_the_owner_and_listed_viewers_get_the_frame_and_a_stranger_gets_nothing(
    wsc, settings_env, seat_toml, monkeypatch
):
    alice = sync_user(wsc, "alice", "Alice")
    bob = sync_user(wsc, "bob", "Bob")
    carol = sync_user(wsc, "carol", "Carol")
    builder = sync_bot(wsc, "gable", BUILDER_KEY)
    settings_env(APPS_BUILDERS=[{"bot_id": builder, "model_source": str(seat_toml)}],
                 APPS_LAUNCH_HELPER=HELPER)
    monkeypatch.setattr(apps, "_run_launch_helper", FakeHelper())
    token = sync_login(wsc, "alice")
    bob_token, carol_token = sync_login(wsc, "bob"), sync_login(wsc, "carol")
    session = make_session(wsc, token, builder)
    app_id, room = session["app"]["id"], session["channel_id"]
    call(wsc, db.execute,
         "INSERT INTO user_apps (user_id, app_id) VALUES (?, ?)", (bob, app_id))
    main = call(wsc, db.fetch_one,
                "SELECT id FROM channels WHERE type = 'main_feed'")["id"]

    with ExitStack() as stack:
        wa = open_user(stack, wsc, token, alice)
        wb = open_user(stack, wsc, bob_token, bob, peers=[wa])
        wc = open_user(stack, wsc, carol_token, carol, peers=[wa, wb])
        wbot = open_bot(stack, wsc, builder)

        r = wsc.delete(f"/apps/{app_id}", headers=cookie(token))
        assert r.status_code == 200, r.text

        gone = {"type": "app_delete", "app_id": app_id}
        assert wa.receive_json() == gone
        assert wb.receive_json() == gone
        for member in (wa, wbot):
            frame = member.receive_json()
            assert (frame["type"], frame["channel_id"]) == ("channel_delete", room)
            assert frame["by_user_id"] == alice

        post_msg(wsc, cookie(token), main, "sentinel")
        for socket in (wb, wc):
            assert socket.receive_json()["type"] == "message_create"


# ---- the migration ----------------------------------------------------------

async def test_migration_025_leaves_every_existing_app_undeleted(tmp_path, monkeypatch):
    from app.config import reset_settings_cache

    monkeypatch.setenv("DB_PATH", str(tmp_path / "old.db"))
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    reset_settings_cache()
    await db.close()
    conn = await db.connect()
    try:
        await conn.execute("""CREATE TABLE schema_migrations (
            filename TEXT PRIMARY KEY, applied_at TEXT NOT NULL)""")
        for path in sorted(db.MIGRATIONS_DIR.glob("*.sql")):
            if path.name == "025_apps_delete.sql":
                continue
            await conn.executescript(path.read_text(encoding="utf-8"))
            await conn.execute(
                "INSERT INTO schema_migrations (filename, applied_at) VALUES (?, ?)",
                (path.name, db.utc_now()))
        await conn.commit()
        uid = await make_user("alice", "Alice")
        await db.execute(
            "INSERT INTO apps (id, owner_user_id, name, builder_bot_id) "
            "VALUES ('abc234567xyz', ?, 'Tips', 1)", (uid,))

        assert await db.run_migrations() == ["025_apps_delete.sql"]

        row = await app_row("abc234567xyz")
        assert (row["deleted_at"], row["deleted_builds"]) == (None, 0)
        assert (await apps._require_app("abc234567xyz"))["name"] == "Tips"
    finally:
        await db.close()
        reset_settings_cache()

