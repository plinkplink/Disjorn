"""Tests for the keyboard-lane GATE DRIFT block — the detector of record.

WHAT IS UNDER TEST. Real git repos and a real sqlite message store in tmp_path:
a mirror with the hook committed in it, a canonical git-dir with the hook
symlinked and a push log beside it, a prod tree, and a #custodian table with
resolvable and unresolvable seqs. The hook's own log grammar is exercised
end-to-end in harness/gatehouse/tests; here the log is written by hand, because
what these tests are about is what the DIGEST concludes from a log — including
the logs no healthy hook would ever write.

The four things worth stating up front, because each was argued for
specifically and each has a test that fails if it is quietly re-implemented:

  * CITATION COMES FROM PUSH TRUTH (G1/G1b). One trailer on the tip of a
    five-commit push cites all five; a later trailer-bearing push can never
    reach back and bless the ancestors of a fail-open push.
  * THE FLOOR'S PROVENANCE CHANGES WHAT SILENCE MEANS (G1d). Below a seeded
    floor is out of scope; below a lazy floor is unverifiable, and must never
    render as clean.
  * THE FLOOR-MOTION BASELINE LIVES OUTSIDE THE GIT-DIR. It is parsed back out
    of the digest's own previous post, so it survives the log being deleted and
    lazily re-born — the one case both in-log tamper tells miss.
  * A LOST LOG DEGRADES TO MORE FLAGS, NEVER FEWER.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import sqlite3
import subprocess
from pathlib import Path

import pytest

import metrics as M

DATE = "2026-08-20"
CUSTODIAN = 4
HOOK_SRC = (Path(__file__).resolve().parents[2]
            / "gatehouse" / "hooks" / "pre-receive-main-review")
PROTECTED = (Path(__file__).resolve().parents[2]
             / "classifier" / "protected-paths.toml")

# Commit dates are PINNED to the reported day. The digest's window falls back
# to `--since/--until` on that day when there is no previous post to measure
# from, so a suite whose commits carry the real wall-clock date would test the
# fallback against an empty window and prove nothing.
ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "keyboard", "GIT_AUTHOR_EMAIL": "plink@example.invalid",
    "GIT_COMMITTER_NAME": "keyboard", "GIT_COMMITTER_EMAIL": "plink@example.invalid",
    "GIT_AUTHOR_DATE": f"{DATE}T09:00:00Z", "GIT_COMMITTER_DATE": f"{DATE}T09:00:00Z",
    "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null",
}


def git(cwd, *args, env=None, check=True) -> str:
    proc = subprocess.run(["git", *args], cwd=str(cwd), env=env or ENV,
                          capture_output=True, text=True)
    if check and proc.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed:\n{proc.stderr}")
    return proc.stdout.strip()


# --------------------------------------------------------------------------
# The lane: mirror + canonical git-dir + prod tree + message store.
# --------------------------------------------------------------------------

SCHEMA = """
create table messages (
  id integer primary key autoincrement, channel_id integer not null,
  seq integer not null, author_type text not null, author_id integer not null,
  content text not null, created_at text not null default '',
  edited_at text, deleted_at text);
create table bots (id integer primary key autoincrement, name text not null unique,
  api_key_hash text);
create table users (id integer primary key autoincrement,
  username text not null unique, display_name text not null);
