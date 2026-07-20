"""Fakes + builders for the WP-H9 residency suite.

No network, no podman, no prod. A fake SDK client records what the adapter
sends/typed/fetched; a fake launcher stands in for the container when a test
doesn't need the real subprocess; builders make events and configs terse.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Optional

import pytest

from disjorn_sdk import MessageCreate, Ready

from config import AdapterConfig
from launcher import SessionResult

TESTS_DIR = Path(__file__).resolve().parent
STUB_LAUNCH = TESTS_DIR / "stub_launch.py"

__all__ = [
    "FakeClient",
    "FakeLauncher",
    "make_message",
    "make_event",
    "make_ready",
    "make_config",
    "STUB_LAUNCH",
    "stub_config",
]


# --------------------------------------------------------------------------- client


class FakeClient:
    """Duck-typed stand-in for DisjornClient.

    ``events()`` replays a scripted list then stops (a real client runs
    forever; finite is what makes ``adapter.run()`` return in a test).
    """

    def __init__(self, events: Optional[list] = None) -> None:
        self._events = list(events or [])
        self.bot_id = 2
        self.last_seen_seq: dict[int, int] = {}
        self.sent: list[SimpleNamespace] = []
        self.typing_calls: list[int] = []
        self.get_messages_calls: list[dict] = []
        self.seeded: list[tuple[int, int]] = []
        self._backfill: dict[int, list[dict]] = {}
        self.typing_fails = False
        self.closed = False

    # backfill data the adapter will fetch via get_messages(before_seq=...)
    def set_backfill(self, channel_id: int, messages: list[dict]) -> None:
        self._backfill[channel_id] = messages

    async def events(self):
        for event in self._events:
            # Mirror the real client: persisted events advance the cursor.
            if isinstance(event, MessageCreate) and event.seq is not None:
                self.last_seen_seq[event.channel_id] = max(
                    self.last_seen_seq.get(event.channel_id, 0), event.seq
                )
            yield event

    async def send(self, channel_id: int, content: str, *, reply_to=None, **kw):
        self.sent.append(
            SimpleNamespace(channel_id=channel_id, content=content,
                            reply_to=reply_to, kwargs=kw)
        )
        return {"id": 9000 + len(self.sent), "seq": 100 + len(self.sent)}

    async def get_messages(self, channel_id: int, *, from_seq=None,
                           before_seq=None, limit=None):
        self.get_messages_calls.append(
            {"channel_id": channel_id, "from_seq": from_seq,
             "before_seq": before_seq, "limit": limit}
        )
        # Mimic before_seq mode: newest-first.
        msgs = list(self._backfill.get(channel_id, []))
        msgs = sorted(msgs, key=lambda m: m.get("seq", 0), reverse=True)
        if limit is not None:
            msgs = msgs[:limit]
        return msgs

    async def typing(self, channel_id: int) -> None:
        if self.typing_fails:
            raise RuntimeError("no live WS")
        self.typing_calls.append(channel_id)

    def seed_seq(self, channel_id: int, seq: int) -> None:
        self.seeded.append((channel_id, seq))
        self.last_seen_seq[channel_id] = max(
            self.last_seen_seq.get(channel_id, 0), seq
        )

    async def aclose(self) -> None:
        self.closed = True

    # convenience for tests: replies to a channel other than custodian
    def replies_to(self, channel_id: int) -> list[SimpleNamespace]:
        return [s for s in self.sent if s.channel_id == channel_id]


# --------------------------------------------------------------------------- launcher


@dataclass
class FakeLauncher:
    result: SessionResult = field(
        default_factory=lambda: SessionResult(
            ok=True, reply="Hello from Gable.", action_count=3, duration_sec=1.2,
            exit_code=0,
        )
    )
    delay: float = 0.0
    prompts: list[str] = field(default_factory=list)

    async def run(self, prompt: str) -> SessionResult:
        import asyncio

        self.prompts.append(prompt)
        if self.delay:
            await asyncio.sleep(self.delay)
        return self.result


# --------------------------------------------------------------------------- builders


def make_message(
    *,
    msg_id: int = 1,
    channel_id: int = 7,
    seq: int = 50,
    author_type: str = "user",
    author_id: int = 11,
    author_name: str = "alice",
    content: str = "hey @gable can you help",
) -> dict:
    return {
        "id": msg_id,
        "channel_id": channel_id,
        "seq": seq,
        "author_type": author_type,
        "author_id": author_id,
        "author": {"id": author_id, "name": author_name},
        "content": content,
    }


def make_event(
    *,
    context: Optional[dict] = None,
    backfilled: bool = False,
    **msg_kwargs,
) -> MessageCreate:
    msg = make_message(**msg_kwargs)
    return MessageCreate(
        channel_id=msg["channel_id"], seq=msg["seq"], message=msg,
        context=context, backfilled=backfilled,
    )


def make_ready(*, reconnected: bool = False) -> Ready:
    return Ready(bot_id=2, reconnected=reconnected)


def make_config(tmp_path: Path, *, use_stub: bool = True, **overrides) -> AdapterConfig:
    """A ready-to-run AdapterConfig with all state under tmp_path.

    ``overrides`` is a nested dict merged over the base (e.g.
    ``budget={"daily_session_cap": 1}``).
    """
    base: dict[str, Any] = {
        "server": {"url": "http://localhost:0", "api_key": "test-key"},
        "summon": {
            "bot_name": "gable",
            "custodian_channel_id": 4,
            "trigger_on_context": True,
            "trigger_channels": [],
            "extra_patterns": [],
            "typing_interval_sec": 2.5,
        },
        "backfill": {"count": 30},
        "container": {
            "command": [sys.executable, str(STUB_LAUNCH)] if use_stub else [],
            "resident": "gable",
            "session_argv": [],
            "timeout_sec": 30,
        },
        "budget": {
            "daily_session_cap": 12,
            "state_path": str(tmp_path / "budget.json"),
        },
        "cursor": {"state_path": str(tmp_path / "cursor.json")},
        "text": {
            "refusal_line": "budget reached, ask a human.",
            "error_line": "something broke on my end.",
        },
    }
    for section, values in overrides.items():
        base.setdefault(section, {}).update(values)
    return AdapterConfig.from_dict(base)


@pytest.fixture()
def stub_config(tmp_path):
    """Config wired to the stub launch script, state under tmp_path."""
    return make_config(tmp_path)
