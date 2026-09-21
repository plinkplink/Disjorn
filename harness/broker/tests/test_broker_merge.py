"""The `merge` verb: a human's `/merge`, the broker's own gates, one commit on
the gatehouse's main."""

from __future__ import annotations

import datetime as dt
import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import broker_testlib as T  # noqa: E402
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
        gate_exit: int = 0, message: bool = True, on_gates=None):
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
                        summary="server 12 passed; harness 4 passed",
                        on_run=on_gates)


def call(h, *, slug: str = SLUG, seq: int = SEQ, channel: int = CHANNEL,
         pass_seq: int | None = None):
    args = {"seq": seq, "channel_id": channel, "slug": slug}
    if pass_seq is not None:
        args["pass_seq"] = pass_seq
    return h.call("merge", args)


def merge(h, **kw) -> list[str]:
    """The whole async `/merge`: the acknowledgement, the thread, the one post
    the room is left with."""
    resp = call(h, **kw)
    assert resp["ok"] is True, resp
    assert resp["result"] == {"started": True, "slug": kw.get("slug", SLUG),
                              "branch": f"loop/{kw.get('slug', SLUG)}"}
    h.finish_merges()
    return h.merge_outcomes()[-1]


def refusal(resp) -> tuple[str, str]:
    return resp["error"]["reason"], resp["error"]["message"]


def late_refusal(h, **kw) -> tuple[str, str, list[str]]:
    """A `/merge` that is taken and then refused: (reason, message, post)."""
    assert call(h, **kw)["ok"] is True
    h.finish_merges()
    reason, message = h.merge_denials()[-1]
    return reason, message, h.merge_outcomes()[-1]


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
    assert merge(harness)[0].startswith(f"merge: merged {SLUG} as ")


def test_the_call_returns_before_the_gates_answer(harness):
    """`/merge` has `/build`'s shape: the socket gets an acknowledgement and the
    room gets the outcome."""
    started = threading.Event()
    release = threading.Event()

    def wait():
        started.set()
        release.wait(timeout=10)

    arm(harness, tier=1, on_gates=wait)
    resp = call(harness)
    assert resp["result"]["started"] is True
    assert started.wait(timeout=10)
    assert harness.merge_outcomes() == []
    assert harness.main_subjects() == ["init"]
    release.set()
    harness.finish_merges()
    assert harness.merge_outcomes()[-1][0].startswith("merge: merged ")


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
    merge(harness)
    assert calls[0]["argv_prefix"] == ["sudo", "-n", "/l/launch", "gate"]
    assert calls[0]["seat"] == "test" and calls[0]["slug"] == SLUG
    assert calls[0]["timeout"] == 1320
    assert calls[0]["log_dir"].endswith("gate-logs")


def test_a_red_gate_is_tier_two_and_refuses_without_a_pass(harness):
    """The red gate is NOT special-cased: the classifier answers Tier 2 and the
    Tier 2 rule is what refuses."""
    arm(harness, gate_exit=1, tier=0)
    reason, message, post = late_refusal(harness)
    assert reason == "pass-missing"
    assert "Tier 2" in message and f"/merge {SLUG} pass <seq>" in message
    assert post[0] == f"merge: refused {SLUG} — {message}"
    assert post[1] == "next: fix the red gate, then /build again"
    assert harness.main_subjects() == ["init"]


# ── step 5: tier 0 and tier 1 merge on this call ─────────────────────────

@pytest.mark.parametrize("tier", [0, 1])
def test_a_tier_below_two_merges_and_stamps_merge_seq_last(harness, tier):
    arm(harness, tier=tier)
    lines = merge(harness)
    sha = harness.build_ledger_lines()[-1]["sha"]
    assert lines == [f"merge: merged {SLUG} as {sha} (tier {tier})",
                     "next: deploy at the keyboard"]
    assert harness.main_subjects()[0] == (
        f"merge: {SLUG} (/merge by plink, tier {tier})")
    body = harness.commit_message().strip().splitlines()
    assert body[-1] == f"merge-seq: {CHANNEL}:{SEQ}"
    assert "review-seq" not in harness.commit_message()