"""


class Lane:
    def __init__(self, root: Path):
        self.root = root
        self.mirror = root / "mirror"
        self.canonical = root / "disjorn.git"
        self.deployed_dir = root / "hooks-deployed"
        self.deployed = self.deployed_dir / "pre-receive-main-review"
        self.prod = root / "prod"

        # -- the mirror, with the hook committed in it (the liveness baseline)
        self.mirror.mkdir(parents=True)
        git(self.mirror, "init", "-q", "-b", "main")
        hook_dst = self.mirror / "harness" / "gatehouse" / "hooks"
        hook_dst.mkdir(parents=True)
        shutil.copy2(HOOK_SRC, hook_dst / "pre-receive-main-review")
        (self.mirror / "README.md").write_text("start\n")
        git(self.mirror, "add", "-A")
        # Dated the day BEFORE, so it sits outside the reported day's window
        # the way real history does. Tests that care about it reach for it by
        # name (the floor tests) rather than finding it in every window.
        old_day = {**ENV, "GIT_AUTHOR_DATE": "2026-08-19T09:00:00Z",
                   "GIT_COMMITTER_DATE": "2026-08-19T09:00:00Z"}
        git(self.mirror, "commit", "-q", "-m", "initial commit", env=old_day)

        # -- the canonical git-dir: hooks/ with a symlink to the deployed copy
        (self.canonical / "hooks").mkdir(parents=True)
        self.deployed_dir.mkdir()
        shutil.copy2(HOOK_SRC, self.deployed)
        (self.canonical / "hooks" / "pre-receive").symlink_to(self.deployed)

        # -- the broker's posting key: bot 3 is the digest's own identity.
        # The hash scheme is pinned to the server's (auth.py hash_api_key:
        # sha256 hexdigest) — the same coupling metrics.py declares.
        self.key_path = root / "broker-api-key"
        self.key_path.write_text("test-broker-key\n", encoding="utf-8")
        broker_key_hash = hashlib.sha256(b"test-broker-key").hexdigest()

        # -- the message store
        self.db_path = root / "disjorn.db"
        db = sqlite3.connect(self.db_path)
        db.executescript(SCHEMA)
        db.executemany("insert into bots (id, name, api_key_hash) values (?, ?, ?)",
                       [(1, "Claudette", None), (2, "Gable", None),
                        (3, "broker", broker_key_hash), (5, "keyboard", None)])
        db.execute("insert into users (id, username, display_name) "
                   "values (1, 'plink', 'plink')")
        db.commit()
        db.close()
        self.post(1428, CUSTODIAN, "bot", 1, "review: the gate spec reads clean")
        self.post(1440, CUSTODIAN, "bot", 2, "review: Gable reviewing")
        self.post(1450, CUSTODIAN, "user", 1,
                  "keyboard: override-merge plan-room-phase0 — reviewers are down")
        self.post(9999, 7, "bot", 1, "not in #custodian")

    # -- message store ------------------------------------------------------

    def post(self, seq, channel, author_type, author_id, content,
             deleted=None) -> None:
        db = sqlite3.connect(self.db_path)
        db.execute("insert into messages (channel_id, seq, author_type, "
                   "author_id, content, created_at, deleted_at) "
                   "values (?,?,?,?,?,?,?)",
                   (channel, seq, author_type, author_id, content,
                    f"{DATE}T09:00:00Z", deleted))
        db.commit()
        db.close()

    # -- the mirror ---------------------------------------------------------

    def commit(self, rel: str, text: str, message: str, who="keyboard") -> str:
        p = self.mirror / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
        env = dict(ENV)
        env.update(GIT_AUTHOR_NAME=who, GIT_COMMITTER_NAME=who,
                   GIT_AUTHOR_EMAIL=f"{who.lower()}@example.invalid",
                   GIT_COMMITTER_EMAIL=f"{who.lower()}@example.invalid")
        git(self.mirror, "add", rel, env=env)
        git(self.mirror, "commit", "-q", "-m", message, env=env)
        return git(self.mirror, "rev-parse", "HEAD", env=env)

    def head(self) -> str:
        return git(self.mirror, "rev-parse", "main")

    # -- the push log -------------------------------------------------------

    @property
    def log_path(self) -> Path:
        return self.canonical / "hooks" / "disjorn-push-log"

    def write_log(self, *lines: str) -> None:
        self.log_path.write_text("".join(ln + "\n" for ln in lines),
                                 encoding="utf-8")

    def genesis(self, kind: str, sha: str, ts=f"{DATE}T08:00:00Z") -> str:
        return f"GENESIS {kind} {ts} {sha}"

    def push(self, old: str, new: str, trailer="NONE", outcome="passed",
             ts=f"{DATE}T09:30:00Z") -> str:
        return f"PUSH {ts} {old}..{new} {trailer} {outcome}"

    # -- prod ---------------------------------------------------------------

    def deploy(self, ref: str = "main") -> None:
        if not self.prod.exists():
            git(self.root, "clone", "-q", str(self.mirror), str(self.prod))
        git(self.prod, "fetch", "-q", "origin")
        git(self.prod, "checkout", "-q", "--detach", ref)

    # -- config -------------------------------------------------------------

    def config(self, **over) -> dict:
        gate = {"canonical_repo": str(self.canonical), "mirror": str(self.mirror),
                "deploy_tree": str(self.prod), "message_db": str(self.db_path)}
        gate.update(over)
        return {"gate": gate,
                "disjorn": {"custodian_channel_id": CUSTODIAN,
                            "api_key_path": str(self.key_path)},
                "paths": {"protected_paths": str(PROTECTED)},
                "residents": {}}

    def drift(self, **over) -> dict:
        return M.gate_drift(self.config(**over), date=DATE)

    def block(self, **over) -> str:
        return M.compose_drift_block(self.drift(**over))


@pytest.fixture()
def lane(tmp_path) -> Lane:
    return Lane(tmp_path)


# --------------------------------------------------------------------------
# Liveness — line 1 (G3).
# --------------------------------------------------------------------------

def test_hook_matches_the_mirror_when_properly_installed(lane):
    live = M.hook_liveness(M.gate_paths(lane.config()))
    assert live["state"] == "MATCH"
    assert live["deployed_sha"] == live["mirror_sha"]
    assert live["target"] == str(lane.deployed)
    assert "MATCH" in lane.block()


def test_a_stale_deployed_copy_is_a_mismatch(lane):
    """Committed is not installed. This is the line that says so."""
    lane.deployed.write_text("#!/bin/sh\nexit 0\n")
    live = M.hook_liveness(M.gate_paths(lane.config()))
    assert live["state"] == "MISMATCH"
    assert "NOT the committed one" in live["detail"]
    assert "MISMATCH" in lane.block()


def test_a_missing_symlink_is_absent(lane):
    (lane.canonical / "hooks" / "pre-receive").unlink()
    assert M.hook_liveness(M.gate_paths(lane.config()))["state"] == "ABSENT"
    assert "hook: ABSENT" in lane.block()


def test_a_dangling_symlink_is_absent(lane):
    """The G4 failure mode: the deployed copy vanishes and the link stays."""
    lane.deployed.unlink()
    assert M.hook_liveness(M.gate_paths(lane.config()))["state"] == "ABSENT"
    assert "NOT installed" in lane.block()


def test_the_liveness_line_comes_first(lane):
    lines = lane.block().splitlines()
    assert lines[0].startswith(M.DRIFT_HEADER)
    assert lines[1].startswith("hook:")
    assert lines[2].startswith("push log:")
    assert lines[3].startswith("floor:")


# --------------------------------------------------------------------------
# The genesis floor and its provenance — line 2 (G1c/G1d).
# --------------------------------------------------------------------------

def test_no_log_is_reported_as_no_log(lane):
    g = M.genesis_state(M.parse_push_log(str(lane.log_path)))
    assert g["state"] == "NO LOG"
    assert "push log: NO LOG" in lane.block()


def test_a_seeded_floor_reads_as_a_plain_state(lane):
    lane.write_log(lane.genesis("seeded", lane.head()))
    g = M.genesis_state(M.parse_push_log(str(lane.log_path)))
    assert (g["state"], g["floor"]) == ("seeded", lane.head())
    line = [l for l in lane.block().splitlines() if l.startswith("push log:")][0]
    assert "genesis seeded" in line
    assert "unverifiable" not in line


def test_a_lazy_floor_reads_as_a_warning_never_a_plain_state(lane):
    """G1d: the two births must not read alike."""
    base = lane.head()
    lane.commit("harness/x.py", "x = 1\n", "harness: x")
    lane.write_log(lane.genesis("lazy", base))
    line = [l for l in lane.block().splitlines() if l.startswith("push log:")][0]
    assert "LAZY" in line and "warning" in line
    assert f"commits before {base[:8]} unverifiable" in line


def test_a_truncated_log_is_not_a_young_one(lane):
    lane.write_log(lane.push("a" * 40, lane.head()))
    g = M.genesis_state(M.parse_push_log(str(lane.log_path)))
    assert g["state"] == "TRUNCATED"
    assert "TRUNCATED" in lane.block()


def test_a_second_genesis_line_means_deleted_and_recreated(lane):
    lane.write_log(lane.genesis("seeded", "a" * 40),
                   lane.genesis("lazy", "b" * 40))
    g = M.genesis_state(M.parse_push_log(str(lane.log_path)))
    assert g["state"] == "REPLACED"
    assert "deleted and recreated" in g["detail"]
    assert "REPLACED" in lane.block()


# --------------------------------------------------------------------------
# Floor motion — line 3, the tell that survives losing the log.
# --------------------------------------------------------------------------

def test_the_first_digest_has_no_baseline_and_says_so(lane):
    lane.write_log(lane.genesis("seeded", lane.head()))
    d = lane.drift()
    assert d["previous"] is None and d["floor_moved"] is False
    assert "no baseline yet" in lane.block()


def test_an_unchanged_floor_is_reported_as_unchanged(lane):
    floor = lane.head()
    lane.write_log(lane.genesis("seeded", floor))
    lane.post(2000, CUSTODIAN, "bot", 3,
              f"[custodian daily] x\n\n{M.DRIFT_HEADER} — keyboard lane\n"
              f"floor: {floor}\nmirror head: {floor}")
    assert lane.drift()["floor_moved"] is False
    assert "unchanged since the previous digest" in lane.block()


def test_a_moved_floor_is_the_loudest_line(lane):
    lane.write_log(lane.genesis("seeded", lane.head()))
    lane.post(2000, CUSTODIAN, "bot", 3,
              f"{M.DRIFT_HEADER} — keyboard lane\n"
              f"floor: {'c' * 40}\nmirror head: {'c' * 40}")
    d = lane.drift()
    assert d["floor_moved"] is True
    block = lane.block()
    assert "FLOOR MOVED" in block
    assert "Floors do not move" in block


def test_a_log_deleted_whole_and_relost_still_trips_floor_motion(lane):
    """The case both in-log tamper tells miss: the log is gone, so there is no
    second genesis line and no truncation to see. The baseline lives in the
    message store, outside the git-dir, so this still fires."""
    lane.post(2000, CUSTODIAN, "bot", 3,
              f"{M.DRIFT_HEADER} — keyboard lane\n"
              f"floor: {'d' * 40}\nmirror head: {'d' * 40}")
    d = lane.drift()  # no log at all
    assert d["genesis"]["state"] == "NO LOG"
    assert d["floor_moved"] is True
    assert "FLOOR MOVED" in lane.block()


def test_the_block_round_trips_its_own_floor_line(lane):
    """The floor-motion check reads the digest's own text back. If the block's
    wording drifts from the parser, the baseline silently disappears and every
    later digest reports 'no baseline yet' forever."""
    floor = lane.head()
    lane.write_log(lane.genesis("seeded", floor))
    body = M.compose_daily_line({}, lane.config(), DATE, drift=lane.drift())
    lane.post(2000, CUSTODIAN, "bot", 3, body)
    prev = M.previous_digest(M.gate_paths(lane.config()))
    assert prev["floor"] == floor
    assert prev["mirror_head"] == lane.head()


def test_no_one_else_can_write_the_baseline(lane):
    """The baseline moved into the message store to survive a log delete —
    but the store is a CHANNEL, writable by everyone. Without the author
    filter, anyone quoting a drift block (verbatim, floor line and all)
    becomes the baseline: chat as detector input, the G1d hole one layer
    out."""
    lane.write_log(lane.genesis("seeded", lane.head()))
    quote = (f"{M.DRIFT_HEADER} — keyboard lane\n"
             f"floor: {'c' * 40}\nmirror head: {'c' * 40}")
    lane.post(2000, CUSTODIAN, "bot", 1, quote)    # a resident
    lane.post(2001, CUSTODIAN, "user", 1, quote)   # a human
    d = lane.drift()
    assert d["previous"] is None
    assert d["floor_moved"] is False
    assert "no baseline yet" in lane.block()


def test_the_brokers_own_build_banner_is_not_the_baseline(lane):
    """The author filter alone cannot carry this: build banners post under
    the SAME bot identity as the digest, and the gate build's own banner
    mentions the header mid-sentence. On day one that banner is the newest
    match — without the block-form rule the first live digest opens with
    FLOOR MOVED against a banner."""
    lane.write_log(lane.genesis("seeded", lane.head()))
    lane.post(2000, CUSTODIAN, "bot", 3,
              f"build done | gate -> loop/gate | diff: hook plus the "
              f"digest's {M.DRIFT_HEADER} block (liveness, floor motion)")
    d = lane.drift()
    assert d["previous"] is None
    assert d["floor_moved"] is False
    assert "no baseline yet" in lane.block()


def test_a_floorless_own_block_is_skipped_for_an_older_true_baseline(lane):
    """A drift block with no parseable floor line (DETECTOR NOT CONFIGURED,
    a mangled post) is no-baseline, never motion — and the scan continues to
    the next-older candidate, because floors never legitimately move, so an
    older true baseline still detects a replaced log."""
    floor = lane.head()
    lane.write_log(lane.genesis("seeded", floor))
    lane.post(2000, CUSTODIAN, "bot", 3,
              f"[custodian daily] x\n\n{M.DRIFT_HEADER} — keyboard lane\n"
              f"floor: {floor}\nmirror head: {floor}")
    lane.post(2100, CUSTODIAN, "bot", 3,
              f"{M.DRIFT_HEADER} — keyboard lane\n"
              f"DETECTOR NOT CONFIGURED: broker.toml has no [gate] block")
    d = lane.drift()
    assert d["previous"] and d["previous"]["seq"] == 2000
    assert d["floor_moved"] is False


def test_an_unresolvable_identity_is_a_detector_fault_not_a_first_run(lane):
    """If the digest cannot resolve its OWN posting identity, widening the
    query would hand the baseline to anyone in the channel, and quietly
    reporting 'no baseline yet' would retire the floor-motion check forever
    behind a benign line. It renders as a fault, and floor motion is
    UNCHECKED, said out loud."""
    floor = lane.head()
    lane.write_log(lane.genesis("seeded", floor))
    lane.post(2000, CUSTODIAN, "bot", 3,
              f"{M.DRIFT_HEADER} — keyboard lane\n"
              f"floor: {floor}\nmirror head: {floor}")
    cfg = lane.config()
    cfg["disjorn"]["api_key_path"] = str(lane.root / "no-such-key")
    d = M.gate_drift(cfg, date=DATE)
    assert d["previous"] is None and d["floor_moved"] is False
    assert d["baseline_error"]
    block = M.compose_drift_block(d)
    assert "BASELINE UNAVAILABLE" in block
    assert "no baseline yet" not in block


def test_a_key_no_bot_owns_is_the_same_detector_fault(lane):
    lane.write_log(lane.genesis("seeded", lane.head()))
    lane.key_path.write_text("rotated-but-not-registered\n", encoding="utf-8")
    d = lane.drift()
    assert d["baseline_error"]
    assert "BASELINE UNAVAILABLE" in lane.block()


# --------------------------------------------------------------------------
# Citation from push truth (G1/G1b) and seq resolution (G2).
# --------------------------------------------------------------------------

def test_one_trailer_on_the_tip_cites_the_whole_push(lane):
    """A five-commit push is ONE cited range, never one pass and four false
    violations."""
    old = lane.head()
    for i in range(5):
        lane.commit(f"harness/f{i}.py", f"x = {i}\n", f"harness: step {i}")
    new = lane.head()
    lane.write_log(lane.genesis("seeded", old),
                   lane.push(old, new, "review-seq:1428"))
    d = lane.drift()
    assert len(d["window"]) == 5
    assert d["uncited"] == []
    assert d["violations"] == []


def test_an_untrailered_push_leaves_its_commits_uncited(lane):
    old = lane.head()
    lane.commit("harness/metrics/x.py", "x = 1\n", "harness: no citation")
    lane.write_log(lane.genesis("seeded", old),
                   lane.push(old, lane.head(), "NONE", "failed-open"))
    d = lane.drift()
    assert len(d["uncited"]) == 1
    assert d["fail_open"] == 1
    assert "fail-open pushes in the log: 1" in lane.block()


def test_a_later_trailer_cannot_bless_an_earlier_fail_open(lane):
    """THE laundering hole, closed. The first push landed only because the hook
    failed open; the second push is properly cited. The first stays uncited."""
    floor = lane.head()
    laundered = lane.commit("server/app/ws.py", "x = 1\n", "server: sneaky")
    lane.commit("harness/honest.py", "y = 2\n",
                "harness: honest\n\nreview-seq: 1428")
    lane.write_log(lane.genesis("seeded", floor),
                   lane.push(floor, laundered, "NONE", "failed-open"),
                   lane.push(laundered, lane.head(), "review-seq:1428"))
    d = lane.drift()
    assert d["uncited"] == [laundered]
    assert [v["sha"] for v in d["violations"]] == [laundered]
    assert "LANE VIOLATION" in lane.block()


def test_an_uncited_tier_two_commit_is_named_as_a_lane_violation(lane):
    floor = lane.head()
    sha = lane.commit("server/app/ws.py", "x = 1\n", "server: fanout tweak")
    lane.write_log(lane.genesis("seeded", floor),
                   lane.push(floor, sha, "NONE", "failed-open"))
    block = lane.block()
    assert f"LANE VIOLATION: {sha[:8]}" in block
    assert "server: fanout tweak" in block
    assert "server/app/ws.py" in block


def test_a_doc_only_uncited_commit_is_not_a_lane_violation(lane):
    """The gate lets doc-only ranges through, so the detector must not then
    report them as violations — that would train the reader to ignore it."""
    floor = lane.head()
    sha = lane.commit("SPECS/2026-08-20-x.md", "a spec\n", "spec: x")
    lane.write_log(lane.genesis("seeded", floor),
                   lane.push(floor, sha, "NONE", "passed"))
    d = lane.drift()
    assert d["uncited"] == [sha]
    assert d["violations"] == []


def test_a_seq_that_does_not_resolve_does_not_cite(lane):
    """Without G2, `review-seq: 1` passes forever and the gate is a spelling
    test."""
    floor = lane.head()
    sha = lane.commit("harness/x.py", "x = 1\n", "harness: x\n\nreview-seq: 1")
    lane.write_log(lane.genesis("seeded", floor),
                   lane.push(floor, sha, "review-seq:1"))
    d = lane.drift()
    assert d["uncited"] == [sha]
    assert d["broken_citations"][0]["detail"] == "no such seq in the message store"
    assert "CITATION DOES NOT RESOLVE" in lane.block()


def test_a_seq_outside_custodian_does_not_cite(lane):
    floor = lane.head()
    sha = lane.commit("harness/x.py", "x = 1\n", "harness: x")
    lane.write_log(lane.genesis("seeded", floor),
                   lane.push(floor, sha, "review-seq:9999"))
    d = lane.drift()
    assert d["uncited"] == [sha]
    assert "not in #custodian" in d["broken_citations"][0]["detail"]


def test_a_review_cited_by_its_own_author_is_flagged_self_cited(lane):
    """The comfortable failure mode, named so it cannot pass as review."""
    floor = lane.head()
    sha = lane.commit("harness/x.py", "x = 1\n", "harness: x", who="Gable")
    lane.write_log(lane.genesis("seeded", floor),
                   lane.push(floor, sha, "review-seq:1440"))  # seq 1440 is Gable's
    d = lane.drift()
    assert len(d["self_cited"]) == 1
    assert d["uncited"] == [], "it is still cited — flagged, not voided"
    assert "SELF-CITED" in lane.block()
    assert "not a review" in lane.block()


def test_a_review_by_someone_else_is_not_self_cited(lane):
    floor = lane.head()
    sha = lane.commit("harness/x.py", "x = 1\n", "harness: x", who="Gable")
    lane.write_log(lane.genesis("seeded", floor),
                   lane.push(floor, sha, "review-seq:1428"))  # Claudette's
    assert lane.drift()["self_cited"] == []


def test_an_override_is_never_self_cited(lane):
    """An override-seq IS the pusher's own line by design. Flagging it would be
    noise, and noise is how a detector stops being read."""
    floor = lane.head()
    sha = lane.commit("harness/x.py", "x = 1\n", "harness: x")  # by keyboard
    lane.write_log(lane.genesis("seeded", floor),
                   lane.push(floor, sha, "override-seq:1450"))  # plink's own
    d = lane.drift()
    assert d["self_cited"] == []
    assert d["uncited"] == []


def test_identity_matching_knows_keyboard_is_plink(lane):
    aliases = M.gate_paths(lane.config())["author_aliases"]
    assert M.identity_matches("keyboard", "keyboard <plink@example.invalid>", aliases)
    assert M.identity_matches("plink", "keyboard <plink@example.invalid>", aliases)
    assert not M.identity_matches("Claudette", "Gable <gable@x>", aliases)
    assert not M.identity_matches(None, "Gable <gable@x>", aliases)


def test_a_deleted_message_does_not_resolve(lane):
    lane.post(1500, CUSTODIAN, "bot", 1, "gone", deleted=f"{DATE}T10:00:00Z")
    db = M._open_db(str(lane.db_path))
    assert M.resolve_seq(db, 1500, CUSTODIAN)["resolves"] is False
    db.close()


# --------------------------------------------------------------------------
# Uncovered commits — the fact that exists nowhere else (G1c).
# --------------------------------------------------------------------------

def test_a_commit_with_no_covering_log_line_is_uncovered(lane):
    """It entered main while the hook was absent or disarmed."""
    floor = lane.head()
    sha = lane.commit("harness/x.py", "x = 1\n", "harness: snuck in")
    lane.write_log(lane.genesis("seeded", floor))  # no push line at all
    d = lane.drift()
    assert d["uncovered"] == [sha]
    block = lane.block()
    assert f"UNCOVERED: {sha[:8]}" in block
    assert "absent or disarmed" in block


def test_below_a_seeded_floor_is_out_of_scope(lane):
    """The gate starts where the log starts: the first digest after install
    flags nothing historical, so the detector doesn't cry wolf on its breath."""
    for i in range(3):
        lane.commit(f"harness/old{i}.py", "x\n", f"harness: history {i}")
    lane.write_log(lane.genesis("seeded", lane.head()))
    d = lane.drift()
    assert d["uncovered"] == []
    assert d["unverifiable"] == 0
    assert "uncovered commits above the floor: 0" in lane.block()


