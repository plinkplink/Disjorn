"""The bot-to-bot summon hop wall (spec 2026-08-24-custodian-mention-summons).

Guard 2 lives here because it needs ONE arbiter: both residents' summon
adapters spend against this counter, so a review -> revision -> fix loop cannot
buy itself twice the rounds by alternating who asks. What these tests hold down
is the shape of the wall, not its politeness:

  * a chain continues only on a LIVE work item — a card the board has in
    Review — and everything else is depth-1, which is not a refusal;
  * at the cap the item parks FOR A HUMAN, with the refusal line said in full;
  * a human post unparks it, once, however many adapters report the post;
  * the clock unparks NOTHING (Claudette #1811) — midnight rolls the daily
    ceiling and only the daily ceiling;
  * and that daily ceiling holds even across resets, which is what bounds the
    trust the broker puts in an adapter's report of a human post.
"""

from __future__ import annotations

import json

SLUG = "2026-08-24-hop-counter"


def _enable(harness):
    harness.set_verbs(**{"summon-hop": True})


def _in_review(harness, slug: str = SLUG, column: str = "Review"):
    harness.planroom_state["cards"] = [{"slug": slug, "column": column,
                                        "title": "hop counter"}]


def _spend(harness, slug=SLUG, summoner="claudette"):
    args = {"action": "spend", "summoner": summoner}
    if slug is not None:
        args["work_item"] = slug
    return harness.call("summon-hop", args)


def _unpark(harness, slug=SLUG, seq=None, by="plink"):
    args = {"action": "unpark", "work_item": slug, "summoner": by}
    if seq is not None:
        args["seq"] = seq
    return harness.call("summon-hop", args)


def _set_day(harness, day: str) -> None:
    harness.broker.hops._today_fn = lambda: day


# ── what buys a chain ────────────────────────────────────────────────────

def test_no_work_item_is_depth_1_not_a_refusal(harness):
    """Rule 1 IS the answer here. `allowed` stays true — the summon is served —
    and `chain` false tells the adapter its reply must not re-trigger anyone."""
    _enable(harness)
    res = _spend(harness, slug=None)["result"]
    assert res["allowed"] is True and res["chain"] is False
    assert res["reason"] == "no-bucket"


def test_a_slug_the_board_does_not_know_buys_no_bucket(harness):
    _enable(harness)
    harness.planroom_state["cards"] = []
    res = _spend(harness)["result"]
    assert res["allowed"] is True and res["chain"] is False


def test_a_card_outside_review_buys_no_bucket(harness):
    """The provision is review -> revision -> fix. A card in Backlog is not a
    live work loop, and naming it must not open one."""
    _enable(harness)
    _in_review(harness, column="Backlog")
    assert _spend(harness)["result"]["chain"] is False


def test_an_unreachable_board_falls_back_to_depth_1(harness):
    _enable(harness)
    _in_review(harness)
    harness.planroom_state["http_error"] = "board down"
    assert _spend(harness)["result"]["chain"] is False


def test_a_live_work_item_grants_hops_and_counts_them(harness):
    _enable(harness)
    _in_review(harness)
    first = _spend(harness)["result"]
    assert first["chain"] is True and first["count"] == 1 and first["cap"] == 8
    assert _spend(harness)["result"]["count"] == 2


def test_both_residents_spend_against_one_counter(harness):
    """The whole reason the counter is broker-side: one wall, not one each."""
    _enable(harness)
    _in_review(harness)
    _spend(harness, summoner="claudette")
    assert _spend(harness, summoner="gable")["result"]["count"] == 2


# ── the cap, and the words it refuses in ─────────────────────────────────

def test_at_the_cap_the_item_parks_for_a_human_in_the_fixed_words(harness):
    _enable(harness)
    _in_review(harness)
    for _ in range(8):
        assert _spend(harness)["result"]["allowed"] is True
    res = _spend(harness)["result"]
    assert res["allowed"] is False and res["reason"] == "parked"
    assert res["refusal"] == (
        f"summon refused: {SLUG} at 8/8 bot hops "
        "— parked until a human posts on it")


def test_the_refusal_is_audited_like_every_other_call(harness):
    _enable(harness)
    _in_review(harness)
    for _ in range(9):
        _spend(harness)
    last = harness.audit_lines()[-1]
    assert last["verb"] == "summon-hop" and last["allowed"] is True
    assert "REFUSED parked 8/8" in last["result_summary"]


