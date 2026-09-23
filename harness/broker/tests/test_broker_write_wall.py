"""The fails-closed Tier-1 wall — `apply-posted-write`.

SPECS/2026-08-26-approval-object-and-resident-write-verbs.md item 2 (confirmed
by plink, #custodian seq 2022). These are the spec's slice-A acceptance tests:
"fails-closed is only real if the tests have watched it close", so every one of
the four checks is exercised END TO END — real socket, real SO_PEERCRED, real
sqlite ledger, real file on disk.

Held down here:

  * the verb ships OFF, and an unconfigured seat is refused even when it is on;
  * a write with NO record is refused and audited, and writes nothing;
  * a VALID record is applied, byte for byte, and only then;
  * a STALE, OTHER-SEAT, HASH-MISMATCHED, OFF-MAP or ALREADY-CONSUMED record is
    refused and audited;
  * consume-then-write: a write that dies after the consume mark leaves the seq
    spent, and the retry refuses — never a free replay;
  * a surface that cannot be trusted refuses to START, rather than coming up
    looking armed.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import socket
import sqlite3
import sys
import textwrap
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from broker_testlib import ALL_VERBS  # noqa: E402
from brokerd import Broker, ConfigError, load_config  # noqa: E402

ME = os.getuid()
OTHER_UID = ME + 1          # never connects; only the uid MAP needs it
CUSTODIAN = 3
SEAT = "res-test"
SEAT_AUTHOR = "Gable"
PREFIX = "bots/fable"
FRESHNESS = 3600


def record_text(path: str, content: str, *, sha: str | None = None,
                header: str = "disjorn-write-record v1",
                trailer: str = "") -> str:
    digest = sha or hashlib.sha256(content.encode("utf-8")).hexdigest()
    return (f"{header}\npath: {path}\nsha256: {digest}\n"
            f"--- content ---\n{content}--- end ---\n{trailer}")


class WallHarness:
    def __init__(self, broker: Broker, verbs_path: Path, ledger: Path,
                 root: Path, tier_map: Path) -> None:
        self.broker = broker
        self.verbs_path = verbs_path
        self.ledger = ledger
        self.root = root
        self.tier_map = tier_map
        self._next_seq = 100

    # -- client side ------------------------------------------------------
    def call(self, verb: str, args: dict | None = None) -> dict:
        deadline = time.time() + 5
        while True:
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s.settimeout(10)
            try:
                s.connect(self.broker.socket_path)
                break
            except (ConnectionRefusedError, FileNotFoundError):
                s.close()
                if time.time() > deadline:
                    raise
                time.sleep(0.02)
        with s:
            s.sendall(json.dumps(
                {"verb": verb, "args": args or {}}).encode() + b"\n")
            buf = b""
            while b"\n" not in buf:
                chunk = s.recv(4096)
                if not chunk:
                    break
                buf += chunk
        return json.loads(buf.split(b"\n", 1)[0])

    def apply(self, seq: int) -> dict:
        return self.call("apply-posted-write", {"seq": seq})

    # -- the ledger -------------------------------------------------------
    def post(self, content: str, *, author: str = SEAT_AUTHOR,
             channel: int = CUSTODIAN, age_sec: float = 0.0,
             deleted: bool = False, author_type: str = "bot") -> int:
        """One #custodian message, as the server would have written it."""
        seq = self._next_seq
        self._next_seq += 1
        when = dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=age_sec)
        stamp = when.strftime("%Y-%m-%dT%H:%M:%S.") + f"{when.microsecond // 1000:03d}Z"
        db = sqlite3.connect(self.ledger)
        with db:
            table, column = (("bots", "name") if author_type == "bot"
                             else ("users", "username"))
            row = db.execute(f"select id from {table} where {column}=?",
                             (author,)).fetchone()
            if row is None:
                cur = db.execute(f"insert into {table} ({column}) values (?)",
                                 (author,))
                author_id = cur.lastrowid
            else:
                author_id = row[0]
            db.execute(
                "insert into messages (channel_id, seq, author_type, author_id, "
                "content, created_at, deleted_at) values (?, ?, ?, ?, ?, ?, ?)",
                (channel, seq, author_type, author_id, content, stamp,
                 stamp if deleted else None))
        db.close()
        return seq

    def post_record(self, path: str, content: str, **kw) -> int:
        return self.post(record_text(path, content), **kw)

    def edit(self, seq: int, content: str) -> None:
        """What PATCH /messages does: new content, edited_at stamped."""
        db = sqlite3.connect(self.ledger)
        with db:
            db.execute("update messages set content=?, edited_at=? "
                       "where channel_id=? and seq=?",
                       (content, "2026-01-01T00:00:00.000Z", CUSTODIAN, seq))
        db.close()

    # -- config side ------------------------------------------------------
    def set_verbs(self, **flags: bool) -> None:
        lines = [f"[{SEAT}]"]
        for verb in ALL_VERBS:
            lines.append(f'"{verb}" = {str(flags.get(verb, False)).lower()}')
        self.verbs_path.write_text("\n".join(lines) + "\n")

    def set_tiers(self, *, tier0=(), tier1=(), seat: str = SEAT) -> None:
        self.tier_map.write_text(textwrap.dedent(f"""\
            [protected]
            files = []
            [tiers.{seat}]
            tier0 = {json.dumps(list(tier0))}
            tier1 = {json.dumps(list(tier1))}
        """))

    # -- inspection -------------------------------------------------------
    def audit_lines(self) -> list[dict]:
        path = Path(self.broker.audit_path)
        if not path.exists():
            return []
        return [json.loads(ln) for ln in path.read_text().splitlines()
                if ln.strip()]

    def surface_file(self, relative: str) -> Path:
        return self.root / relative