def test_below_a_lazy_floor_is_unverifiable_not_clean(lane):
    """G1d. A hook disarmed between install and its first firing mints the
    floor on the far side of exactly the window this flag exists to catch."""
    for i in range(3):
        lane.commit(f"harness/old{i}.py", "x\n", f"harness: history {i}")
    lane.write_log(lane.genesis("lazy", lane.head()))
    d = lane.drift()
    assert d["unverifiable"] == 4  # three commits plus the initial one
    block = lane.block()
    assert "UNVERIFIABLE" in block
    assert "Not clean, just unknown" in block


def test_a_refused_push_is_not_permanent_mirror_drift_noise(lane):
    """A refusal is the hook doing its one job: nothing landed, so the range
    can never resolve in the mirror. Rev-listing it would increment 'N logged
    push ranges do not resolve — history was rewritten' on every digest
    forever, one legitimate refusal at a time — a counter that only goes up
    and never means anything gets muted in a week."""
    floor = lane.head()
    lane.write_log(lane.genesis("seeded", floor),
                   lane.push(floor, "e" * 40, "NONE", "refused"))
    d = lane.drift()
    assert d["unresolvable_ranges"] == 0
    assert "do not resolve" not in lane.block()


def test_commits_on_main_whose_only_log_line_is_a_refusal_are_uncovered(lane):
    """A refused line attests a refusal, not a landing. If the range's commits
    are on `main` anyway, they arrived by a path the hook never passed — the
    absent-or-disarmed case — and reading the refusal as coverage would
    render exactly that arrival as clean, forever, once it ages out of the
    digest window. (Before the review fix this asserted the opposite:
    refused ranges counted as covered.)"""
    floor = lane.head()
    sha = lane.commit("harness/x.py", "x = 1\n", "harness: x")
    lane.write_log(lane.genesis("seeded", floor),
                   lane.push(floor, sha, "NONE", "refused"))
    assert lane.drift()["uncovered"] == [sha]


