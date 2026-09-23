"""Outside #custodian Gable wakes on Claudette's phrase list, with his name."""

import tomllib
from pathlib import Path

import pytest

from config import AdapterConfig
from detector import SummonDetector
from residency_testlib import make_config, make_event

TEMPLATE = Path(__file__).resolve().parent.parent / "summon.toml.template"

PHRASES = [
    "hey gable", "hey, gable", "gable,", "gable ", "yo gable", "ok gable",
    "ok, gable", "thanks gable", "thanks, gable", "thank you gable",
    "thank you, gable", "yeah gable", "yeah, gable", "sure, gable",
    "sure gable", "alright, gable", "alright gable", "jesus gable",
    "hi gable", "hi, gable", "damn it, gable", "bots", "hey bots",
]


@pytest.fixture
def det(tmp_path):
    shipped = tomllib.loads(TEMPLATE.read_text(encoding="utf-8"))["summon"]
    config = make_config(tmp_path, summon={
        "extra_patterns": shipped["extra_patterns"],
        "custodian_mention_only": shipped["custodian_mention_only"]})
    assert isinstance(config, AdapterConfig)
    return SummonDetector(config.summon)


@pytest.mark.parametrize("phrase", PHRASES)
def test_a_phrase_at_the_start_wakes_him_outside_custodian(det, phrase):
    ev = make_event(context=None, channel_id=7, content=f"  {phrase.upper()} look at this")
    assert det.detect(ev).mode == "pattern"


@pytest.mark.parametrize("text", ["gable look", "gable, look", "hey gable!", "bots?"])
def test_the_name_ends_at_a_word_boundary_not_at_a_space(det, text):
    assert det.detect(make_event(context=None, channel_id=7, content=text)).mode == "pattern"


@pytest.mark.parametrize("phrase", PHRASES)
def test_a_phrase_mid_sentence_does_not(det, phrase):
    ev = make_event(context=None, channel_id=7, content=f"well {phrase} look")
    assert det.detect(ev) is None


@pytest.mark.parametrize("phrase", PHRASES)
def test_no_phrase_wakes_him_in_custodian(det, phrase):
    for context in (None, {"awake_users": []}):
        ev = make_event(context=context, channel_id=4, content=f"{phrase} look")
        assert det.detect(ev) is None


@pytest.mark.parametrize("phrase", PHRASES)
def test_a_bot_saying_a_phrase_wakes_nobody(det, phrase):
    ev = make_event(context=None, channel_id=7, author_type="bot",
                    content=f"{phrase} look")
    assert det.detect(ev) is None


@pytest.mark.parametrize("text", ["gabled roofs", "hey gabriel", "robots rule",
                                  "hey gabled roofs", "botswana is warm",
                                  "hi gables everywhere", "thanks gableton"])
def test_near_misses_do_not_wake_him(det, text):
    assert det.detect(make_event(context=None, channel_id=7, content=text)) is None