def build_broker(tmp_path: Path, *, write_verbs: bool = True,
                 author: str | None = SEAT_AUTHOR,
                 root: Path | None = None,
                 repo_prefix: str | None = PREFIX,
                 protected_paths: bool = True,
                 message_db: bool = True,
                 consumed_ledger: bool = True,
                 state_dir: Path | None = None,
                 extra_seat: str | None = None) -> WallHarness:
    ledger = tmp_path / "disjorn.db"
    db = sqlite3.connect(ledger)
    with db:
        db.execute("create table messages (id integer primary key autoincrement,"
                   " channel_id integer, seq integer, author_type text,"
                   " author_id integer, content text, created_at text,"
                   " edited_at text, deleted_at text)")
        db.execute("create table bots (id integer primary key autoincrement,"
                   " name text)")
        db.execute("create table users (id integer primary key autoincrement,"
                   " username text)")
    db.close()

    surface = root if root is not None else tmp_path / "surface"
    surface.mkdir(exist_ok=True)
    (surface / "spine").mkdir(exist_ok=True)
    state = state_dir if state_dir is not None else tmp_path / "state"
    state.mkdir(exist_ok=True)
    tier_map = tmp_path / "protected-paths.toml"
    tier_map.write_text("[protected]\nfiles = []\n"
                        f"[tiers.{SEAT}]\ntier1 = [\"{PREFIX}/spine\"]\n")

    block = ""
    if write_verbs:
        block = "\n[write_verbs]\n"
        block += f"freshness_sec = {FRESHNESS}\n"
        if message_db:
            block += f'message_db = "{ledger}"\n'
        if consumed_ledger:
            block += f'consumed_ledger = "{state / "write-consumed.jsonl"}"\n'
        block += f"\n[write_verbs.{extra_seat or SEAT}]\n"
        if author is not None:
            block += f'author = "{author}"\n'
        if repo_prefix is not None:
            block += f'repo_prefix = "{repo_prefix}"\n'
        block += f'root = "{surface}"\n'

    broker_toml = tmp_path / "broker.toml"
    broker_toml.write_text(textwrap.dedent(f"""\
        [broker]
        socket_path = "{tmp_path / 'b.sock'}"
        audit_log = "{tmp_path / 'audit.jsonl'}"

        [uids]
        "{ME}" = "{SEAT}"
        "{OTHER_UID}" = "res-other"

        [residents.{SEAT}]
        log_path = "{tmp_path / 'seat.log'}"

        [residents.res-other]
        log_path = "{tmp_path / 'other.log'}"

        [paths]
        {'protected_paths = "' + str(tier_map) + '"' if protected_paths else ''}

        [disjorn]
        url = "http://127.0.0.1:1"
        api_key_path = "{tmp_path / 'no-key'}"
        custodian_channel_id = {CUSTODIAN}
    """) + block)

    verbs_path = tmp_path / "verbs.toml"
    verbs_path.write_text(f'[{SEAT}]\n"apply-posted-write" = true\n')
    broker = Broker(load_config(str(broker_toml)), str(verbs_path))
    h = WallHarness(broker, verbs_path, ledger, surface, tier_map)
    h.consumed = state / "write-consumed.jsonl"
    t = threading.Thread(target=broker.serve_forever, daemon=True)
    t.start()
    deadline = time.time() + 5
    while not os.path.exists(broker.socket_path):
        if time.time() > deadline:
            raise RuntimeError("broker socket never appeared")
        time.sleep(0.01)
    h._thread = t
    return h