# --------------------------------------------------------------------------
# A lost log degrades to MORE flags, never fewer.
# --------------------------------------------------------------------------

def test_without_a_log_citation_falls_back_to_strict_trailer_presence(lane):
    """No reachability inference, ever (G1b). A multi-commit push reads as
    uncited, which is the safe direction."""
    for i in range(3):
        lane.commit(f"harness/f{i}.py", f"x = {i}\n", f"harness: step {i}")
    lane.commit("harness/tip.py", "x\n", "harness: tip\n\nreview-seq: 1428")
    d = lane.drift()  # no log
    assert d["strict_fallback"] is True
    assert len(d["window"]) == 4
    assert len(d["uncited"]) == 3, "only the tip carries a trailer, and with " \
                                   "no log there is no range for it to cite"
    block = lane.block()
    assert "no push log" in block
    assert "no reachability inference" in block


def test_without_a_log_the_unknowables_say_unknown(lane):
    block = lane.block()
    assert "fail-open pushes in the log: UNKNOWN (no log)" in block
    assert "uncovered commits: UNKNOWN" in block


def test_a_malformed_log_line_is_counted_not_fatal(lane):
    lane.write_log(lane.genesis("seeded", lane.head()), "this is not a line")
    log = M.parse_push_log(str(lane.log_path))
    assert log["malformed"] == 1
    assert log["present"] is True


