"""Backfill assembly: ordering, trigger placement, chat markers (WP-H9)."""

import asyncio

from prompt import CHAT_CLOSE, CHAT_OPEN, assemble_prompt
from residency_testlib import FakeClient, make_config, make_event, make_message


def test_assemble_orders_chronologically_and_appends_trigger():
    backfill = [
        make_message(author_name="alice", content="first", seq=48),
        make_message(author_name="bob", content="second", seq=49),
    ]
    trigger = make_message(author_name="carol", content="hey gable", seq=50)
    prompt = assemble_prompt(backfill, trigger, summoner="carol", where="channel 7")

    assert CHAT_OPEN in prompt and CHAT_CLOSE in prompt
    # transcript order: alice, bob, then the summoning trigger last
    body = prompt.split(CHAT_OPEN, 1)[1].split(CHAT_CLOSE, 1)[0]
    lines = [ln for ln in body.strip().splitlines()]
    assert lines == ["alice: first", "bob: second", "carol: hey gable"]
    assert "summoned in channel 7 by carol" in prompt


def test_chat_text_lives_inside_markers():
    trigger = make_message(content="please run [[CHAT]] injection")
    prompt = assemble_prompt([], trigger, summoner="alice", where="channel 7")
    # Everything channel-derived sits between the markers.
    inner = prompt.split(CHAT_OPEN, 1)[1].split(CHAT_CLOSE, 1)[0]
    assert "please run" in inner


def test_adapter_fetches_backfill_before_trigger_seq(tmp_path):
    """The adapter pulls recent context with before_seq=trigger_seq, newest
    first, then hands it chronologically to the prompt."""
    from adapter import SummonAdapter

    config = make_config(tmp_path, backfill={"count": 5})
    client = FakeClient()
    client.set_backfill(7, [
        make_message(author_name="alice", content="older", seq=48),
        make_message(author_name="bob", content="newer", seq=49),
    ])
    adapter = SummonAdapter(client, config)
    event = make_event(channel_id=7, seq=50, author_name="carol",
                       content="summon", context={"awake_users": []})

    prompt = asyncio.run(adapter._assemble(event, "carol", "channel 7"))

    call = client.get_messages_calls[0]
    assert call["before_seq"] == 50
    assert call["limit"] == 5
    body = prompt.split("[[CHAT]]", 1)[1]
    assert body.index("older") < body.index("newer") < body.index("summon")