@pytest.fixture()
def wall(tmp_path):
    h = build_broker(tmp_path)
    yield h
    h.broker.shutdown()
    h._thread.join(timeout=5)


# ------------------------------------------------------------- ships OFF

def test_the_verb_ships_off(wall):
    wall.set_verbs()  # everything explicitly false, which is the template
    seq = wall.post_record(f"{PREFIX}/spine/05.md", "hello\n")
    resp = wall.apply(seq)
    assert resp["error"]["code"] == "verb-disabled"
    assert not wall.surface_file("spine/05.md").exists()
    assert wall.audit_lines()[-1]["allowed"] is False


def test_a_seat_with_no_write_section_is_refused(tmp_path):
    """The kill switch on and no surface configured is still no surface."""
    h = build_broker(tmp_path, write_verbs=False)
    try:
        seq = h.post_record(f"{PREFIX}/spine/05.md", "hello\n")
        resp = h.apply(seq)
        assert resp["error"]["code"] == "verb-disabled"
        assert "write_verbs" in resp["error"]["message"]
        # A refusal that audited as allowed would read as a capability the
        # seat has.
        assert h.audit_lines()[-1]["allowed"] is False
    finally:
        h.broker.shutdown()
        h._thread.join(timeout=5)


# ---------------------------------------------------- no record → refused

def test_a_write_with_no_record_is_refused_and_audited(wall):
    resp = wall.apply(4242)
    assert resp["error"]["code"] == "bad-args"
    assert "does not resolve" in resp["error"]["message"]
    assert not wall.surface_file("spine/05.md").exists()
    line = wall.audit_lines()[-1]
    assert line["verb"] == "apply-posted-write" and line["allowed"] is False


def test_a_record_posted_in_another_channel_does_not_count(wall):
    seq = wall.post_record(f"{PREFIX}/spine/05.md", "hello\n",
                           channel=CUSTODIAN + 1)
    resp = wall.apply(seq)
    assert resp["error"]["code"] == "bad-args"
    assert "in #custodian" in resp["error"]["message"]


def test_a_deleted_post_is_not_a_record(wall):
    seq = wall.post_record(f"{PREFIX}/spine/05.md", "hello\n", deleted=True)
    resp = wall.apply(seq)
    assert resp["error"]["code"] == "bad-args"
    assert resp["error"]["reason"] == "post-deleted"
    line = wall.audit_lines()[-1]
    assert line["allowed"] is False and "(post-deleted)" in line["result_summary"]


def test_a_record_edited_after_it_was_posted_is_refused(wall):
    """The sha cannot catch this: whoever edits the content edits the sha line
    in the same stroke. What was shown is the only thing the wall may write."""
    seq = wall.post_record(f"{PREFIX}/spine/05.md", "as shown\n")
    wall.edit(seq, record_text(f"{PREFIX}/spine/05.md", "swapped in later\n"))
    resp = wall.apply(seq)
    assert resp["error"]["code"] == "bad-args"
    assert resp["error"]["reason"] == "post-edited"
    assert not wall.surface_file("spine/05.md").exists()
    line = wall.audit_lines()[-1]
    assert line["allowed"] is False and "(post-edited)" in line["result_summary"]


def test_a_post_that_is_not_a_record_is_refused(wall):
    seq = wall.post("I am going to update my spine, honest")
    resp = wall.apply(seq)
    assert resp["error"]["code"] == "bad-args"
    assert "not a write record" in resp["error"]["message"]


def test_a_record_buried_in_prose_is_refused(wall):
    """The post IS the record. A record that may be embedded is a record whose
    boundaries the parser and a reader can disagree about."""
    body = record_text(f"{PREFIX}/spine/05.md", "hello\n",
                       trailer="and here is why I did it\n")
    seq = wall.post(body)
    resp = wall.apply(seq)
    assert resp["error"]["code"] == "bad-args"
    assert "end fence" in resp["error"]["message"]