def test_the_merge_commit_is_a_no_ff_merge_by_the_broker(harness):
    arm(harness, tier=1)
    merge(harness)
    assert T._git(harness.gatehouse, "log", "-1", "--format=%an <%ae>",
                  "main").strip() == "disjorn-broker <broker@disjorn.local>"
    parents = T._git(harness.gatehouse, "log", "-1", "--format=%P",
                     "main").split()
    assert len(parents) == 2


def test_the_merge_line_lands_on_the_ledger(harness):
    arm(harness, tier=1)
    sha = merge(harness)[0].split(" as ")[1].split(" ")[0]
    line = harness.build_ledger_lines()[-1]
    assert line == {"ts": line["ts"], "kind": "merge", "seq": SEQ,
                    "channel_id": CHANNEL, "author": "plink", "slug": SLUG,
                    "tier": 1, "sha": sha, "pass_seq": None,
                    "self_merge": False}


def test_the_merge_is_audited_once_it_has_happened(harness):
    arm(harness, tier=1)
    merge(harness)
    sha = harness.build_ledger_lines()[-1]["sha"]
    done = [e for e in harness.audit_lines()
            if e["verb"] == "merge" and e["allowed"] is True]
    assert done[0]["result_summary"].startswith(f"merge {SLUG} started")
    assert done[-1]["merged_sha"] == sha and done[-1]["merge_tier"] == 1


def test_the_mirror_and_the_board_follow_the_merge(harness):
    arm(harness, tier=1)
    merge(harness)
    ran = [a for a in harness.recorded_argv() if "--ff-only" in a]
    assert ran, "the mirror was never fast-forwarded after the merge"


# ── step 5: tier 2 needs a reviewer's PASS ───────────────────────────────

def test_tier_two_without_a_pass_is_refused(harness):
    arm(harness, tier=2)
    reason, message, post = late_refusal(harness)
    assert reason == "pass-missing" and "#custodian" in message
    assert post[1] == (f"next: PASS from Claudette in #custodian, then "
                       f"/merge {SLUG} pass <seq>")
    assert harness.main_subjects() == ["init"]


def test_a_valid_pass_merges_with_review_seq_after_merge_seq(harness):
    arm(harness, tier=2)
    harness.add_custodian_post(PASS_SEQ, f"PASS {SLUG} — read it, it is fine",
                               author="Claudette", created_at=later())
    lines = merge(harness, pass_seq=PASS_SEQ)
    assert lines[0].endswith("(tier 2)")
    body = harness.commit_message().strip().splitlines()
    assert body[0] == f"merge: {SLUG} (/merge by plink, tier 2)"
    assert body[-2] == f"merge-seq: {CHANNEL}:{SEQ}"
    assert body[-1] == f"review-seq: {PASS_SEQ}"
    assert harness.build_ledger_lines()[-1]["pass_seq"] == PASS_SEQ


def test_a_pass_from_the_wrong_reviewer_is_refused(harness):
    arm(harness, tier=2)
    harness.add_custodian_post(PASS_SEQ, f"PASS {SLUG}", author="Gable",
                               created_at=later())
    reason, message, _post = late_refusal(harness, pass_seq=PASS_SEQ)
    assert reason == "pass-invalid"
    assert "Gable's" in message and "Claudette" in message
    assert harness.main_subjects() == ["init"]


def test_a_pass_from_a_person_is_not_a_reviewers_post(harness):
    arm(harness, tier=2)
    harness.add_custodian_post(PASS_SEQ, f"PASS {SLUG}", author="plink",
                               author_type="user", created_at=later())
    reason, message, _post = late_refusal(harness, pass_seq=PASS_SEQ)
    assert reason == "pass-invalid" and "reviewer's post" in message


def test_a_post_that_never_says_pass_is_refused(harness):
    arm(harness, tier=2)
    harness.add_custodian_post(PASS_SEQ, f"{SLUG} looks fine to me",
                               author="Claudette", created_at=later())
    reason, message, _post = late_refusal(harness, pass_seq=PASS_SEQ)
    assert reason == "pass-invalid" and "does not say PASS" in message


def test_a_pass_that_also_blocks_is_not_a_pass(harness):
    """A mixed verdict is a BLOCK: the reviewer said not to merge this."""
    arm(harness, tier=2)
    harness.add_custodian_post(
        PASS_SEQ, f"PASS on the docs, BLOCK on the schema change in {SLUG}",
        author="Claudette", created_at=later())
    reason, message, _post = late_refusal(harness, pass_seq=PASS_SEQ)
    assert reason == "pass-invalid"
    assert message == (f"seq {PASS_SEQ} says BLOCK as well as PASS, so it is "
                       "not a PASS")
    assert harness.main_subjects() == ["init"]