# --------------------------------------------------------------------------
# Overrides, derived and counted forever (G5).
# --------------------------------------------------------------------------

def test_overrides_are_derived_from_mains_trailers_not_from_the_log(lane):
    """The count must survive any database rebuild AND the loss of the log, so
    it is computed from `main`'s history every time."""
    lane.commit("harness/a.py", "x\n", "harness: a\n\noverride-seq: 1450")
    lane.commit("harness/b.py", "y\n", "harness: b\n\noverride-seq: 1451")
    lane.commit("harness/c.py", "z\n", "harness: c\n\nreview-seq: 1428")
    ov = M.override_trailers(str(lane.mirror))
    assert [o["seq"] for o in ov] == [1451, 1450]  # newest first, as git logs
    assert "overrides to date: 2" in lane.block()  # with no push log at all
    assert "override-seq 1451, 1450" in lane.block()


def test_no_overrides_reports_zero_without_naming_any(lane):
    assert "overrides to date: 0" in lane.block()


# --------------------------------------------------------------------------
# deploy_state() — the named importable the Plan Room badge calls (P6).
# --------------------------------------------------------------------------

def test_deploy_state_in_sync(lane):
    lane.deploy()
    d = M.deploy_state(lane.config())
    assert d["state"] == "in-sync"
    assert d["mirror_head"] == d["deployed_head"] == lane.head()
    assert d["dirty"] is False


