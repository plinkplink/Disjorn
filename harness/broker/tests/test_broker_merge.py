"""The `merge` verb: a human's `/merge`, the broker's own gates, one commit on
the gatehouse's main."""

from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from brokerd import Broker, ConfigError, merge_commit_message  # noqa: E402
from broker_testlib import harness  # noqa: E402,F401

CHANNEL = 7
SEQ = 300
SLUG = "2026-09-20-a-typo-in-the-docs"
PASS_SEQ = 2704


def later(seconds: int = 60) -> str:
    stamp = dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=seconds)
    return stamp.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def earlier(seconds: int = 3600) -> str:
    return later(-seconds)


def arm(h, *, slug: str = SLUG, author: str = "plink",
        author_type: str = "user", flags: str = "{}", content: str | None = None,
        path: str = "docs/new.md", branch: bool = True, tier: int = 1,
        gate_exit: int = 0, message: bool = True):
    """A `server` caller, the verb on, a real gatehouse, and the gates stubbed."""
    h.become_server()
    h.set_verbs("server", merge=True, build=True)
    h.make_gatehouse()
    if branch:
        h.push_branch(slug, path=path)
    if message:
        h.add_message(CHANNEL, SEQ, content or f"/merge {slug}", author=author,
                      author_type=author_type, flags=flags)
    h.set_tier(tier)
    return h.stub_gates(exit_code=gate_exit,
                        tests=(gate_exit == 0),
                        summary="server 12 passed; harness 4 passed")


def call(h, *, slug: str = SLUG, seq: int = SEQ, channel: int = CHANNEL,
         pass_seq: int | None = None):
    args = {"seq": seq, "channel_id": channel, "slug": slug}
    if pass_seq is not None:
        args["pass_seq"] = pass_seq
    return h.call("merge", args)


def refusal(resp) -> tuple[str, str]:
    return resp["error"]["reason"], resp["error"]["message"]


# ── the wire ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize("args", [
    {"seq": SEQ, "channel_id": CHANNEL},
    {"seq": SEQ, "slug": SLUG},
    {"channel_id": CHANNEL, "slug": SLUG},
    {"seq": SEQ, "channel_id": CHANNEL, "slug": SLUG, "tests": True},
    {"seq": SEQ, "channel_id": CHANNEL, "slug": SLUG, "gates": {}},
    {"seq": 0, "channel_id": CHANNEL, "slug": SLUG},
    {"seq": SEQ, "channel_id": CHANNEL, "slug": SLUG, "pass_seq": 0},
])
def test_the_arg_schema_admits_no_caller_supplied_gates(harness, args):
    arm(harness)
    assert harness.call("merge", args)["error"]["code"] == "bad-args"


def test_pass_seq_is_optional(harness):
    arm(harness, tier=1)
    assert call(harness)["ok"] is True


# ── step 1: the human ────────────────────────────────────────────────────

def test_a_bot_authored_merge_is_refused(harness):
    arm(harness, author="gable", author_type="bot")
    reason, message = refusal(call(harness))
    assert reason == "human" and "only a person" in message


def test_a_privacy_flagged_merge_is_refused(harness):
    arm(harness, flags='{"off_the_record": true}')
    assert refusal(call(harness))[0] == "human"


def test_an_author_not_on_the_human_list_is_refused(harness):
    arm(harness, author="stranger")
    reason, message = refusal(call(harness))
    assert reason == "human" and "human list" in message


def test_an_unknown_seq_is_refused(harness):
    arm(harness, message=False)
    reason, message = refusal(call(harness))
    assert reason == "human" and "no message 300" in message


def test_a_refusal_is_audited_as_a_denial(harness):
    arm(harness, author="stranger")
    call(harness)
    entry = harness.audit_lines()[-1]
    assert entry["verb"] == "merge" and entry["allowed"] is False
    assert entry["result_summary"].startswith("denied: ")
    assert "(human)" in entry["result_summary"]
    assert entry["resident"] == "server"


# ── step 2: the branch ───────────────────────────────────────────────────

def test_a_missing_branch_is_refused(harness):
    arm(harness, branch=False)
    reason, message = refusal(call(harness))
    assert reason == "branch-missing"
    assert f"no loop/{SLUG}" in message


