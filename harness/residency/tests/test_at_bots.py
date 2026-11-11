"""A person's `@bots`, as the server's `mentioned_bot_ids` list tells it."""

import asyncio

from adapter import SummonAdapter
from detector import MODE_MENTION, SummonDetector
from residency_testlib import (
    FakeArbiter,
    FakeClient,
    FakeLauncher,
    make_config,
    make_event,
    make_ready,
)

CUSTODIAN = 4
MAIN = 7
ME = 2
CONTEXT = {"awake_users": [], "addressed_by": "mention"}


def _detector(tmp_path, *, own_bot_id=ME, **summon):
    det = SummonDetector(make_config(tmp_path, summon=summon).summon)
    det.own_bot_id = own_bot_id
    return det


def _event(listed, *, context=CONTEXT, **kwargs):
    kwargs.setdefault("content", "@bots where does the build stand")
    kwargs.setdefault("channel_id", CUSTODIAN)
    event = make_event(context=context, **kwargs)
    if listed is not None:
        event.message["mentioned_bot_ids"] = listed
    return event


def test_at_bots_in_custodian_summons_when_the_server_lists_this_bot(tmp_path):
    trigger = _detector(tmp_path).detect(_event([1, ME]))
    assert trigger.mode == MODE_MENTION and trigger.depth == 0
    assert trigger.group is True
    assert "called with every bot in this room (@bots)" in trigger.describe()


def test_the_words_alone_summon_nobody_in_custodian(tmp_path):
    det = _detector(tmp_path)
    assert det.detect(_event(None)) is None
    assert det.detect(_event([])) is None
    assert det.detect(_event([1, 6])) is None
    assert det.detect(_event("2")) is None


def test_a_list_without_the_servers_context_block_does_not_summon(tmp_path):
    assert _detector(tmp_path).detect(_event([ME], context=None)) is None


def test_a_detector_that_does_not_know_its_own_id_is_not_listed(tmp_path):
    det = _detector(tmp_path, own_bot_id=None)
    assert det.detect(_event([ME])) is None
    assert det.detect(_event([None])) is None


def test_a_bot_author_is_never_read_from_the_list(tmp_path):
    det = _detector(tmp_path, bot_summon=True, peer_bots=["claudette"])
    ev = _event([ME], author_type="bot", author_id=1, author_name="claudette")
    assert det.detect(ev) is None
    assert det.detect(_event([ME], author_type="bot", author_id=1,
                             author_name="claudette", channel_id=MAIN)) is None


def test_a_named_mention_is_not_called_a_group_one(tmp_path):
    det = _detector(tmp_path)
    named = det.detect(_event([ME], content="@gable and only gable"))
    assert named.mode == MODE_MENTION and named.group is False
    assert "@bots" not in named.describe()


def test_outside_custodian_the_context_summons_and_the_list_names_the_kind(tmp_path):
    det = _detector(tmp_path)
    group = det.detect(_event([1, ME], channel_id=MAIN))
    assert group.mode == MODE_MENTION and group.group is True
    bare = det.detect(_event([], channel_id=MAIN, content="gable, thoughts?"))
    assert bare.mode == MODE_MENTION and bare.group is False


def test_the_session_is_told_it_was_called_with_the_others(tmp_path):
    client = FakeClient(events=[
        make_ready(),
        _event([1, ME], seq=50, author_name="plink"),
    ])
    launcher = FakeLauncher()
    adapter = SummonAdapter(client, make_config(tmp_path), launcher=launcher,
                            hops=FakeArbiter())
    asyncio.run(adapter.run())

    assert len(launcher.prompts) == 1
    assert ("How you were woken: mention by plink (user); "
            "called with every bot in this room (@bots); chain depth 0.") in launcher.prompts[0]
    assert client.replies_to(CUSTODIAN)[0].content == "Hello from Gable."
