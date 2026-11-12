"""A summon that arrives while another is running: the SDK hands it over live
once the first returns, and the adapter judges it as it would any message."""

import asyncio
import json

from disjorn_sdk import MessageCreate

from adapter import SummonAdapter
from launcher import SessionResult
from residency_testlib import FakeClient, make_config, make_event, make_ready

ROOM = 7
CUSTODIAN = 4


class LateClient(FakeClient):
    """events() hands over what has arrived, one event at a time."""

    def __init__(self) -> None:
        super().__init__()
        self.arrived: asyncio.Queue = asyncio.Queue()

    def arrive(self, *events) -> None:
        for event in events:
            self.arrived.put_nowait(event)

    async def events(self):
        while (event := await self.arrived.get()) is not None:
            if isinstance(event, MessageCreate):
                self.last_seen_seq[event.channel_id] = max(
                    self.last_seen_seq.get(event.channel_id, 0), event.seq)
            yield event


class HeldLauncher:
    """Each session runs until the test lets it go."""

    def __init__(self) -> None:
        self.prompts: list[str] = []
        self.log: list[str] = []
        self._started = asyncio.Event()
        self._go = asyncio.Event()

    async def run(self, prompt: str, env=None) -> SessionResult:
        self.prompts.append(prompt)
        n = len(self.prompts)
        self.log.append(f"start {n}")
        self._started.set()
        await self._go.wait()
        self._go.clear()
        self.log.append(f"end {n}")
        return SessionResult(ok=True, reply=f"reply {n}", action_count=1,
                             duration_sec=0.1, exit_code=0)

    async def started(self) -> None:
        await asyncio.wait_for(self._started.wait(), 5)
        self._started.clear()

    def finish(self) -> None:
        self._go.set()


def _summon(seq: int, msg_id: int, who: str) -> MessageCreate:
    return make_event(channel_id=ROOM, seq=seq, msg_id=msg_id, author_name=who,
                      context={"awake_users": []})


def _cursor(tmp_path) -> dict:
    return json.loads((tmp_path / "cursor.json").read_text())


def test_a_summon_arriving_mid_summon_is_served_after_it(tmp_path):
    async def scenario():
        client, launcher = LateClient(), HeldLauncher()
        adapter = SummonAdapter(client, make_config(tmp_path), launcher=launcher)
        daemon = asyncio.create_task(adapter.run())

        client.arrive(make_ready(), _summon(50, 1001, "alice"))
        await launcher.started()
        client.arrive(
            make_event(channel_id=ROOM, seq=51, msg_id=1002, author_name="carol",
                       content="idle chatter"),
            _summon(52, 1003, "bob"),
            None,
        )
        await asyncio.sleep(0.05)
        assert launcher.log == ["start 1"]
        assert client.replies_to(ROOM) == []
        assert not (tmp_path / "cursor.json").exists()

        launcher.finish()
        await launcher.started()
        assert [r.reply_to for r in client.replies_to(ROOM)] == [1001]
        assert _cursor(tmp_path) == {str(ROOM): 51}

        launcher.finish()
        await asyncio.wait_for(daemon, 5)
        return client, launcher

    client, launcher = asyncio.run(scenario())

    assert launcher.log == ["start 1", "end 1", "start 2", "end 2"]
    replies = client.replies_to(ROOM)
    assert [(r.reply_to, r.content) for r in replies] == [
        (1001, "reply 1"), (1003, "reply 2")]
    assert "by bob" in launcher.prompts[1]
    audit = [s.content for s in client.replies_to(CUSTODIAN)]
    assert [line.split(" | ")[1].split()[0] for line in audit] == ["alice", "bob"]
    assert json.loads((tmp_path / "budget.json").read_text())["count"] == 2
    assert _cursor(tmp_path) == {str(ROOM): 52}


def test_a_summon_arriving_mid_summon_still_meets_the_daily_cap(tmp_path):
    async def scenario():
        client, launcher = LateClient(), HeldLauncher()
        config = make_config(tmp_path, budget={"daily_session_cap": 1})
        daemon = asyncio.create_task(
            SummonAdapter(client, config, launcher=launcher).run())

        client.arrive(_summon(50, 1001, "alice"))
        await launcher.started()
        client.arrive(_summon(51, 1002, "bob"), None)
        launcher.finish()
        await asyncio.wait_for(daemon, 5)
        return client, launcher

    client, launcher = asyncio.run(scenario())

    assert launcher.log == ["start 1", "end 1"]
    served, refused = client.replies_to(ROOM)
    assert (served.reply_to, served.content) == (1001, "reply 1")
    assert refused.reply_to == 1002
    assert refused.content.startswith("budget reached, ask a human.")
    assert "summoned by bob" in refused.content
