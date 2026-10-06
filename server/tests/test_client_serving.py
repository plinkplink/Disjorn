"""Static client serving: unhashed shell files revalidate, hashed assets do not."""

import httpx
import pytest

from app import db, events
from app import main as main_module


@pytest.fixture
async def dist_client(tmp_db_path, tmp_path, monkeypatch):
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<!doctype html><title>shell</title>")
    (dist / "sw.js").write_text("self.addEventListener('fetch', () => {});")
    (dist / "assets" / "index-abc123.js").write_text("export {};")
    monkeypatch.setattr(main_module, "CLIENT_DIST", dist)

    events.clear_subscribers()
    await db.close()
    application = main_module.create_app()
    async with application.router.lifespan_context(application):
        transport = httpx.ASGITransport(app=application)
        async with httpx.AsyncClient(transport=transport, base_url="https://test") as c:
            yield c
    await db.close()
    events.clear_subscribers()


async def test_the_app_shell_is_revalidated_on_every_load(dist_client):
    for path in ["/", "/index.html", "/some/deep/link", "/sw.js"]:
        resp = await dist_client.get(path)
        assert resp.status_code == 200, path
        assert resp.headers["cache-control"] == "no-cache", path


async def test_hashed_assets_are_not_forced_to_revalidate(dist_client):
    resp = await dist_client.get("/assets/index-abc123.js")
    assert resp.status_code == 200
    assert "no-cache" not in resp.headers.get("cache-control", "")


async def test_an_unknown_api_path_still_answers_json_not_the_shell(dist_client):
    resp = await dist_client.get("/channels/999999/nope")
    assert resp.headers["content-type"].startswith("application/json")
