"""Mention-only #custodian + bot-to-bot summons (spec 2026-08-24).

The four guards, from the outside:

  1. depth-1 — a bot-triggered summon's reply cannot re-trigger any bot;
  2. the work-loop provision — a chain the broker grants keeps its mentions;
  3. loud refusal — never a silent drop, always attributed;
  4. budget + attribution — a summon spends the SUMMONED seat's budget and the
     footer says whose turn it was.

Plus the request that started it: a bare name in #custodian is inert data, so a
human can type "gable" mid-sentence without summoning anyone.
"""

import asyncio
import json

from adapter import SummonAdapter
from detector import (
    MODE_BOT_CHAIN,
    MODE_DIGEST,
    MODE_MENTION,
    MODE_PATTERN,
    SummonDetector,
    demote_mentions,
    find_work_item,
)
from hops import HopDecision
from launcher import SessionResult
from residency_testlib import (
    FakeArbiter,
    FakeClient,
    FakeLauncher,
    make_config,
    make_event,
)

CUSTODIAN = 4
MAIN = 7


def _detector(tmp_path, **summon):
    return SummonDetector(make_config(tmp_path, summon=summon).summon)


def _chain_config(tmp_path, **summon):
    base = {"bot_summon": True, "peer_bots": ["claudette"]}
    base.update(summon)
    return make_config(tmp_path, summon=base)


def _run(adapter):
    asyncio.run(adapter.run())


# ── #custodian is mention-only ───────────────────────────────────────────

def test_a_bare_name_in_custodian_is_inert(tmp_path):
    """THE REQUEST (plink, seq 1802). The server attaches its context block on
    a bare name too, so this message arrives looking exactly like a mention;
    what makes it inert is the missing @."""
    det = _detector(tmp_path)
    ev = make_event(channel_id=CUSTODIAN, context={"awake_users": []},
                    content="I was talking to gable about the hop counter")
    assert det.detect(ev) is None


def test_an_explicit_mention_in_custodian_summons(tmp_path):
    det = _detector(tmp_path)
    ev = make_event(channel_id=CUSTODIAN, context={"awake_users": []},
                    content="@gable can you read this spec")
    trigger = det.detect(ev)
    assert trigger is not None and trigger.mode == MODE_MENTION


def test_a_forged_at_without_server_context_still_does_not_summon(tmp_path):
    """The @ check only ever NARROWS. Content alone never summons — the
    server-attested block is still necessary — so no authority moved into
    chat."""
    det = _detector(tmp_path)
    ev = make_event(channel_id=CUSTODIAN, context=None, content="@gable hi")
    assert det.detect(ev) is None


def test_patterns_and_trigger_channels_are_off_in_custodian(tmp_path):
    det = _detector(tmp_path, trigger_channels=[CUSTODIAN],
                    extra_patterns=[r"\bhey gable\b"])
    ev = make_event(channel_id=CUSTODIAN, context=None, content="oh hey gable")
    assert det.detect(ev) is None


def test_other_channels_are_unchanged(tmp_path):
    """'Applies to #custodian only.' A name-match in #main still summons."""
    det = _detector(tmp_path, extra_patterns=[r"\bhey gable\b"])
    named = make_event(channel_id=MAIN, context={"awake_users": []},
                       content="gable, thoughts?")
    assert det.detect(named).mode == MODE_MENTION
    pattern = make_event(channel_id=MAIN, context=None, content="oh hey gable")
    assert det.detect(pattern).mode == MODE_PATTERN


def test_mention_only_can_be_switched_off(tmp_path):
    det = _detector(tmp_path, custodian_mention_only=False)
    ev = make_event(channel_id=CUSTODIAN, context={"awake_users": []},
                    content="gable said something")
    assert det.detect(ev).mode == MODE_MENTION


# ── who may open a chain ─────────────────────────────────────────────────

def test_bot_authors_stay_inert_until_plink_turns_them_on(tmp_path):
    det = _detector(tmp_path)
    ev = make_event(channel_id=CUSTODIAN, author_type="bot",
                    author_name="claudette", content="@gable take a look",
                    context={"awake_users": []})
    assert det.detect(ev) is None


def test_a_bot_mention_in_custodian_opens_a_chain_at_depth_1(tmp_path):
    det = SummonDetector(_chain_config(tmp_path).summon)
    ev = make_event(channel_id=CUSTODIAN, author_type="bot",
                    author_name="claudette", context={"awake_users": []},
                    content="@gable review 2026-08-24-hop-counter please")
    trigger = det.detect(ev)
    assert trigger.mode == MODE_BOT_CHAIN
    assert trigger.summoner_type == "bot" and trigger.depth == 1
    assert trigger.work_item == "2026-08-24-hop-counter"


def test_a_bot_cannot_open_a_chain_outside_a_mention_only_channel(tmp_path):
    """Guard 1 is enforced by demoting @mentions, and a demoted mention is only
    inert where bare names are. Outside #custodian there is no wall to enforce,
    so there is no chain."""
    det = SummonDetector(_chain_config(tmp_path).summon)
    ev = make_event(channel_id=MAIN, author_type="bot", author_name="claudette",
                    context={"awake_users": []}, content="@gable look")
    assert det.detect(ev) is None