def test_deploy_state_behind_is_drift(lane):
    lane.deploy()
    lane.commit("harness/new.py", "x\n", "harness: unpublished to prod")
    d = M.deploy_state(lane.config())
    assert d["state"] == "drift"
    assert d["behind"] == 1 and d["ahead"] == 0
    assert "prod is at" in d["detail"]


def test_a_dirty_prod_tree_is_drift_even_at_the_right_commit(lane):
    """The ship-by-not-publishing case (seq 1380): code that is running and was
    never published. Nothing else in the house catches it."""
    lane.deploy()
    (lane.prod / "harness" / "hotfix.py").write_text("x = 1\n")
    lane.deploy_dirty = True
    d = M.deploy_state(lane.config())
    assert d["state"] == "drift"
    assert d["dirty"] is True
    assert "never published" in d["detail"]


def test_deploy_state_unknown_when_unconfigured(lane):
    d = M.deploy_state({"gate": {}})
    assert d["state"] == "unknown"
    assert "no [gate].mirror" in d["detail"]


def test_deploy_state_takes_explicit_paths_for_the_plan_room(lane):
    lane.deploy()
    d = M.deploy_state(mirror=str(lane.mirror), deploy_tree=str(lane.prod))
    assert d["state"] == "in-sync"


def test_the_drift_block_carries_the_deploy_line(lane):
    lane.deploy()
    assert "deploy: in-sync" in lane.block()


