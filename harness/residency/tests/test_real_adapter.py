"""The real SDK client under the real adapter against a scratch server: a
summon that arrives during a summon is served on the same connection, and
reads the first one's reply."""

import asyncio
import importlib.util
import logging
import sys
from pathlib import Path

import pytest

SDK_TESTS = Path(__file__).resolve().parents[3] / "sdk" / "tests"
if not (SDK_TESTS.parents[1] / "server" / "cli.py").is_file():
    pytest.skip("needs the checkout's server and SDK tests beside it",
                allow_module_level=True)


def _load(name: str, path: Path):
    """The SDK suite's modules by path: its conftest shares a name with ours."""
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, path)
    module = sys.modules[name] = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


server = _load("sdk_live_conftest", SDK_TESTS / "conftest.py").server
live = _load("test_sdk_live", SDK_TESTS / "test_sdk_live.py")
alice, bot, main_id, fast_keepalive = (
    live.alice, live.bot, live.main_id, live.fast_keepalive)

from adapter import SummonAdapter  # noqa: E402
from residency_testlib import make_config  # noqa: E402
from test_late_summon import ENDS_AT_TRIGGER, _chat, _header, _names, _waited  # noqa: E402
from test_summon_during_summon import HeldLauncher  # noqa: E402

pytestmark = pytest.mark.integration


async def _bot_rows(user, channel_id: int) -> list[dict]:
    resp = await user.get(f"/channels/{channel_id}/messages", params={"limit": 50})
    return [m for m in reversed(resp.json()) if m["author_type"] == "bot"]


async def test_a_summon_during_a_summon_is_served_and_reads_the_first_reply(
    server, alice, main_id, bot, fast_keepalive, tmp_path, caplog
):
    """Over 16 frames and a turn longer than the keepalive sit between the
    two summons, which is what cost the second one its delivery."""
    caplog.set_level(logging.INFO)
    name = server.bot_name
    launcher = HeldLauncher()
    config = make_config(tmp_path, summon={"bot_name": name})
    daemon = asyncio.create_task(
        SummonAdapter(bot, config, launcher=launcher).run())
    ws = await live._user_ws(server, alice)
    try:
        one = await live._post(alice, main_id, f"@{name} one")
        await launcher.started()
        await live._flip_presence(ws, 20)
        two = await live._post(alice, main_id, f"@{name} two")
        await live._post(alice, main_id, "the door code is 4411",
                         privacy_flags={"secret": True})
        await live._post(alice, main_id, "still here")
        await live._flip_presence(ws, 4)
        await asyncio.sleep(6 * live.FAST_PING)
        launcher.finish()
        await launcher.started()
        launcher.finish()

        for _ in range(500):
            if len(await _bot_rows(alice, main_id)) == 2:
                break
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.01)
        three = await live._post(alice, main_id, f"@{name} three")
        await launcher.started()
        launcher.finish()
        for _ in range(500):
            if len(await _bot_rows(alice, main_id)) == 3:
                break
            await asyncio.sleep(0.01)
    finally:
        await ws.close()
        daemon.cancel()
        await asyncio.gather(daemon, return_exceptions=True)

    assert launcher.log == ["start 1", "end 1", "start 2", "end 2",
                            "start 3", "end 3"]
    assert [r.getMessage() for r in caplog.records
            if r.name == "disjorn_sdk" and r.levelno >= logging.INFO] == [
        "connected as bot %d" % bot.bot_id]
    first, second, third = await _bot_rows(alice, main_id)
    assert [(m["reply_to_id"], m["content"]) for m in (first, second, third)] == [
        (one["id"], "reply 1"), (two["id"], "reply 2"), (three["id"], "reply 3")]

    waited, ordinary = launcher.prompts[1:]
    assert _chat(waited)[-3:] == [
        f"alice: [#{two['seq']}] @{name} two",
        f"alice: [#{two['seq'] + 2}] still here",
        f"{name}: [#{first['seq']}] reply 1",
    ]
    assert "door code" not in waited
    assert _waited("2") in _header(waited)
    assert _header(waited)[-1] == _names(two["seq"])

    assert _chat(ordinary)[-2:] == [
        f"{name}: [#{second['seq']}] reply 2",
        f"alice: [#{three['seq']}] @{name} three",
    ]
    assert _header(ordinary)[-1] == ENDS_AT_TRIGGER
    assert not any("arrived while" in line for line in _header(ordinary))