def test_a_bot_naming_us_without_the_at_is_inert(tmp_path):
    det = SummonDetector(_chain_config(tmp_path).summon)
    ev = make_event(channel_id=CUSTODIAN, author_type="bot",
                    author_name="claudette", context={"awake_users": []},
                    content="gable already answered that one")
    assert det.detect(ev) is None


# ── the digest carve-out ─────────────────────────────────────────────────

def test_the_daily_digest_wakes_only_the_seat_configured_for_it(tmp_path):
    off = _detector(tmp_path, digest_author_ids=[3])
    on = _detector(tmp_path, wake_on_digest=True, digest_author_ids=[3])
    digest = make_event(channel_id=CUSTODIAN, author_type="bot", author_id=3,
                        author_name="broker",
                        content="[custodian daily 2026-08-24 UTC — complete day")
    assert off.detect(digest) is None
    assert on.detect(digest).mode == MODE_DIGEST


def test_another_bot_typing_the_digest_header_does_not_ring_the_bell(tmp_path):
    det = _detector(tmp_path, wake_on_digest=True, digest_author_ids=[3])
    ev = make_event(channel_id=CUSTODIAN, author_type="bot", author_id=9,
                    author_name="claudette",
                    content="[custodian daily 2026-08-24 UTC — complete day")
    assert det.detect(ev) is None


# ── work items ───────────────────────────────────────────────────────────

def test_work_item_shapes():
    assert find_work_item("please fix 2026-08-24-hop-counter.md") == \
        "2026-08-24-hop-counter"
    assert find_work_item("see backlog-17 and keyboard-a1b2c3d") == "backlog-17"
    assert find_work_item("nothing cited here") is None


# ── guard 1: the reply cannot re-trigger ─────────────────────────────────

def test_demote_mentions_leaves_the_sentence_readable():
    assert demote_mentions("thanks @claudette — over to you", ["claudette"]) == \
        "thanks claudette — over to you"


def test_a_depth_1_reply_cannot_retrigger_anyone(tmp_path):
    config = _chain_config(tmp_path)
    client = FakeClient(events=[
        make_event(channel_id=CUSTODIAN, seq=50, msg_id=1, author_type="bot",
                   author_name="claudette", context={"awake_users": []},
                   content="@gable what do you make of this"),
    ])
    launcher = FakeLauncher(SessionResult(
        ok=True, reply="agreed, @claudette should take the next pass.",
        action_count=1, duration_sec=0.5))
    _run(SummonAdapter(client, config, launcher=launcher,
                       hops=FakeArbiter()))

    reply = client.replies_to(CUSTODIAN)[0].content
    assert "@claudette" not in reply
    assert "claudette should take the next pass" in reply


def test_a_human_triggered_reply_keeps_its_mentions(tmp_path):
    """Depth-1 is a chain rule, not a mute. A human's summon may still hand off
    to another bot — that handoff IS depth 1."""
    config = _chain_config(tmp_path)
    client = FakeClient(events=[
        make_event(channel_id=CUSTODIAN, seq=50, author_name="plink",
                   context={"awake_users": []}, content="@gable thoughts?"),
    ])
    launcher = FakeLauncher(SessionResult(
        ok=True, reply="@claudette owns that lane.", action_count=1,
        duration_sec=0.5))
    _run(SummonAdapter(client, config, launcher=launcher, hops=FakeArbiter()))
    assert "@claudette" in client.replies_to(CUSTODIAN)[0].content


# ── guard 2: the work loop ───────────────────────────────────────────────

def test_a_granted_chain_keeps_its_mentions_and_reports_its_depth(tmp_path):
    config = _chain_config(tmp_path)
    arbiter = FakeArbiter(decision=HopDecision(
        allowed=True, chain=True, reason="hop",
        work_item="2026-08-24-hop-counter", count=3, cap=8))
    client = FakeClient(events=[
        make_event(channel_id=CUSTODIAN, seq=50, author_type="bot",
                   author_name="claudette", context={"awake_users": []},
                   content="@gable revision on 2026-08-24-hop-counter"),
    ])
    launcher = FakeLauncher(SessionResult(
        ok=True, reply="fixed — @claudette, back to you.", action_count=1,
        duration_sec=0.5))
    _run(SummonAdapter(client, config, launcher=launcher, hops=arbiter))

    assert arbiter.spends == [{"work_item": "2026-08-24-hop-counter",
                               "summoner": "claudette"}]
    assert "@claudette" in client.replies_to(CUSTODIAN)[0].content
    # The session is TOLD the mode and the depth (Claudette #1803 cond. 2).
    prompt = launcher.prompts[0]
    assert "bot-chain by claudette (bot)" in prompt
    assert "chain depth 3 of 8" in prompt
    assert "work item 2026-08-24-hop-counter" in prompt