# --------------------------------------------------------------------------
# An unconfigured detector must not read like a clean one (G3).
# --------------------------------------------------------------------------

def test_an_unconfigured_gate_says_so_loudly(lane):
    d = M.gate_drift({"disjorn": {"custodian_channel_id": 4}}, date=DATE)
    block = M.compose_drift_block(d)
    assert d["configured"] is False
    assert "DETECTOR NOT CONFIGURED" in block
    assert "This is not an empty drift block" in block


def test_gate_drift_never_raises_on_garbage_paths():
    cfg = {"gate": {"canonical_repo": "/nope/disjorn.git", "mirror": "/nope/mirror",
                    "deploy_tree": "/nope/prod", "message_db": "/nope/db"},
           "disjorn": {"custodian_channel_id": 4}, "paths": {}}
    block = M.compose_drift_block(M.gate_drift(cfg, date=DATE))
    assert "hook: ABSENT" in block
    assert "NO LOG" in block
    assert block.startswith(M.DRIFT_HEADER)


# --------------------------------------------------------------------------
# The block reaches the daily line.
# --------------------------------------------------------------------------

def test_the_daily_line_gains_the_drift_block(lane):
    lane.write_log(lane.genesis("seeded", lane.head()))
    doc = {"broker_actions": {"by_resident": {}}, "tool_actions": {"by_resident": {}}}
    body = M.compose_daily_line(doc, lane.config(), DATE, drift=lane.drift())
    assert body.startswith(f"[custodian daily {DATE} UTC")
    assert f"\n\n{M.DRIFT_HEADER}" in body


