"""The buffer between the SDK's socket reader and its consumer; no server."""

from __future__ import annotations

import asyncio

from disjorn_sdk import MessageCreate, Presence, TypingStart
from disjorn_sdk.client import _End, _Inbox


def _msg(seq: int) -> MessageCreate:
    return MessageCreate(channel_id=1, seq=seq, message={"id": seq})


def _typing(channel_id: int = 1, author_id: int = 5) -> TypingStart:
    return TypingStart(channel_id=channel_id, author_type="user", author_id=author_id)


async def _take(inbox: _Inbox, count: int) -> list:
    return [await asyncio.wait_for(inbox.get(), 1) for _ in range(count)]


async def _is_empty(inbox: _Inbox) -> bool:
    inbox.end(None)
    return isinstance(await asyncio.wait_for(inbox.get(), 1), _End)


async def test_waiting_typing_and_presence_keep_the_newest_per_sender():
    inbox = _Inbox(100)
    await inbox.put(_typing())
    await inbox.put(Presence(user_id=5, status="idle"))
    await inbox.put(_msg(1))
    for _ in range(50):
        await inbox.put(_typing())
        await inbox.put(Presence(user_id=5, status="dnd"))
    await inbox.put(_typing(channel_id=2))
    await inbox.put(Presence(user_id=6, status="online"))
    await inbox.put(Presence(user_id=5, status="offline"))
    await inbox.put(_msg(2))

    assert await _take(inbox, 6) == [
        _typing(),
        Presence(user_id=5, status="offline"),
        _msg(1),
        _typing(channel_id=2),
        Presence(user_id=6, status="online"),
        _msg(2),
    ]
    assert await _is_empty(inbox)


async def test_typing_taken_by_the_consumer_no_longer_absorbs_the_next():
    inbox = _Inbox(100)
    await inbox.put(_typing())
    assert await _take(inbox, 1) == [_typing()]
    await inbox.put(_typing())
    assert await _take(inbox, 1) == [_typing()]


async def test_a_full_inbox_drops_typing_and_makes_a_message_wait():
    inbox = _Inbox(2)
    await inbox.put(_msg(1))
    await inbox.put(_msg(2))
    await asyncio.wait_for(inbox.put(_typing()), 1)
    await asyncio.wait_for(inbox.put(Presence(user_id=5, status="idle")), 1)

    waiting = asyncio.create_task(inbox.put(_msg(3)))
    await asyncio.sleep(0.05)
    assert not waiting.done()

    assert await _take(inbox, 1) == [_msg(1)]
    await asyncio.wait_for(waiting, 1)
    assert await _take(inbox, 2) == [_msg(2), _msg(3)]
    assert await _is_empty(inbox)


async def test_an_error_reaches_the_consumer_after_what_was_already_read():
    inbox = _Inbox(1)
    await inbox.put(_msg(1))
    boom = RuntimeError("reader died")
    inbox.end(boom)
    assert await _take(inbox, 1) == [_msg(1)]
    end = await inbox.get()
    assert isinstance(end, _End) and end.error is boom
