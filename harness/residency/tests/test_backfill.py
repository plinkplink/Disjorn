"""Backfill assembly: ordering, trigger placement, chat markers (WP-H9);
per-channel backfill depth (WP-L1)."""

import asyncio

import pytest

from config import AdapterConfig
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


# --------------------------------------------------------- per-channel depth (WP-L1)


def test_backfill_default_when_per_channel_absent():
    """No [backfill.per_channel] table: every channel gets the default count."""
    cfg = AdapterConfig.from_dict({"backfill": {"count": 30}})
    assert cfg.backfill.per_channel == {}
    assert cfg.backfill.count_for(4) == 30
    assert cfg.backfill.count_for(99) == 30


def test_backfill_per_channel_override_applied():
    """A per-channel entry deepens that channel; others keep the default."""
    cfg = AdapterConfig.from_dict(
        {"backfill": {"count": 30, "per_channel": {"4": 100}}}
    )
    assert cfg.backfill.count_for(4) == 100      # #custodian: deep window
    assert cfg.backfill.count_for(7) == 30       # untouched channel: default


def test_backfill_malformed_per_channel_rejected():
    """Non-integer depth fails loud at parse time, not silently at fetch."""
    with pytest.raises(ValueError):
        AdapterConfig.from_dict(
            {"backfill": {"count": 30, "per_channel": {"4": "deep"}}}
        )


def test_adapter_uses_custodian_depth_for_custodian_channel(tmp_path):
    """The adapter fetches the #custodian override depth when the summon
    originates there, and the default depth elsewhere."""
    from adapter import SummonAdapter

    config = make_config(
        tmp_path, backfill={"count": 30, "per_channel": {4: 100}}
    )
    client = FakeClient()
    adapter = SummonAdapter(client, config)

    custodian = make_event(channel_id=4, seq=140, author_name="carol",
                           content="design thread", context={"awake_users": []})
    asyncio.run(adapter._assemble(custodian, "carol", "#custodian"))
    assert client.get_messages_calls[-1]["limit"] == 100

    main = make_event(channel_id=7, seq=50, author_name="carol",
                      content="quick q", context={"awake_users": []})
    asyncio.run(adapter._assemble(main, "carol", "channel 7"))
    assert client.get_messages_calls[-1]["limit"] == 30