def test_a_lowercase_block_does_not_unmake_a_pass(harness):
    arm(harness, tier=2)
    harness.add_custodian_post(PASS_SEQ, f"PASS {SLUG}; nothing blocks it",
                               author="Claudette", created_at=later())
    assert merge(harness, pass_seq=PASS_SEQ)[0].startswith("merge: merged ")


def test_a_pass_that_names_another_slug_is_refused(harness):
    arm(harness, tier=2)
    harness.add_custodian_post(PASS_SEQ, "PASS 2026-09-20-something-else",
                               author="Claudette", created_at=later())
    assert late_refusal(harness, pass_seq=PASS_SEQ)[0] == "pass-invalid"


def test_a_pass_posted_before_the_tip_is_refused(harness):
    """A reviewer cannot have read a commit that did not exist yet."""
    arm(harness, tier=2)
    harness.add_custodian_post(PASS_SEQ, f"PASS {SLUG}", author="Claudette",
                               created_at=earlier())
    reason, message, _post = late_refusal(harness, pass_seq=PASS_SEQ)
    assert reason == "pass-invalid" and "before the tip" in message


def test_a_pass_in_another_channel_is_refused(harness):
    arm(harness, tier=2)
    harness.add_custodian_post(PASS_SEQ, f"PASS {SLUG}", author="Claudette",
                               created_at=later(), channel_id=CHANNEL + 50)
    reason, message, _post = late_refusal(harness, pass_seq=PASS_SEQ)
    assert reason == "pass-invalid"
    assert f"no message {PASS_SEQ} in #custodian" in message


def test_a_changed_path_with_no_lane_owner_is_a_keyboard_merge(harness):
    arm(harness, tier=2, path="harness/broker/brokerd.py")
    harness.add_custodian_post(PASS_SEQ, f"PASS {SLUG}", author="Claudette",
                               created_at=later())
    reason, message, post = late_refusal(harness, pass_seq=PASS_SEQ)
    assert reason == "pass-invalid"
    assert message == ("no lane owner for harness/broker/brokerd.py; "
                       "keyboard merge")
    assert post[1] == "next: merge it at the keyboard"


def test_a_pass_is_ignored_below_tier_two(harness):
    arm(harness, tier=1)
    harness.add_custodian_post(PASS_SEQ, "nothing to do with it",
                               author="Gable", created_at=earlier())
    assert merge(harness, pass_seq=PASS_SEQ)[0].startswith("merge: merged ")
    assert "review-seq" not in harness.commit_message()


# ── step 7: the merge itself ─────────────────────────────────────────────

def test_a_conflict_refuses_and_moves_nothing(harness):
    """Main can only conflict with a branch that contained it a moment ago, so
    the conflicting commit has to land while the gates run."""
    arm(harness, tier=1, path="docs/a.md",
        on_gates=lambda: harness.move_main())
    reason, message, post = late_refusal(harness)
    assert reason == "conflict" and "nothing moved" in message
    assert post[1] == "next: fold main into the branch, then /merge again"
    assert harness.main_subjects() == ["main moves", "init"]


def test_the_merge_workspace_is_thrown_away(harness):
    arm(harness, tier=1)
    merge(harness)
    work = Path(harness.broker.merge_work_dir)
    assert list(work.iterdir()) == []


# ── the message, the ancestry and the one run per slug ───────────────────

def test_a_message_that_does_not_name_the_slug_is_refused(harness):
    calls = arm(harness, content="/merge 2026-09-20-some-other-branch")
    reason, message = refusal(call(harness))
    assert reason == "slug-mismatch"
    assert message == f"message {SEQ} does not ask to merge {SLUG}"
    assert calls == [] and harness.main_subjects() == ["init"]


@pytest.mark.parametrize("content", [
    f"/merge {SLUG}", f"/merge loop/{SLUG}", f"/merge `{SLUG}` please",
])
def test_the_slug_is_read_as_a_whole_token(harness, content):
    arm(harness, content=content)
    assert call(harness)["ok"] is True
    harness.finish_merges()


