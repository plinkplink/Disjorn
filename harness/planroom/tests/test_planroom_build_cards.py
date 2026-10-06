"""Chat builds on the board: a `/build` branch has no SPECS file, so its Review
card comes from the build ledger and the gatehouse shelf."""

import hashlib
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

import planroom as P  # noqa: E402

SLUG = "2026-10-06-dark-mode-toggle"
REQUEST = "dark mode toggle in settings\nand remember it per device"
GIT_ENV = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
           "GIT_CONFIG_GLOBAL": "/dev/null"}
OWNERS = {"server/": "Claudette", "client/": "Gable"}


class Shelf:
    def __init__(self, tmp: Path):
        self.tmp = tmp
        self.work = tmp / "work"
        self.mirror = tmp / "mirror"
        self.gatehouse = tmp / "gatehouse"
        self.ledger = tmp / "build-ledger.jsonl"
        self.db = str(tmp / "disjorn.db")
        self.work.mkdir()
        self.git("init", "-q", "-b", "main")
        self.git("commit", "-q", "--allow-empty", "-m", "init")
        self.gatehouse.mkdir()
        subprocess.run(["git", "clone", "-q", "--bare", str(self.work),
                        str(self.gatehouse / "disjorn.git")], check=True,
                       capture_output=True)
        self.git("remote", "add", "origin", str(self.gatehouse / "disjorn.git"))
        (self.mirror / "SPECS").mkdir(parents=True)
        subprocess.run(["git", "init", "-q", "-b", "main", str(self.mirror)],
                       check=True, capture_output=True)
        subprocess.run(["git", "-C", str(self.mirror), "commit", "-q",
                        "--allow-empty", "-m", "init"], check=True,
                       capture_output=True)
        db = sqlite3.connect(self.db)
        db.execute("create table channels (id integer primary key, type text, "
                   "name text, visibility text)")
        db.execute("create table messages (channel_id int, seq int, content text, "
                   "author_type text, privacy_flags text, deleted_at text)")
        db.execute("insert into channels values (1, 'text', 'general', 'public')")
        db.execute("insert into channels values (7, 'text', 'hush', 'private')")
        db.commit()
        db.close()

    def git(self, *args: str) -> str:
        return subprocess.run(["git", "-C", str(self.work), *args], check=True,
                              capture_output=True, text=True).stdout.strip()

    def push_branch(self, slug: str = SLUG, paths=("server/app/x.py",)) -> str:
        self.git("checkout", "-q", "-b", f"loop/{slug}", "main")
        for p in paths:
            f = self.work / p
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(slug)
            self.git("add", p)
        self.git("commit", "-q", "-m", slug)
        self.git("push", "-q", "origin", f"loop/{slug}")
        self.git("checkout", "-q", "main")
        return self.git("rev-parse", f"loop/{slug}")

    def merge_on_gatehouse(self, slug: str = SLUG) -> None:
        self.git("merge", "-q", "--no-ff", "-m", f"merge: {slug}", f"loop/{slug}")
        self.git("push", "-q", "origin", "main")

    def message(self, seq: int, content: str, channel: int = 1, **kw) -> None:
        db = sqlite3.connect(self.db)
        db.execute("insert into messages values (?,?,?,?,?,?)",
                   (channel, seq, content, kw.get("author_type", "user"),
                    kw.get("flags", "{}"), kw.get("deleted_at")))
        db.commit()
        db.close()

    def ledger_line(self, **rec) -> None:
        with open(self.ledger, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec) + "\n")

    def build(self, slug: str = SLUG, seq: int = 1563, channel: int = 1,
              text: str = REQUEST) -> None:
        self.ledger_line(ts="2026-10-06T10:00:00+00:00", seq=seq,
                         channel_id=channel, author="plink", slug=slug,
                         session_id=16,
                         text_sha256=hashlib.sha256(text.encode()).hexdigest())

    def tier(self, tier: int, tip: str, slug: str = SLUG, green=True) -> None:
        self.ledger_line(ts="2026-10-06T10:30:00+00:00", kind="tier", slug=slug,
                         tier=tier, tip=tip, gates_green=green)

    def cards(self, **kw) -> dict:
        data = P.derive_cards(None, repo=self.mirror, gatehouse=self.gatehouse,
                              message_db=self.db, drift={},
                              lane_owners=kw.pop("lane_owners", OWNERS),
                              build_ledger=str(self.ledger), **kw)
        self.face = data["face"]
        return {c["slug"]: c for c in data["cards"] if c["kind"] == "build"}


@pytest.fixture
def shelf(tmp_path, monkeypatch):
    bin_ = tmp_path / "bin"
    bin_.mkdir()
    (bin_ / "sudo").write_text('#!/bin/sh\nexec "$@"\n')
    (bin_ / "sudo").chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_}:{os.environ['PATH']}")
    for k, v in GIT_ENV.items():
        monkeypatch.setenv(k, v)
    return Shelf(tmp_path)