def test_a_human_post_on_a_work_item_unparks_it(tmp_path):
    config = _chain_config(tmp_path)
    arbiter = FakeArbiter()
    client = FakeClient(events=[
        make_event(channel_id=CUSTODIAN, seq=60, author_name="plink",
                   context=None,
                   content="had a look at 2026-08-24-hop-counter, carry on"),
    ])
    _run(SummonAdapter(client, config, launcher=FakeLauncher(), hops=arbiter))
    assert arbiter.unparks == [{"work_item": "2026-08-24-hop-counter",
                                "by": "plink", "seq": 60}]


def test_a_human_summon_on_the_work_item_also_unparks(tmp_path):
    """'A human summon of either bot on it counts.' The reset must not be
    reachable only through the not-a-summon path."""
    config = _chain_config(tmp_path)
    arbiter = FakeArbiter()
    client = FakeClient(events=[
        make_event(channel_id=CUSTODIAN, seq=61, author_name="plink",
                   context={"awake_users": []},
                   content="@gable where did 2026-08-24-hop-counter land?"),
    ])
    _run(SummonAdapter(client, config, launcher=FakeLauncher(), hops=arbiter))
    assert arbiter.unparks[0]["work_item"] == "2026-08-24-hop-counter"


def test_a_bot_post_never_unparks(tmp_path):
    config = _chain_config(tmp_path)
    arbiter = FakeArbiter()
    client = FakeClient(events=[
        make_event(channel_id=CUSTODIAN, seq=62, author_type="bot",
                   author_name="claudette", context=None,
                   content="still working 2026-08-24-hop-counter"),
    ])
    _run(SummonAdapter(client, config, launcher=FakeLauncher(), hops=arbiter))
    assert arbiter.unparks == []


# ── guard 3: loud refusal ────────────────────────────────────────────────

def test_a_capped_chain_is_refused_in_channel_in_the_brokers_words(tmp_path):
    config = _chain_config(tmp_path)
    refusal = ("summon refused: 2026-08-24-hop-counter at 8/8 bot hops "
               "— parked until a human posts on it")
    arbiter = FakeArbiter(decision=HopDecision(
        allowed=False, chain=False, reason="parked", refusal=refusal,
        work_item="2026-08-24-hop-counter", count=8, cap=8))
    client = FakeClient(events=[
        make_event(channel_id=CUSTODIAN, seq=50, msg_id=7, author_type="bot",
                   author_name="claudette", context={"awake_users": []},
                   content="@gable another pass on 2026-08-24-hop-counter"),
    ])
    launcher = FakeLauncher()
    _run(SummonAdapter(client, config, launcher=launcher, hops=arbiter))

    assert launcher.prompts == []            # no session ran
    posted = client.replies_to(CUSTODIAN)[0]
    assert posted.content.startswith(refusal)
    assert "summoned by claudette" in posted.content   # attributed
    assert "refused by the house broker" in posted.content
    assert posted.reply_to == 7
    # and the refusal is audited on its own line, never silence
    assert any("summon refused | claudette" in s.content
               for s in client.replies_to(CUSTODIAN)[1:])
    # a refused summon spends nothing
    assert not (tmp_path / "budget.json").exists()


def test_a_bot_off_the_allowlist_is_refused_not_ignored(tmp_path):
    config = _chain_config(tmp_path, peer_bots=["claudette"])
    client = FakeClient(events=[
        make_event(channel_id=CUSTODIAN, seq=50, author_type="bot",
                   author_name="stranger", context={"awake_users": []},
                   content="@gable do a thing"),
    ])
    launcher = FakeLauncher()
    arbiter = FakeArbiter()
    _run(SummonAdapter(client, config, launcher=launcher, hops=arbiter))

    assert launcher.prompts == [] and arbiter.spends == []
    reply = client.replies_to(CUSTODIAN)[0].content
    assert "not on this seat's bot allowlist" in reply
    assert "summoned by stranger" in reply


# ── guard 4: budget + attribution ────────────────────────────────────────

def test_a_bot_summon_spends_the_summoned_seats_budget(tmp_path):
    config = _chain_config(tmp_path)
    client = FakeClient(events=[
        make_event(channel_id=CUSTODIAN, seq=50, author_type="bot",
                   author_name="claudette", context={"awake_users": []},
                   content="@gable one question"),
    ])
    _run(SummonAdapter(client, config, launcher=FakeLauncher(),
                       hops=FakeArbiter()))
    assert json.loads((tmp_path / "budget.json").read_text())["count"] == 1
    assert "summoned by claudette" in client.replies_to(CUSTODIAN)[0].content


def test_an_exhausted_seat_refuses_before_it_spends_a_hop(tmp_path):
    """A summon this seat cannot afford must not burn a hop off the SHARED
    counter on its way to being refused."""
    config = _chain_config(tmp_path, **{})
    config.budget.daily_session_cap = 0
    arbiter = FakeArbiter()
    client = FakeClient(events=[
        make_event(channel_id=CUSTODIAN, seq=50, author_type="bot",
                   author_name="claudette", context={"awake_users": []},
                   content="@gable one question"),
    ])
    _run(SummonAdapter(client, config, launcher=FakeLauncher(), hops=arbiter))
    assert arbiter.spends == []
    assert "summoned by claudette" in client.replies_to(CUSTODIAN)[0].content
