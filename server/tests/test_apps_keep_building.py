"""A build session on the owner's LIVE app, and what the public root serves meanwhile.

The trees are moved by the seat's own code (the harvest's preview publisher, the
launcher's rotate) and read back through the real gate with house-minted grants.
"""

import importlib.util
import shutil
from importlib.machinery import SourceFileLoader
from pathlib import Path

import httpx
import pytest

from app import db, gate

from tests.test_apps import (  # noqa: F401 — fixtures are used by name
    login,
    make_user,
    post_stage,
    seat_toml,
    settings_env,
    start_session,
)
from tests.test_apps_serving import (  # noqa: F401 — fixtures are used by name
    GATE_SECRET,
    HELPER,
    ORIGIN,
    FakeHelper,
    a_session,
    gate_env,
    helper,
    make_live,
    open_app,
    share_with,
)

SEAT = Path(__file__).resolve().parents[2] / "harness" / "cc" / "apps"
RSYNC = shutil.which("rsync")
needs_the_seat = pytest.mark.skipif(
    RSYNC is None or not (SEAT / "apps_harvest.py").is_file(),
    reason="no rsync, or no harness tree beside the server",
)


def seat_module(filename: str):
    loader = SourceFileLoader(filename.replace("-", "_").removesuffix(".py"),
                              str(SEAT / filename))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


class SeatHelper(FakeHelper):
    """The house's spawn, answered by the launcher's own rotate on the scratch root."""

    def __init__(self, www):
        super().__init__()
        self.www = www
        self.launcher = seat_module("disjorn-apps-launch")

    def __call__(self, argv):
        assert argv[-2] == "publish", argv
        self.launcher.publish_app(self.www, argv[-1], rsync_bin=RSYNC)
        return super().__call__(argv)


async def a_turn(client, www, work, session, turn: int, page: str) -> None:
    """One build turn: the work tree changes, the harvest publishes the preview,
    the broker posts the turn."""
    (work / "index.html").write_text(page)
    seat_module("apps_harvest.py").copy_preview(
        work, www / session["app"]["id"] / "preview", turn, rsync_bin=RSYNC
    )
    await post_stage(client, session["id"], "scoped", {"turn": turn})
    await post_stage(client, session["id"], "files_written",
                     {"turn": turn, "files": ["index.html"], "tokens": 10})
    await post_stage(client, session["id"], "deployed", {"turn": turn})


async def served(www, url: str) -> httpx.Response:
    """What the gate answers a viewer who follows a URL the house minted."""
    settings = gate.GateSettings(
        secret=GATE_SECRET.encode(), www_root=www,
        house_origin="https://test", origin_base=ORIGIN,
    )
    transport = httpx.ASGITransport(app=gate.build_app(settings))
    async with httpx.AsyncClient(transport=transport, follow_redirects=True) as viewer:
        return await viewer.get(url)


async def open_as(client, username: str, app_id: str, root: str = "live"):
    await login(client, username)
    return await open_app(client, app_id, root)


def tree(root: Path) -> dict[str, bytes]:
    return {str(p.relative_to(root)): p.read_bytes()
            for p in sorted(root.rglob("*")) if p.is_file()}


async def test_a_session_opens_on_the_owners_live_app_and_on_nobody_elses(
    client, app, gate_env, settings_env, seat_toml, helper
):
    first = await a_session(client, settings_env, seat_toml)
    gate_env()
    app_id, builder = first["app"]["id"], first["builder"]["bot_id"]
    await make_live(app_id)
    await client.post(f"/apps/sessions/{first['id']}/end")
    bob = await make_user("bob", "Bob")
    await share_with(app_id, bob)

    await login(client, "bob")
    refused = await start_session(client, builder, app_id=app_id)
    assert refused.status_code == 403
    assert refused.json()["detail"] == "Only the app's owner can change it"
    sessions = await db.fetch_all("SELECT id FROM app_sessions WHERE app_id = ?", (app_id,))
    assert [row["id"] for row in sessions] == [first["id"]]

    await login(client, "alice")
    again = await start_session(client, builder, app_id=app_id)
    assert again.status_code == 200, again.text
    body = again.json()
    assert body["id"] != first["id"] and body["app"]["id"] == app_id
    assert body["app"]["status"] == "live"

    # Nothing was built in this session, so Live has nothing to rotate in.
    early = await client.post(f"/apps/sessions/{body['id']}/live")
    assert early.status_code == 409
    assert helper.calls == []
    assert (await open_as(client, "bob", app_id)).status_code == 200


@needs_the_seat
async def test_a_turn_on_a_live_app_moves_the_preview_and_not_the_public_root(
    client, app, gate_env, settings_env, seat_toml, monkeypatch, tmp_path
):
    first = await a_session(client, settings_env, seat_toml)
    gate_env()
    www = gate_env.www
    publisher = SeatHelper(www)
    monkeypatch.setattr("app.routers.apps._run_launch_helper", publisher)
    app_id, builder = first["app"]["id"], first["builder"]["bot_id"]
    bob = await make_user("bob", "Bob")
    await share_with(app_id, bob)
    work = tmp_path / "work"
    work.mkdir()

    await a_turn(client, www, work, first, 1, "<html><body>version one</body></html>")
    await login(client, "alice")
    assert (await client.post(f"/apps/sessions/{first['id']}/live")).status_code == 200
    await client.post(f"/apps/sessions/{first['id']}/end")
    public = (await open_as(client, "bob", app_id)).json()["url"]
    assert "version one" in (await served(www, public)).text
    live_before = tree(www / app_id / "live")

    await login(client, "alice")
    second = (await start_session(client, builder, app_id=app_id)).json()
    await a_turn(client, www, work, second, 1, "<html><body>version two</body></html>")

    assert publisher.calls == [[*HELPER, "publish", app_id]]
    assert tree(www / app_id / "live") == live_before
    assert not (www / app_id / "live.prev").exists()
    row = await db.fetch_one("SELECT status FROM apps WHERE id = ?", (app_id,))
    assert row["status"] == "live"

    # The viewer: the same page on a fresh grant, and no door to the new one.
    bobs = await open_as(client, "bob", app_id)
    assert "version one" in (await served(www, bobs.json()["url"])).text
    assert (await open_as(client, "bob", app_id, "preview")).status_code == 403
    sideways = bobs.json()["url"].replace(f"/{app_id}/", f"/{app_id}/preview/")
    assert (await served(www, sideways)).status_code == 403

    # The owner: the new page in the preview, the old one at the public root.
    preview = await open_as(client, "alice", app_id, "preview")
    assert "version two" in (await served(www, preview.json()["url"])).text
    mine = await open_as(client, "alice", app_id)
    assert "version one" in (await served(www, mine.json()["url"])).text

    assert (await client.post(f"/apps/sessions/{second['id']}/live")).status_code == 200
    assert publisher.calls == [[*HELPER, "publish", app_id]] * 2
    after = await open_as(client, "bob", app_id)
    assert "version two" in (await served(www, after.json()["url"])).text
    assert tree(www / app_id / "live.prev") == live_before