def test_post_daily_line_computes_the_block_when_none_is_given(lane):
    sent = {}

    def stub(_cfg, body):
        sent["body"] = body
        return {"seq": 1}

    doc = {"broker_actions": {"by_resident": {}}, "tool_actions": {"by_resident": {}}}
    M.post_daily_line(lane.config(), doc, date=DATE, transport=stub)
    assert M.DRIFT_HEADER in sent["body"]


def test_a_daily_line_without_a_drift_argument_is_unchanged(lane):
    """The block is added, never spliced into the existing line."""
    doc = {"broker_actions": {"by_resident": {}}, "tool_actions": {"by_resident": {}}}
    assert M.DRIFT_HEADER not in M.compose_daily_line(doc, lane.config(), DATE)


# --------------------------------------------------------------------------
# No silent caps.
# --------------------------------------------------------------------------

def test_a_long_flag_list_says_how_many_it_dropped(lane):
    floor = lane.head()
    for i in range(M.FLAG_CAP + 3):
        lane.commit(f"harness/u{i}.py", "x\n", f"harness: uncovered {i}")
    lane.write_log(lane.genesis("seeded", floor))
    block = lane.block()
    assert f"uncovered commits above the floor: {M.FLAG_CAP + 3}" in block
    assert "and 3 more, not named here" in block