def fold_lines(h) -> list[dict]:
    return [ln for ln in h.build_ledger_lines() if ln.get("kind") == "fold"]


def test_a_branch_behind_main_is_folded_and_gated_at_the_folded_tip(harness):
    """The branch was cut before main advanced, so the broker folds main in
    first — and what the gates run against is the tip the fold made."""
    gated: list[str] = []
    arm(harness, tier=1,
        on_gates=lambda: gated.append(harness.branch_tip(SLUG)))
    old_tip = harness.branch_tip(SLUG)
    harness.move_main(path="docs/elsewhere.md")
    old_main = harness.sha("refs/heads/main")

    lines = merge(harness)
    folded = harness.branch_tip(SLUG)
    assert gated == [folded] and folded != old_tip
    sha = harness.build_ledger_lines()[-1]["sha"]
    assert lines == [
        f"merge: merged {SLUG} as {sha} after folding main (tier 1)",
        "next: deploy at the keyboard"]
    assert T._git(harness.gatehouse, "log", "-1", "--format=%s%n%an <%ae>",
                  folded).splitlines() == [
        f"fold main into loop/{SLUG} (broker, before the gates)",
        "disjorn-broker <broker@disjorn.local>"]
    assert T._git(harness.gatehouse, "log", "-1", "--format=%P",
                  folded).split() == [old_tip, old_main]
    history = T._git(harness.gatehouse, "log", "--format=%H", "main").split()
    assert {old_tip, old_main, folded} <= set(history)


def test_a_branch_that_conflicts_with_main_moves_nothing(harness):
    """A fold that cannot happen is a posted refusal, like every other one
    the gates' side of the verb reaches."""
    calls = arm(harness, tier=1, path="docs/a.md")
    old_tip = harness.branch_tip(SLUG)
    harness.move_main()
    before = harness.main_subjects()

    reason, message, post = late_refusal(harness)
    assert reason == "moved"
    assert message == (f"loop/{SLUG} conflicts with main; fold it at the "
                       "keyboard")
    assert post[0] == f"merge: refused {SLUG} — {message}"
    assert post[1] == "next: fold main into the branch, then /merge again"
    assert calls == [] and fold_lines(harness) == []
    assert harness.main_subjects() == before
    assert harness.branch_tip(SLUG) == old_tip


def test_the_call_is_acknowledged_before_any_branch_work(harness):
    """The socket thread does no git on the branch: the fold runs in the
    background, like the gates."""
    reached, release = threading.Event(), threading.Event()
    real = harness.broker._fold_main_into

    def blocking(slug: str):
        reached.set()
        release.wait(timeout=10)
        return real(slug)

    arm(harness, tier=1)
    harness.broker._fold_main_into = blocking
    harness.move_main(path="docs/elsewhere.md")
    old_tip = harness.branch_tip(SLUG)

    assert call(harness)["result"]["started"] is True
    assert reached.wait(timeout=10)
    assert harness.branch_tip(SLUG) == old_tip
    release.set()
    harness.finish_merges()
    assert harness.merge_outcomes()[-1][0].startswith("merge: merged ")


def test_a_commit_pushed_while_the_gates_run_refuses_the_merge(harness):
    """The merge is pinned to the gated tip: a commit that lands after the
    gates is a tree nobody ran them against."""
    arm(harness, tier=1, on_gates=lambda: harness.commit_on_branch(SLUG))
    reason, message, post = late_refusal(harness)
    assert reason == "moved"
    assert message == f"loop/{SLUG} moved while the gates ran; /merge again"
    assert post[0] == f"merge: refused {SLUG} — {message}"
    assert harness.main_subjects() == ["init"]


@pytest.mark.parametrize("behind", [False, True])
def test_a_branch_with_nothing_of_its_own_is_refused(harness, behind):
    arm(harness, tier=1, branch=False)
    harness.push_empty_branch(SLUG)
    if behind:
        harness.move_main(path="docs/elsewhere.md")
    reason, message, post = late_refusal(harness)
    assert reason == "branch-missing"
    assert message == f"loop/{SLUG} has no commits of its own to merge"
    assert post[1] == "next: merge it at the keyboard"
    assert [s for s in harness.main_subjects() if s.startswith("merge: ")] == []