# ── the unpark, and what cannot do it ────────────────────────────────────

def test_a_human_post_unparks_the_chain(harness):
    _enable(harness)
    _in_review(harness)
    for _ in range(8):
        _spend(harness)
    assert _unpark(harness, seq=1811)["result"]["reset"] is True
    resumed = _spend(harness)["result"]
    assert resumed["allowed"] is True and resumed["count"] == 1


def test_the_same_human_post_reported_twice_resets_once(harness):
    """Both adapters watch #custodian and both report the post. Without the seq
    guard the second report would be a second reset, and the daily ceiling
    would be the only counter still doing any work."""
    _enable(harness)
    _in_review(harness)
    for _ in range(8):
        _spend(harness)
    _unpark(harness, seq=1811)
    _spend(harness)                                  # 1/8
    assert _unpark(harness, seq=1811)["result"]["reset"] is False
    assert _spend(harness)["result"]["count"] == 2


def test_midnight_does_not_unpark_a_parked_chain(harness):
    """THE RULING (Claudette #1811). A chain parked at 23:59 is still parked at
    00:01: the clock rolls the daily ceiling and nothing else. Otherwise
    "parked for a human" quietly becomes "parked until tomorrow"."""
    _enable(harness)
    _in_review(harness)
    _set_day(harness, "2026-08-24")
    for _ in range(8):
        _spend(harness)
    _set_day(harness, "2026-08-25")
    res = _spend(harness)["result"]
    assert res["allowed"] is False and res["reason"] == "parked"


def test_the_daily_ceiling_holds_across_resets(harness):
    """24 hops per work item per UTC day REGARDLESS of unparks, so repeated
    human nudges cannot compound into an all-day burn."""
    _enable(harness)
    _in_review(harness)
    _set_day(harness, "2026-08-24")
    for round_no in range(3):
        for _ in range(8):
            _spend(harness)
        _unpark(harness, seq=1000 + round_no)
    res = _spend(harness)["result"]
    assert res["allowed"] is False and res["reason"] == "daily-ceiling"
    assert res["refusal"] == (
        f"summon refused: {SLUG} at 24/24 bot hops today "
        "— the daily ceiling, which clears at 00:00 UTC")


def test_the_daily_ceiling_is_the_one_thing_the_clock_does_clear(harness):
    _enable(harness)
    _in_review(harness)
    _set_day(harness, "2026-08-24")
    for round_no in range(3):
        for _ in range(8):
            _spend(harness)
        _unpark(harness, seq=1000 + round_no)
    _set_day(harness, "2026-08-25")
    # The hop counter is still where the last unpark left it, so the item is
    # live again — but only because a human unparked it, never because of the
    # date.
    assert _spend(harness)["result"]["allowed"] is True


def test_the_counter_survives_a_broker_restart(harness):
    """A parked chain that unparks itself by bouncing the daemon is not a wall.
    The state file is what makes the human gate mean anything."""
    _enable(harness)
    _in_review(harness)
    for _ in range(8):
        _spend(harness)
    state = json.loads(open(harness.broker.hops.path).read())
    assert state[SLUG]["hops"] == 8


# ── what chat and config can and cannot do ───────────────────────────────

def test_the_caps_are_configs_and_no_argument_moves_them(harness):
    _enable(harness)
    _in_review(harness)
    bad = harness.call("summon-hop", {"action": "spend", "work_item": SLUG,
                                      "cap": 500})
    assert bad["error"]["code"] == "bad-args"


def test_an_unknown_action_is_refused(harness):
    _enable(harness)
    assert harness.call("summon-hop", {"action": "reset"})["error"]["code"] \
        == "bad-args"


def test_unpark_needs_a_work_item(harness):
    _enable(harness)
    assert harness.call("summon-hop", {"action": "unpark"})["error"]["code"] \
        == "bad-args"


def test_the_verb_ships_off(harness):
    harness.set_verbs()
    assert harness.call("summon-hop", {"action": "spend"})["error"]["code"] \
        == "verb-disabled"


def test_with_no_wall_configured_everything_is_depth_1(harness):
    """[summon_hops] absent from broker.toml: no counter, no work loops, and
    the behaviour that shipped with WP-H9 — which is the safe direction."""
    _enable(harness)
    _in_review(harness)
    harness.broker.hops = None
    res = _spend(harness)["result"]
    assert res["allowed"] is True and res["chain"] is False
    assert _unpark(harness)["result"]["reset"] is False
