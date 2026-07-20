"""Summon detection (WP-H9)."""

from detector import SummonDetector
from residency_testlib import make_config, make_event


def _detector(tmp_path, **summon):
    return SummonDetector(make_config(tmp_path, summon=summon).summon)


def test_mention_context_summons(tmp_path):
    det = _detector(tmp_path)
    # Server attaches context only to a mentioned bot's copy.
    assert det.is_summon(make_event(context={"awake_users": []})) is True


def test_no_context_no_pattern_is_ignored(tmp_path):
    det = _detector(tmp_path)
    assert det.is_summon(make_event(context=None, content="just chatting")) is False


def test_trigger_channel_summons_without_mention(tmp_path):
    det = _detector(tmp_path, trigger_channels=[7])
    ev = make_event(channel_id=7, context=None, content="no mention here")
    assert det.is_summon(ev) is True


def test_extra_pattern_summons(tmp_path):
    det = _detector(tmp_path, extra_patterns=[r"\bhey gable\b"])
    assert det.is_summon(make_event(context=None, content="oh hey gable")) is True
    assert det.is_summon(make_event(context=None, content="hey gabriel")) is False


def test_bot_authors_never_summon(tmp_path):
    det = _detector(tmp_path)
    # Even with a mention context, a bot author (e.g. Gable's own reply) is
    # never a summon — no loops.
    ev = make_event(author_type="bot", context={"awake_users": []})
    assert det.is_summon(ev) is False


def test_backfilled_history_never_summons(tmp_path):
    det = _detector(tmp_path)
    ev = make_event(context={"awake_users": []}, backfilled=True)
    assert det.is_summon(ev) is False


def test_trigger_on_context_can_be_disabled(tmp_path):
    det = _detector(tmp_path, trigger_on_context=False)
    assert det.is_summon(make_event(context={"awake_users": []})) is False


def test_summoner_name(tmp_path):
    det = _detector(tmp_path)
    assert det.summoner_name(make_event(author_name="bob")) == "bob"