def test_a_pass_posted_before_the_fold_no_longer_holds(harness):
    """The fold makes a new tip, so a PASS read against the old one is a PASS
    for a tree that is not the one being merged."""
    arm(harness, tier=2)
    harness.move_main(path="docs/elsewhere.md")
    harness.add_custodian_post(PASS_SEQ, f"PASS {SLUG}", author="Claudette",
                               created_at=later(0))
    # A commit time has one-second resolution; the fold has to land in a later
    # second than the post or the comparison proves nothing.
    time.sleep(1.1)
    reason, message, _post = late_refusal(harness, pass_seq=PASS_SEQ)
    folded = harness.branch_tip(SLUG)
    assert reason == "pass-invalid"
    assert message == (f"loop/{SLUG} was folded onto main as {folded[:7]}, so "
                       f"PASS {PASS_SEQ} no longer names the gated tree; ask "
                       "for a fresh PASS")
    assert fold_lines(harness)[-1]["to"] == folded
    assert harness.main_subjects() == ["main moves", "init"]


def test_the_fold_lands_on_the_ledger_and_the_verbs_audit_line(harness):
    arm(harness, tier=1)
    old_tip = harness.branch_tip(SLUG)
    harness.move_main(path="docs/elsewhere.md")
    old_main = harness.sha("refs/heads/main")

    assert call(harness)["ok"] is True
    harness.finish_merges()
    folded = harness.branch_tip(SLUG)
    line = fold_lines(harness)[-1]
    assert line == {"ts": line["ts"], "kind": "fold", "slug": SLUG,
                    "from": old_tip, "to": folded, "main": old_main}
    done = [e for e in harness.audit_lines()
            if e["verb"] == "merge" and e.get("merged_sha")]
    assert done[-1]["folded"] == folded


def test_a_merge_that_folded_nothing_says_nothing_about_folding(harness):
    arm(harness, tier=1)
    assert merge(harness)[0] == (
        f"merge: merged {SLUG} as {harness.sha()} (tier 1)")
    assert fold_lines(harness) == []
    assert "folded" not in [k for e in harness.audit_lines() for k in e]


def test_main_moving_while_the_gates_run_refuses_the_push(harness):
    arm(harness, tier=1,
        on_gates=lambda: harness.move_main(path="docs/elsewhere.md"))
    reason, message, post = late_refusal(harness)
    assert reason == "moved"
    assert message == "main moved while the gates ran; /merge again"
    assert post[1] == "next: fold main into the branch, then /merge again"
    assert [s for s in harness.main_subjects() if s.startswith("merge: ")] == []


def test_a_second_merge_while_the_gates_run_is_refused(harness):
    seen: list = []
    arm(harness, tier=1, on_gates=lambda: seen.append(call(harness)))
    assert merge(harness)[0].startswith("merge: merged ")
    reason, message = refusal(seen[0])
    assert reason == "busy"
    assert message == f"a gate run for {SLUG} is already in flight"


def test_a_merge_while_a_build_end_gate_run_is_up_touches_nothing(harness):
    """The claim is taken before any git runs for the slug, so the refused
    `/merge` cannot fold a branch the build end is already working on."""
    seen: list = []
    tips: list = []

    def probe() -> None:
        tips.append(harness.branch_tip(SLUG))
        seen.append(call(harness))
        tips.append(harness.branch_tip(SLUG))

    arm(harness, tier=1, on_gates=probe)
    harness.move_main(path="docs/elsewhere.md")
    outcome(harness)
    assert refusal(seen[0]) == (
        "busy", f"a gate run for {SLUG} is already in flight")
    assert tips[0] == tips[1] and len(fold_lines(harness)) == 1


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
    assert outcome(harness)[3].startswith("next: merged ")
    # The first merge moved main, so the second branch is cut from where main
    # is now — a branch behind main never reaches the budget at all.
    harness.push_branch("2026-09-20-second-typo", path="docs/second.md")
    lines = outcome(harness, slug="2026-09-20-second-typo")
    assert lines[3] == "next: Tier 0 budget spent today; /merge 2026-09-20-second-typo"
    merges = [x for x in harness.main_subjects() if x.startswith("merge: ")]
    assert len(merges) == 1


def test_the_self_merge_says_so_on_the_ledger(harness):
    arm(harness, tier=0)
    outcome(harness)
    assert harness.build_ledger_lines()[-1]["self_merge"] is True