@pytest.mark.parametrize("slug", ["not-a-build-slug", "2026-09-20-", "../etc"])
def test_a_slug_that_is_not_a_build_slug_is_refused(harness, slug):
    arm(harness)
    assert refusal(call(harness, slug=slug))[0] == "branch-missing"


def test_a_branch_missing_merge_never_runs_the_gates(harness):
    calls = arm(harness, branch=False)
    call(harness)
    assert calls == []


# ── step 3: the gates are the broker's own ───────────────────────────────

def test_the_gates_run_under_the_build_seat_with_the_gate_argv(harness):
    calls = arm(harness)
    harness.broker.start_build["command"] = ["sudo", "-n", "/l/launch", "run"]
    assert call(harness)["ok"] is True
    assert calls[0]["argv_prefix"] == ["sudo", "-n", "/l/launch", "gate"]
    assert calls[0]["seat"] == "test" and calls[0]["slug"] == SLUG
    assert calls[0]["timeout"] == 30
    assert calls[0]["log_dir"].endswith("gate-logs")


def test_a_red_gate_is_tier_two_and_refuses_without_a_pass(harness):
    """The red gate is NOT special-cased: the classifier answers Tier 2 and the
    Tier 2 rule is what refuses."""
    arm(harness, gate_exit=1, tier=0)
    reason, message = refusal(call(harness))
    assert reason == "pass-missing"
    assert "Tier 2" in message and f"/merge {SLUG} pass <seq>" in message
    assert harness.main_subjects() == ["init"]


# ── step 5: tier 0 and tier 1 merge on this call ─────────────────────────

@pytest.mark.parametrize("tier", [0, 1])
def test_a_tier_below_two_merges_and_stamps_merge_seq_last(harness, tier):
    arm(harness, tier=tier)
    resp = call(harness)
    assert resp["ok"] is True
    result = resp["result"]
    assert result == {"merged": True, "slug": SLUG, "sha": result["sha"],
                      "tier": tier}
    assert harness.main_subjects()[0] == (
        f"merge: {SLUG} (/merge by plink, tier {tier})")
    body = harness.commit_message().strip().splitlines()
    assert body[-1] == f"merge-seq: {CHANNEL}:{SEQ}"
    assert "review-seq" not in harness.commit_message()


def test_the_merge_commit_is_a_no_ff_merge_by_the_broker(harness):
    arm(harness, tier=1)
    call(harness)
    import broker_testlib as T
    assert T._git(harness.gatehouse, "log", "-1", "--format=%an <%ae>",
                  "main").strip() == "disjorn-broker <broker@disjorn.local>"
    parents = T._git(harness.gatehouse, "log", "-1", "--format=%P",
                     "main").split()
    assert len(parents) == 2


def test_the_merge_line_lands_on_the_ledger(harness):
    arm(harness, tier=1)
    sha = call(harness)["result"]["sha"]
    line = harness.build_ledger_lines()[-1]
    assert line == {"ts": line["ts"], "kind": "merge", "seq": SEQ,
                    "channel_id": CHANNEL, "author": "plink", "slug": SLUG,
                    "tier": 1, "sha": sha, "pass_seq": None}


def test_the_mirror_and_the_board_follow_the_merge(harness):
    arm(harness, tier=1)
    assert call(harness)["ok"] is True
    ran = [a for a in harness.recorded_argv() if "--ff-only" in a]
    assert ran, "the mirror was never fast-forwarded after the merge"


# ── step 5: tier 2 needs a reviewer's PASS ───────────────────────────────

def test_tier_two_without_a_pass_is_refused(harness):
    arm(harness, tier=2)
    reason, message = refusal(call(harness))
    assert reason == "pass-missing" and "#custodian" in message
    assert harness.main_subjects() == ["init"]