# ------------------------------------------------------ valid → applied

def test_a_valid_record_is_applied_byte_for_byte(wall):
    content = "# bearings\n\nthe new line\n"
    seq = wall.post_record(f"{PREFIX}/spine/05-bearings.md", content)
    resp = wall.apply(seq)
    assert resp["ok"] is True, resp
    assert resp["result"]["path"] == f"{PREFIX}/spine/05-bearings.md"
    assert resp["result"]["tier"] == 1
    assert wall.surface_file("spine/05-bearings.md").read_text() == content


def test_a_valid_record_creates_a_file_that_did_not_exist(wall):
    """A new spine entry is the case the tiers spec names; a wall that only
    lets you edit what is already there is not the wall it claims to be."""
    seq = wall.post_record(f"{PREFIX}/spine/99-new.md", "brand new\n")
    assert wall.apply(seq)["ok"] is True
    assert wall.surface_file("spine/99-new.md").read_text() == "brand new\n"


def test_the_consume_mark_lands_before_the_result_line(wall):
    seq = wall.post_record(f"{PREFIX}/spine/05.md", "hello\n")
    before = len(wall.audit_lines())
    wall.apply(seq)
    consume, result = wall.audit_lines()[before:]
    assert consume["consumed_seq"] == seq
    assert "writing next" in consume["result_summary"]
    assert result["write_applied"] is True
    assert "applied" in result["result_summary"]


def test_an_existing_file_keeps_its_mode(wall):
    target = wall.surface_file("spine/05.md")
    target.write_text("old\n")
    target.chmod(0o640)
    seq = wall.post_record(f"{PREFIX}/spine/05.md", "new\n")
    assert wall.apply(seq)["ok"] is True
    assert target.read_text() == "new\n"
    assert oct(target.stat().st_mode & 0o777) == "0o640"


# ------------------------------------------------------- the four refusals

def test_a_stale_record_is_refused(wall):
    seq = wall.post_record(f"{PREFIX}/spine/05.md", "hello\n",
                           age_sec=FRESHNESS + 60)
    resp = wall.apply(seq)
    assert resp["error"]["code"] == "bad-args"
    assert "freshness window" in resp["error"]["message"]
    assert not wall.surface_file("spine/05.md").exists()


def test_another_seats_record_is_refused(wall):
    seq = wall.post_record(f"{PREFIX}/spine/05.md", "hello\n",
                           author="Claudette")
    resp = wall.apply(seq)
    assert resp["error"]["code"] == "bad-args"
    assert "posted by" in resp["error"]["message"]
    assert not wall.surface_file("spine/05.md").exists()


def test_a_person_account_named_like_the_seat_is_refused(wall):
    """Check (a) is on the seat's bot identity. A human account that happens
    to share the seat's name is not the seat."""
    seq = wall.post_record(f"{PREFIX}/spine/05.md", "hello\n",
                           author=SEAT_AUTHOR, author_type="user")
    resp = wall.apply(seq)
    assert resp["error"]["code"] == "bad-args"
    assert resp["error"]["reason"] == "not-a-bot-post"
    assert not wall.surface_file("spine/05.md").exists()
    assert wall.audit_lines()[-1]["allowed"] is False


def test_a_hash_mismatched_record_is_refused(wall):
    seq = wall.post(record_text(f"{PREFIX}/spine/05.md", "hello\n",
                                sha="0" * 64))
    resp = wall.apply(seq)
    assert resp["error"]["code"] == "bad-args"
    assert "sha256 does not match" in resp["error"]["message"]
    assert not wall.surface_file("spine/05.md").exists()


def test_a_consumed_record_is_refused_the_second_time(wall):
    seq = wall.post_record(f"{PREFIX}/spine/05.md", "first\n")
    assert wall.apply(seq)["ok"] is True
    wall.surface_file("spine/05.md").write_text("edited by hand\n")
    resp = wall.apply(seq)
    assert resp["error"]["code"] == "bad-args"
    assert "already consumed" in resp["error"]["message"]
    assert wall.surface_file("spine/05.md").read_text() == "edited by hand\n"