def test_human_merges_never_spend_the_tier_zero_budget(harness):
    """Only a self-merge is budgeted, so the day's human merges cannot use up
    a build's own."""
    arm(harness, tier=0)
    (Path(harness.broker._protected_paths())
     .write_text("[limits]\ndaily_auto_apply_budget = 1\n"))
    for n, slug in enumerate(("2026-09-20-one", "2026-09-20-two"), start=1):
        harness.push_branch(slug, path=f"docs/{slug}.md")
        harness.add_message(CHANNEL, SEQ + n, f"/merge {slug}")
        assert merge(harness, slug=slug, seq=SEQ + n)[0].startswith(
            "merge: merged ")
    harness.push_branch(SLUG, path="docs/new.md")
    assert outcome(harness)[3].startswith("next: merged ")


def test_a_human_merge_is_never_budgeted(harness):
    arm(harness, tier=0)
    (Path(harness.broker._protected_paths())
     .write_text("[limits]\ndaily_auto_apply_budget = 0\n"))
    assert merge(harness)[0].startswith("merge: merged ")


def test_a_build_whose_branch_fell_behind_main_folds_and_self_merges(harness):
    calls = arm(harness, tier=0)
    old_tip = harness.branch_tip(SLUG)
    harness.move_main(path="docs/elsewhere.md")

    lines = outcome(harness)
    folded = harness.branch_tip(SLUG)
    assert folded != old_tip and fold_lines(harness)[-1]["to"] == folded
    assert len(calls) == 1 and calls[0]["slug"] == SLUG
    assert lines[0] == "tests: pass — server 12 passed; harness 4 passed"
    assert lines[2].endswith(" (main folded in)")
    sha = harness.build_ledger_lines()[-1]["sha"]
    assert lines[3] == f"next: merged {sha}"
    assert harness.main_subjects()[0] == (
        f"merge: {SLUG} (/merge by plink, tier 0)")


def test_a_commit_pushed_while_a_builds_gates_run_stops_the_self_merge(harness):
    arm(harness, tier=0, on_gates=lambda: harness.commit_on_branch(SLUG))
    assert outcome(harness)[3] == f"next: /merge {SLUG}"
    assert harness.main_subjects() == ["init"]


def test_a_build_whose_branch_conflicts_with_main_gates_nothing(harness):
    calls = arm(harness, tier=0, path="docs/a.md")
    harness.move_main()
    lines = outcome(harness)
    assert calls == []
    assert lines[0] == "tests: n/a — nothing was gated"
    assert lines[3] == (f"next: loop/{SLUG} conflicts with main; fold it at "
                        f"the keyboard, then /merge {SLUG}")
    assert [s for s in harness.main_subjects() if s.startswith("merge: ")] == []


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
    ({"gate_timeout_sec": 900}, "build.gate_timeout_sec"),
    ({"gate_timeout_sec": 1200}, "build.gate_timeout_sec"),
    ({"gate_log_dir": "gate-logs"}, "build.gate_log_dir"),
    ({"merge_work_dir": ""}, "build.merge_work_dir"),
])
def test_a_bad_gate_or_merge_knob_refuses_to_start(harness, cfg, fragment):
    with pytest.raises(ConfigError) as ei:
        fresh(harness, **cfg)
    assert fragment in str(ei.value)


def test_a_gate_timeout_under_the_units_own_cap_says_why(harness):
    with pytest.raises(ConfigError) as ei:
        fresh(harness, gate_timeout_sec=1200)
    assert ("the gate unit's own runtime cap is 1200 seconds; the broker must "
            "outwait it") in str(ei.value)
    assert fresh(harness, gate_timeout_sec=1201).gate_timeout == 1201


def test_the_defaults_are_the_shipped_ones(harness):
    config = {k: v for k, v in harness.broker.config.items()}
    config["build"] = {"humans": ["plink"], "seat": "test",
                       "ledger": harness.broker.build_ledger}
    broker = Broker(config, str(harness.verbs_path),
                    transport=lambda cfg, b: {})
    assert broker.chat_build_cap == 4
    assert broker.gate_timeout == 1320
    assert broker.gate_log_dir == "/var/lib/disjorn-broker/gate-logs"
    assert broker.merge_work_dir == "/var/lib/disjorn-broker/merge-work"