def test_a_valid_pass_merges_with_review_seq_after_merge_seq(harness):
    arm(harness, tier=2)
    harness.add_custodian_post(PASS_SEQ, f"PASS {SLUG} — read it, it is fine",
                               author="Claudette", created_at=later())
    resp = call(harness, pass_seq=PASS_SEQ)
    assert resp["ok"] is True and resp["result"]["tier"] == 2
    body = harness.commit_message().strip().splitlines()
    assert body[0] == f"merge: {SLUG} (/merge by plink, tier 2)"
    assert body[-2] == f"merge-seq: {CHANNEL}:{SEQ}"
    assert body[-1] == f"review-seq: {PASS_SEQ}"
    assert harness.build_ledger_lines()[-1]["pass_seq"] == PASS_SEQ


def test_a_pass_from_the_wrong_reviewer_is_refused(harness):
    arm(harness, tier=2)
    harness.add_custodian_post(PASS_SEQ, f"PASS {SLUG}", author="Gable",
                               created_at=later())
    reason, message = refusal(call(harness, pass_seq=PASS_SEQ))
    assert reason == "pass-invalid"
    assert "Gable's" in message and "Claudette" in message
    assert harness.main_subjects() == ["init"]


def test_a_pass_from_a_person_is_not_a_reviewers_post(harness):
    arm(harness, tier=2)
    harness.add_custodian_post(PASS_SEQ, f"PASS {SLUG}", author="plink",
                               author_type="user", created_at=later())
    reason, message = refusal(call(harness, pass_seq=PASS_SEQ))
    assert reason == "pass-invalid" and "reviewer's post" in message


def test_a_post_that_never_says_pass_is_refused(harness):
    arm(harness, tier=2)
    harness.add_custodian_post(PASS_SEQ, f"{SLUG} looks fine to me",
                               author="Claudette", created_at=later())
    reason, message = refusal(call(harness, pass_seq=PASS_SEQ))
    assert reason == "pass-invalid" and "does not say PASS" in message


def test_a_pass_that_names_another_slug_is_refused(harness):
    arm(harness, tier=2)
    harness.add_custodian_post(PASS_SEQ, "PASS 2026-09-20-something-else",
                               author="Claudette", created_at=later())
    assert refusal(call(harness, pass_seq=PASS_SEQ))[0] == "pass-invalid"


def test_a_pass_posted_before_the_tip_is_refused(harness):
    """A reviewer cannot have read a commit that did not exist yet."""
    arm(harness, tier=2)
    harness.add_custodian_post(PASS_SEQ, f"PASS {SLUG}", author="Claudette",
                               created_at=earlier())
    reason, message = refusal(call(harness, pass_seq=PASS_SEQ))
    assert reason == "pass-invalid" and "before the tip" in message


def test_a_pass_in_another_channel_is_refused(harness):
    arm(harness, tier=2)
    harness.add_custodian_post(PASS_SEQ, f"PASS {SLUG}", author="Claudette",
                               created_at=later(), channel_id=CHANNEL + 50)
    reason, message = refusal(call(harness, pass_seq=PASS_SEQ))
    assert reason == "pass-invalid"
    assert f"no message {PASS_SEQ} in #custodian" in message


def test_a_changed_path_with_no_lane_owner_is_a_keyboard_merge(harness):
    arm(harness, tier=2, path="harness/broker/brokerd.py")
    harness.add_custodian_post(PASS_SEQ, f"PASS {SLUG}", author="Claudette",
                               created_at=later())
    reason, message = refusal(call(harness, pass_seq=PASS_SEQ))
    assert reason == "pass-invalid"
    assert message == ("no lane owner for harness/broker/brokerd.py; "
                       "keyboard merge")


def test_a_pass_is_ignored_below_tier_two(harness):
    arm(harness, tier=1)
    harness.add_custodian_post(PASS_SEQ, "nothing to do with it",
                               author="Gable", created_at=earlier())
    assert call(harness, pass_seq=PASS_SEQ)["ok"] is True
    assert "review-seq" not in harness.commit_message()


# ── step 7: the merge itself ─────────────────────────────────────────────

def test_a_conflict_refuses_and_moves_nothing(harness):
    arm(harness, tier=1, path="docs/a.md")
    harness.move_main()
    before = harness.main_subjects()
    reason, message = refusal(call(harness))
    assert reason == "conflict" and "nothing moved" in message
    assert harness.main_subjects() == before