def test_consume_happens_before_the_write(wall):
    """Rev 2's ordering, watched: a write that fails AFTER the consume mark
    leaves the seq spent. The caller posts a fresh record; it never gets a free
    replay off the old one."""
    if os.geteuid() == 0:
        pytest.skip("root ignores directory permissions")
    seq = wall.post_record(f"{PREFIX}/spine/05.md", "hello\n")
    spine = wall.root / "spine"
    spine.chmod(0o500)
    try:
        resp = wall.apply(seq)
        assert resp["error"]["code"] == "exec-failure"
        assert not (spine / "05.md").exists()
    finally:
        spine.chmod(0o755)
    retry = wall.apply(seq)
    assert retry["error"]["code"] == "bad-args"
    assert "already consumed" in retry["error"]["message"]


def test_a_spent_seq_still_refuses_after_the_audit_log_is_truncated(wall):
    """The consumed-set is its own file, so the audit log can be rotated,
    truncated or lost without re-arming a spent record."""
    seq = wall.post_record(f"{PREFIX}/spine/05.md", "first\n")
    assert wall.apply(seq)["ok"] is True
    wall.surface_file("spine/05.md").write_text("edited by hand\n")
    Path(wall.broker.audit_path).write_text("")
    resp = wall.apply(seq)
    assert resp["error"]["code"] == "bad-args"
    assert "already consumed" in resp["error"]["message"]
    assert wall.surface_file("spine/05.md").read_text() == "edited by hand\n"


def test_a_spent_seq_still_refuses_after_the_audit_log_is_rotated(wall):
    seq = wall.post_record(f"{PREFIX}/spine/05.md", "first\n")
    assert wall.apply(seq)["ok"] is True
    audit = Path(wall.broker.audit_path)
    audit.rename(audit.with_suffix(".jsonl.1"))
    assert "already consumed" in wall.apply(seq)["error"]["message"]


def test_the_consumed_set_names_seq_seat_path_and_hash(wall):
    content = "hello\n"
    seq = wall.post_record(f"{PREFIX}/spine/05.md", content)
    assert wall.apply(seq)["ok"] is True
    entry, = [json.loads(ln) for ln in wall.consumed.read_text().splitlines()]
    assert entry["seq"] == seq and entry["seat"] == SEAT
    assert entry["path"] == f"{PREFIX}/spine/05.md"
    assert entry["sha256"] == hashlib.sha256(content.encode()).hexdigest()


def test_an_unreadable_consumed_set_refuses_the_write(wall):
    """A line that does not parse could be hiding a spent seq."""
    wall.consumed.write_text("not json\n")
    seq = wall.post_record(f"{PREFIX}/spine/05.md", "hello\n")
    resp = wall.apply(seq)
    assert resp["error"]["code"] == "internal"
    assert not wall.surface_file("spine/05.md").exists()


def test_a_torn_final_line_is_dropped_not_fatal(wall):
    """A crash mid-append leaves a line with no newline. Its fsync never
    returned, so nothing was written on it: the next mark trims it away."""
    first = wall.post_record(f"{PREFIX}/spine/05.md", "one\n")
    assert wall.apply(first)["ok"] is True
    with wall.consumed.open("a") as fh:
        fh.write('{"seq": 9')
    second = wall.post_record(f"{PREFIX}/spine/06.md", "two\n")
    assert wall.apply(second)["ok"] is True
    seqs = [json.loads(ln)["seq"] for ln in wall.consumed.read_text().splitlines()]
    assert seqs == [first, second]
    assert "already consumed" in wall.apply(first)["error"]["message"]


# ------------------------------------------------------------ the tier map

def test_a_path_no_tier_row_names_is_refused(wall):
    seq = wall.post_record(f"{PREFIX}/GENESIS.md", "rewritten\n")
    resp = wall.apply(seq)
    assert resp["error"]["code"] == "bad-args"
    assert "Tier-0/1 surface map" in resp["error"]["message"]
    assert not wall.surface_file("GENESIS.md").exists()


def test_a_tier0_row_is_written_too(wall):
    wall.set_tiers(tier0=[f"{PREFIX}/README.md"], tier1=[f"{PREFIX}/spine"])
    seq = wall.post_record(f"{PREFIX}/README.md", "docs\n")
    resp = wall.apply(seq)
    assert resp["ok"] is True and resp["result"]["tier"] == 0


def test_an_empty_tier_map_writes_nothing(wall):
    """How both seats ship: the rows are the other half of OFF."""
    wall.set_tiers()
    seq = wall.post_record(f"{PREFIX}/spine/05.md", "hello\n")
    assert wall.apply(seq)["error"]["code"] == "bad-args"
    assert not wall.surface_file("spine/05.md").exists()