def test_an_unmerged_tier_two_chat_build_waits_in_review_on_its_reviewer(shelf):
    tip = shelf.push_branch()
    shelf.message(1563, f"/build {REQUEST}")
    shelf.build()
    shelf.tier(2, tip)
    c = shelf.cards()[SLUG]
    assert c["column"] == "Review"
    assert c["title"] == "dark mode toggle in settings"
    assert c["tier"] == "Tier 2"
    assert c["review_owner"] == "Claudette"
    assert c["whose_move"] == "residents"
    assert f"/merge {SLUG} pass <seq>" in c["note"]
    assert c["origin"] == {"channel_id": 1, "channel": "#general", "seq": 1563}
    assert c["where"].startswith("/build in #general seq 1563 · disjorn repo, ")
    assert c["branch"] == f"loop/{SLUG}"
    assert c["requester"] == "plink"


def test_every_owner_of_a_changed_path_is_named_first_match_per_path(shelf):
    shelf.push_branch(paths=("server/app/x.py", "client/src/y.ts", "notes.txt"))
    shelf.build()
    owners = {"server/": "Claudette", "server/app/": "Hob", "client/": "Gable"}
    c = shelf.cards(lane_owners=owners)[SLUG]
    assert c["review_owner"] == "Gable or Claudette"
    assert "no-lane-owner" in c["flags"]


def test_a_build_with_no_tier_line_reads_pending(shelf):
    shelf.push_branch()
    shelf.build()
    c = shelf.cards()[SLUG]
    assert c["tier"] == "Tier pending"
    assert c["whose_move"] == "plink"


def test_a_tier_recorded_for_an_older_tip_reads_pending(shelf):
    shelf.push_branch()
    shelf.build()
    shelf.tier(2, "0" * 40)
    c = shelf.cards()[SLUG]
    assert c["tier"] == "Tier pending"
    assert "has moved since" in c["tier_note"]


def test_red_gates_at_build_end_send_the_card_back_to_plink(shelf):
    tip = shelf.push_branch()
    shelf.build()
    shelf.tier(1, tip, green=False)
    c = shelf.cards()[SLUG]
    assert "gates-red" in c["flags"]
    assert c["whose_move"] == "plink"


def test_a_merged_branch_drops_the_card(shelf):
    tip = shelf.push_branch()
    shelf.build()
    shelf.tier(1, tip)
    assert SLUG in shelf.cards()
    shelf.merge_on_gatehouse()
    assert shelf.cards() == {}


def test_a_deleted_branch_drops_the_card(shelf):
    shelf.push_branch()
    shelf.build()
    shelf.git("push", "-q", "origin", f":loop/{SLUG}")
    assert shelf.cards() == {}


def test_a_tier_zero_self_merged_build_never_shows(shelf):
    tip = shelf.push_branch()
    shelf.build()
    shelf.tier(0, tip)
    shelf.ledger_line(kind="merge", slug=SLUG, tier=0, self_merge=True)
    assert shelf.cards() == {}


def test_a_build_with_a_spec_file_is_left_to_its_spec_card(shelf):
    shelf.push_branch()
    shelf.build()
    (shelf.mirror / "SPECS" / f"{SLUG}.md").write_text("# Spec: x\n")
    assert shelf.cards() == {}


@pytest.mark.parametrize("channel,content,kw", [
    (7, f"/build {REQUEST}", {}),
    (1, "/build something else entirely", {}),
    (1, f"/build {REQUEST}", {"deleted_at": "2026-10-06T11:00:00Z"}),
    (1, f"/build {REQUEST}", {"flags": '{"secret": true}'}),
])
def test_a_request_the_board_may_not_quote_titles_the_card_by_slug(
        shelf, channel, content, kw):
    shelf.push_branch()
    shelf.message(1563, content, channel=channel, **kw)
    shelf.build(channel=channel)
    c = shelf.cards()[SLUG]
    assert c["title"] == SLUG
    assert c["body"] is None


def test_an_unreadable_ledger_is_declared_not_a_crash(shelf):
    shelf.ledger.mkdir()
    assert shelf.cards() == {}
    assert any("build ledger unreadable" in n for n in shelf.face["notes"])


def test_the_ledger_path_comes_from_the_build_block(shelf):
    shelf.push_branch()
    shelf.build()
    data = P.derive_cards({"build": {"ledger": str(shelf.ledger)}},
                          repo=shelf.mirror, gatehouse=shelf.gatehouse,
                          message_db=shelf.db, drift={})
    assert [c["slug"] for c in data["cards"] if c["kind"] == "build"] == [SLUG]