def test_the_merge_workspace_is_thrown_away(harness):
    arm(harness, tier=1)
    assert call(harness)["ok"] is True
    work = Path(harness.broker.merge_work_dir)
    assert list(work.iterdir()) == []


# ── the /build end: gates, the banner, and the Tier 0 self-merge ─────────

def outcome(h, *, slug: str = SLUG, sha: str = "deadbeef" * 5,
            unit_reason=None, published=True, no_commits=False):
    publish: dict = {"published": [("disjorn.git", sha)] if published else []}
    if no_commits:
        publish["no_commits"] = ["disjorn.git"]
    h.broker._seq_build_outcome(
        slug=slug, branch=f"loop/{slug}", publish=publish,
        origin={"channel_id": CHANNEL, "session_id": 12, "seq": SEQ,
                "author": "plink"},
        report={"files": "docs/new.md", "tests": "ok", "diff": "+1 -0"},
        unit_reason=unit_reason)
    return h.channel_posts[-1]["body"].splitlines()


def stages(h) -> list[tuple[str, dict]]:
    return [(c["payload"]["stage"], c["payload"]["detail"])
            for c in h.planroom_calls if c["path"].endswith("/stage")]


def test_a_green_tier_zero_build_merges_itself(harness):
    arm(harness, tier=0)
    lines = outcome(harness)
    assert len(lines) == 4
    assert lines[0] == ("tests: pass — server 12 passed; harness 4 passed")
    assert lines[1] == "tier: 0 — stub says tier 0; second reason"
    assert lines[2].startswith("diffstat: 1 file changed")
    sha = harness.build_ledger_lines()[-1]["sha"]
    assert lines[3] == f"next: merged {sha}"
    assert harness.main_subjects()[0].startswith(f"merge: {SLUG}")
    assert harness.commit_message().strip().splitlines()[-1] == (
        f"merge-seq: {CHANNEL}:{SEQ}")


def test_the_deployed_stage_carries_the_tier_and_the_merge(harness):
    arm(harness, tier=0)
    outcome(harness)
    deployed = [d for s, d in stages(harness) if s == "deployed"][0]
    assert deployed["tier"] == 0
    assert deployed["merged_sha"] == harness.build_ledger_lines()[-1]["sha"]
    assert deployed["branch"] == f"loop/{SLUG}"


def test_a_tier_one_build_waits_for_one_merge(harness):
    arm(harness, tier=1)
    lines = outcome(harness)
    assert lines[1] == "tier: 1 — stub says tier 1; second reason"
    assert lines[3] == f"next: /merge {SLUG}"
    assert harness.main_subjects() == ["init"]
    deployed = [d for s, d in stages(harness) if s == "deployed"][0]
    assert "merged_sha" not in deployed and "tier" not in deployed


def test_a_tier_two_build_names_the_lane_reviewer(harness):
    arm(harness, tier=2)
    lines = outcome(harness)
    assert lines[3] == (f"next: PASS from Claudette in #custodian, then "
                        f"/merge {SLUG} pass <seq>")


def test_a_tier_two_build_with_no_lane_owner_names_a_reviewer(harness):
    arm(harness, tier=2, path="harness/x.md")
    assert outcome(harness)[3] == (
        f"next: PASS from a reviewer in #custodian, then "
        f"/merge {SLUG} pass <seq>")


def test_a_red_gate_sends_the_build_back(harness):
    arm(harness, tier=0, gate_exit=1)
    lines = outcome(harness)
    assert lines[0].startswith("tests: fail — ")
    assert lines[1].startswith("tier: 2 — gate failed: tests")
    assert lines[3] == "next: fix the red gate, then /build again"
    assert harness.main_subjects() == ["init"]


def test_the_tier_zero_budget_is_counted_from_the_ledger(harness):
    arm(harness, tier=0)
    (Path(harness.broker._protected_paths())
     .write_text("[limits]\ndaily_auto_apply_budget = 1\n"))
    harness.push_branch("2026-09-20-second-typo", path="docs/second.md")
    assert outcome(harness)[3].startswith("next: merged ")
    lines = outcome(harness, slug="2026-09-20-second-typo")
    assert lines[3] == "next: Tier 0 budget spent today; /merge 2026-09-20-second-typo"
    merges = [x for x in harness.main_subjects() if x.startswith("merge: ")]
    assert len(merges) == 1