def test_a_tier_row_outside_the_seats_prefix_resolves_to_nothing(wall):
    """The two plink-owned files must AGREE: a tier row naming someone else's
    tree has no host path here, so neither file can widen the surface alone."""
    wall.set_tiers(tier1=["server/app/privacy.py"])
    seq = wall.post_record("server/app/privacy.py", "# nope\n")
    resp = wall.apply(seq)
    assert resp["error"]["code"] == "bad-args"
    assert "outside this seat's surface" in resp["error"]["message"]


def test_another_seats_tier_rows_do_not_apply(wall):
    wall.set_tiers(tier1=[f"{PREFIX}/spine"], seat="res-other")
    seq = wall.post_record(f"{PREFIX}/spine/05.md", "hello\n")
    assert wall.apply(seq)["error"]["code"] == "bad-args"


def test_a_traversing_path_is_refused(wall):
    seq = wall.post(record_text(f"{PREFIX}/spine/../../../etc/passwd", "x\n"))
    resp = wall.apply(seq)
    assert resp["error"]["code"] == "bad-args"
    assert "unusable component" in resp["error"]["message"]


def test_an_absolute_path_is_refused(wall):
    seq = wall.post(record_text("/etc/passwd", "x\n"))
    resp = wall.apply(seq)
    assert resp["error"]["code"] == "bad-args"
    assert "repo-relative" in resp["error"]["message"]


def test_a_write_into_a_directory_that_does_not_exist_is_refused(wall):
    wall.set_tiers(tier1=[PREFIX])
    seq = wall.post_record(f"{PREFIX}/new-dir/thing.md", "x\n")
    resp = wall.apply(seq)
    assert resp["error"]["code"] == "bad-args"
    assert "creates no directories" in resp["error"]["message"]


def test_an_unreadable_tier_map_refuses_the_write(wall):
    wall.tier_map.unlink()
    seq = wall.post_record(f"{PREFIX}/spine/05.md", "hello\n")
    resp = wall.apply(seq)
    assert resp["error"]["code"] == "internal"
    assert not wall.surface_file("spine/05.md").exists()


# ------------------------------------------------------- refuse to START

def test_a_resident_writable_root_refuses_to_start(tmp_path):
    root = tmp_path / "open-surface"
    root.mkdir()
    root.chmod(0o777)
    with pytest.raises(ConfigError) as exc:
        build_broker(tmp_path, root=root)
    assert "write_verbs" in str(exc.value)
    assert "world-writable" in str(exc.value)


def test_a_section_with_no_author_refuses_to_start(tmp_path):
    with pytest.raises(ConfigError) as exc:
        build_broker(tmp_path, author=None)
    assert "author is missing" in str(exc.value)


def test_a_section_with_no_repo_prefix_refuses_to_start(tmp_path):
    with pytest.raises(ConfigError) as exc:
        build_broker(tmp_path, repo_prefix=None)
    assert "repo_prefix is missing" in str(exc.value)


def test_a_section_for_a_stranger_refuses_to_start(tmp_path):
    with pytest.raises(ConfigError) as exc:
        build_broker(tmp_path, extra_seat="res-nobody")
    assert "not a resident of this house" in str(exc.value)


def test_no_tier_map_configured_refuses_to_start(tmp_path):
    with pytest.raises(ConfigError) as exc:
        build_broker(tmp_path, protected_paths=False)
    assert "protected_paths" in str(exc.value)


def test_no_consumed_ledger_refuses_to_start(tmp_path):
    with pytest.raises(ConfigError) as exc:
        build_broker(tmp_path, consumed_ledger=False)
    assert "consumed_ledger" in str(exc.value)


def test_a_resident_writable_consumed_ledger_dir_refuses_to_start(tmp_path):
    state = tmp_path / "open-state"
    state.mkdir()
    state.chmod(0o777)
    with pytest.raises(ConfigError) as exc:
        build_broker(tmp_path, state_dir=state)
    assert "consumed_ledger" in str(exc.value)
    assert "world-writable" in str(exc.value)


def test_no_message_store_refuses_to_start(tmp_path):
    with pytest.raises(ConfigError) as exc:
        build_broker(tmp_path, message_db=False)
    assert "message store" in str(exc.value)