def test_a_human_merge_is_never_budgeted(harness):
    arm(harness, tier=0)
    (Path(harness.broker._protected_paths())
     .write_text("[limits]\ndaily_auto_apply_budget = 0\n"))
    assert call(harness)["ok"] is True


def test_a_build_with_no_commits_gates_nothing_and_says_so(harness):
    calls = arm(harness, tier=0)
    lines = outcome(harness, published=False, no_commits=True)
    assert calls == []
    assert len(lines) == 4
    assert lines[0] == "tests: n/a — nothing was gated"
    assert lines[1] == "tier: n/a — nothing to classify"
    assert lines[3] == "next: no commits — /build again with more detail"


def test_a_halted_build_still_posts_a_banner_carrying_the_reason(harness):
    """The room sees no turn line for a repo build, so the reason has to be
    here or it is nowhere."""
    calls = arm(harness, tier=0)
    lines = outcome(harness, published=False,
                    unit_reason="exit 1: the seat could not run the suite")
    assert calls == []
    assert len(lines) == 4
    assert lines[0] == "tests: n/a — nothing was gated"
    assert lines[3] == ("next: build halted — exit 1: the seat could not run "
                        "the suite; /build again")
    halted = [d for s, d in stages(harness) if d.get("halted")]
    assert halted and halted[0]["reason"].startswith("exit 1")


def test_a_build_with_no_seq_in_its_origin_never_self_merges(harness):
    """A build adopted from before the trailer existed cannot cite a seq, and a
    merge with nothing to cite is a merge nobody authorized."""
    arm(harness, tier=0)
    harness.broker._seq_build_outcome(
        slug=SLUG, branch=f"loop/{SLUG}",
        publish={"published": [("disjorn.git", "a" * 40)]},
        origin={"channel_id": CHANNEL, "session_id": 12},
        report={"files": "x", "tests": "ok", "diff": ""})
    assert harness.channel_posts[-1]["body"].splitlines()[3] == (
        f"next: /merge {SLUG}")
    assert harness.main_subjects() == ["init"]


# ── the commit message, on its own ───────────────────────────────────────

def test_the_trailers_are_the_last_lines_and_review_seq_wins():
    text = merge_commit_message(slug=SLUG, author="plink", tier=2,
                                channel_id=4, seq=2704, pass_seq=2710)
    assert text == (f"merge: {SLUG} (/merge by plink, tier 2)\n"
                    "\n"
                    "merge-seq: 4:2704\n"
                    "review-seq: 2710\n")


# ── boot validation ──────────────────────────────────────────────────────

def fresh(harness, **build_cfg) -> Broker:
    config = {**harness.broker.config,
              "build": {**harness.broker.config["build"], **build_cfg}}
    return Broker(config, str(harness.verbs_path), transport=lambda cfg, b: {})


@pytest.mark.parametrize("cfg,fragment", [
    ({"daily_build_cap": 0}, "build.daily_build_cap"),
    ({"daily_build_cap": "four"}, "build.daily_build_cap"),
    ({"gate_timeout_sec": -1}, "build.gate_timeout_sec"),
    ({"gate_log_dir": "gate-logs"}, "build.gate_log_dir"),
    ({"merge_work_dir": ""}, "build.merge_work_dir"),
])
def test_a_bad_gate_or_merge_knob_refuses_to_start(harness, cfg, fragment):
    with pytest.raises(ConfigError) as ei:
        fresh(harness, **cfg)
    assert fragment in str(ei.value)


def test_the_defaults_are_the_shipped_ones(harness):
    config = {k: v for k, v in harness.broker.config.items()}
    config["build"] = {"humans": ["plink"], "seat": "test",
                       "ledger": harness.broker.build_ledger}
    broker = Broker(config, str(harness.verbs_path),
                    transport=lambda cfg, b: {})
    assert broker.chat_build_cap == 4
    assert broker.gate_timeout == 900
    assert broker.gate_log_dir == "/var/lib/disjorn-broker/gate-logs"
    assert broker.merge_work_dir == "/var/lib/disjorn-broker/merge-work"
