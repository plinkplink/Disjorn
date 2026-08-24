"""Plan Room router — the tab, and the wall around its two write halves.

Two claims are on trial here and everything else is detail:

1. **The server derives nothing.** It reads the broker-written index and its
   own two board-native tables, and there is no third source. So these tests
   build an index file by hand and assert the server repeats it — including
   when the index says something the server would never have concluded itself.
2. **Derived state has no write path.** Not "is checked" — absent. The write
   endpoints touch `card_meta` and `card_comments`, and a rebuild of the index
   wipes nothing a human wrote.
"""

import json
import sqlite3

import pytest

from app import db
from app.config import reset_settings_cache
from app.routers import auth

PASSWORD = "correct horse battery staple"
PASSWORD_HASH = auth.hash_password(PASSWORD)  # hash once — argon2 is slow
BOT_KEY = "bot-key-planroom"

CARD = {
    "slug": "2026-08-20-plan-room",
    "kind": "spec",
    "title": "The Plan Room",
    "column": "Ready",
    "spec_path": "SPECS/2026-08-20-plan-room.md",
    "status": "confirmed",
    "status_word": "confirmed",
    "tier": "Tier 2",
    "lane": "cross-lane",
    "review_owner": "Claudette",
    "builder": "Gable",
    "requester": "plink",
    "confirm_seq": 1434,
    "flags": [],
    "shas": [],
    "deploy": None,
    "whose_move": "residents",
    "note": "Confirmed and pressable.",
    "where": "SPECS/2026-08-20-plan-room.md",
    "position": 0,
}


def card(**over) -> dict:
    c = dict(CARD)
    c.update(over)
    return c


FACE = {
    "derived_at": "2026-08-23T00:00:00+00:00",
    "mirror_head": "9052940a90c7dead",
    "deploy": {"badge": "green", "detail": "prod runs mirror head"},
    "columns": ["Backlog", "Proposed", "Ready", "Building", "Review", "Merged",
                "Archived"],
    "notes": [],
}


def write_index(path, cards, face=None) -> None:
    """Build an index file the way the broker does. The server never learns
    where these came from, which is the point."""
    conn = sqlite3.connect(str(path))
    conn.executescript("""
        CREATE TABLE face (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE cards (
            slug TEXT PRIMARY KEY, board_column TEXT NOT NULL,
            position INTEGER NOT NULL, kind TEXT NOT NULL, title TEXT NOT NULL,
            whose_move TEXT NOT NULL, haystack TEXT NOT NULL,
            card_json TEXT NOT NULL);
    """)
    conn.executemany("INSERT INTO face VALUES (?, ?)",
                     [(k, json.dumps(v)) for k, v in (face or FACE).items()])
    conn.executemany(
        "INSERT INTO cards VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        [(c["slug"], c["column"], c.get("position", i), c["kind"], c["title"],
          c["whose_move"], json.dumps(c), json.dumps(c))
         for i, c in enumerate(cards)])
    conn.commit()
    conn.close()


@pytest.fixture
def index(tmp_path, monkeypatch):
    """An index file the server is pointed at. Returns a writer so a test can
    rebuild it mid-flight — the broker does exactly that."""
    path = tmp_path / "planroom-index.db"

    def build(cards, face=None):
        if path.exists():
            path.unlink()
        write_index(path, cards, face)
        return path

    monkeypatch.setenv("PLANROOM_INDEX", str(path))
    reset_settings_cache()
    build([card()])
    yield build
    reset_settings_cache()


@pytest.fixture
def no_index(tmp_path, monkeypatch):
    monkeypatch.setenv("PLANROOM_INDEX", str(tmp_path / "never-written.db"))
    reset_settings_cache()
    yield
    reset_settings_cache()


async def make_user(username: str, *, admin: bool = False) -> int:
    cur = await db.execute(
        "INSERT INTO users (username, password_hash, display_name, is_admin) "
        "VALUES (?, ?, ?, ?)",
        (username, PASSWORD_HASH, username.capitalize(), 1 if admin else 0))
    return cur.lastrowid


async def make_bot(name: str = "broker", api_key: str = BOT_KEY) -> int:
    cur = await db.execute("INSERT INTO bots (name, api_key_hash) VALUES (?, ?)",
                           (name, auth.hash_api_key(api_key)))
    return cur.lastrowid


async def login(client, username: str) -> None:
    r = await client.post("/auth/login",
                          json={"username": username, "password": PASSWORD})
    assert r.status_code == 200, r.text


def key(api_key: str = BOT_KEY) -> dict:
    return {"X-Api-Key": api_key}


# ── the server derives nothing ──────────────────────────────────────────────

async def test_the_board_is_whatever_the_index_says(client, app, index):
    await make_user("reader")
    await login(client, "reader")
    r = await client.get("/planroom/board")
    assert r.status_code == 200
    body = r.json()
    assert [c["slug"] for c in body["cards"]] == ["2026-08-20-plan-room"]
    assert body["cards"][0]["column"] == "Ready"
    assert body["face"]["mirror_head"] == "9052940a90c7dead"


async def test_the_server_repeats_the_index_even_when_it_is_surprising(
        client, app, index):
    """The strongest form of "does not derive": the index says a `confirmed`
    spec sits in Merged. A server with its own opinion would correct it. This
    one has no opinion to correct it with."""
    index([card(column="Merged", status_word="confirmed")])
    await make_user("reader")
    await login(client, "reader")
    body = (await client.get("/planroom/board")).json()
    assert body["cards"][0]["column"] == "Merged"


async def test_the_face_carries_derivation_time_and_mirror_head(client, app,
                                                                 index):
    await make_user("reader")
    await login(client, "reader")
    face = (await client.get("/planroom/board")).json()["face"]
    assert face["derived_at"] == "2026-08-23T00:00:00+00:00"
    assert face["mirror_head"]
    assert face["available"] is True


async def test_a_missing_index_is_declared_not_empty(client, app, no_index):
    """An absent index and an empty board must not read alike."""
    await make_user("reader")
    await login(client, "reader")
    body = (await client.get("/planroom/board")).json()
    assert body["face"]["available"] is False
    assert body["face"]["unavailable_reason"]
    assert body["cards"] == []


async def test_an_unconfigured_index_says_so(client, app, monkeypatch):
    monkeypatch.setenv("PLANROOM_INDEX", "")
    reset_settings_cache()
    await make_user("reader")
    await login(client, "reader")
    body = (await client.get("/planroom/board")).json()
    assert "not configured" in body["face"]["unavailable_reason"]
    reset_settings_cache()


async def test_a_corrupt_index_does_not_500(client, app, tmp_path, monkeypatch):
    p = tmp_path / "bad.db"
    p.write_bytes(b"not a database at all")
    monkeypatch.setenv("PLANROOM_INDEX", str(p))
    reset_settings_cache()
    await make_user("reader")
    await login(client, "reader")
    r = await client.get("/planroom/board")
    assert r.status_code == 200
    assert r.json()["face"]["available"] is False
    reset_settings_cache()


async def test_a_rebuild_underneath_a_live_server_is_picked_up(client, app,
                                                                index):
    """The broker rebuilds by rename. A connection held open across that would
    keep serving the deleted inode — a board that silently stops moving."""
    await make_user("reader")
    await login(client, "reader")
    assert (await client.get("/planroom/board")).json()["cards"][0]["column"] == "Ready"
    index([card(column="Building")])
    assert (await client.get("/planroom/board")).json()["cards"][0]["column"] == "Building"


# ── everyone may read the tab ───────────────────────────────────────────────

async def test_a_non_admin_may_read(client, app, index):
    await make_user("plain")
    await login(client, "plain")
    assert (await client.get("/planroom/board")).status_code == 200


async def test_a_bot_may_read(client, app, index):
    await make_bot()
    assert (await client.get("/planroom/board", headers=key())).status_code == 200


async def test_an_anonymous_caller_may_not(client, app, index):
    assert (await client.get("/planroom/board")).status_code == 401


# ── board-native state is ours, and survives every rebuild ──────────────────

async def test_a_comment_survives_an_index_rebuild(client, app, index):
    """THE property. The index rebuilds from zero; comments are not in it."""
    await make_user("admin", admin=True)
    await login(client, "admin")
    r = await client.post("/planroom/cards/2026-08-20-plan-room/comment",
                          json={"text": "blocked on the gatehouse chown"})
    assert r.status_code == 200, r.text
    index([card(column="Building")])          # rebuild from zero
    body = (await client.get("/planroom/cards/2026-08-20-plan-room")).json()
    assert body["card"]["column"] == "Building"
    assert [c["text"] for c in body["comments"]] == \
           ["blocked on the gatehouse chown"]


async def test_a_blocked_flag_survives_an_index_rebuild(client, app, index):
    await make_user("admin", admin=True)
    await login(client, "admin")
    await client.post("/planroom/cards/2026-08-20-plan-room/flag",
                      json={"blocked": True, "reason": "waiting on plink"})
    index([card()])
    c = (await client.get("/planroom/board")).json()["cards"][0]
    assert c["blocked"] is True
    assert c["blocked_reason"] == "waiting on plink"


async def test_blocked_is_a_flag_never_a_column(client, app, index):
    """A held card keeps its place so everyone can see where it re-enters."""
    await make_user("admin", admin=True)
    await login(client, "admin")
    await client.post("/planroom/cards/2026-08-20-plan-room/flag",
                      json={"blocked": True, "reason": "held"})
    c = (await client.get("/planroom/board")).json()["cards"][0]
    assert c["blocked"] is True
    assert c["column"] == "Ready", "a blocked card does not move"
    assert "Blocked" not in (await client.get("/planroom/board")).json()["counts"]


async def test_blocking_without_a_reason_is_refused(client, app, index):
    """A card blocked for no stated reason is one nobody can unblock."""
    await make_user("admin", admin=True)
    await login(client, "admin")
    r = await client.post("/planroom/cards/2026-08-20-plan-room/flag",
                          json={"blocked": True, "reason": "   "})
    assert r.status_code == 400
    assert "reason" in r.json()["detail"]


async def test_unblocking_clears_the_reason(client, app, index):
    await make_user("admin", admin=True)
    await login(client, "admin")
    await client.post("/planroom/cards/2026-08-20-plan-room/flag",
                      json={"blocked": True, "reason": "held"})
    await client.post("/planroom/cards/2026-08-20-plan-room/flag",
                      json={"blocked": False})
    c = (await client.get("/planroom/board")).json()["cards"][0]
    assert c["blocked"] is False
    assert c["blocked_reason"] is None


async def test_archiving_moves_a_merged_card_to_the_archived_column(client, app,
                                                                     index):
    index([card(column="Merged", status_word="merged")])
    await make_user("admin", admin=True)
    await login(client, "admin")
    r = await client.post("/planroom/cards/2026-08-20-plan-room/archive",
                          json={"archived": True})
    assert r.status_code == 200, r.text
    assert r.json()["card"]["column"] == "Archived"


async def test_only_a_merged_card_can_be_archived(client, app, index):
    """Archiving anything else would hide work that is still in flight."""
    await make_user("admin", admin=True)
    await login(client, "admin")
    r = await client.post("/planroom/cards/2026-08-20-plan-room/archive",
                          json={"archived": True})
    assert r.status_code == 400


async def test_the_index_never_carries_the_archived_flag(client, app, index):
    """Derivation cannot know a board-native fact. If it tried, the flag would
    be lost on every rebuild — which is exactly what this asserts does not
    happen."""
    index([card(column="Merged")])
    await make_user("admin", admin=True)
    await login(client, "admin")
    await client.post("/planroom/cards/2026-08-20-plan-room/archive",
                      json={"archived": True})
    index([card(column="Merged")])       # the index still says Merged
    assert (await client.get("/planroom/board")).json()["cards"][0]["column"] \
        == "Archived"


async def test_order_overrides_the_derived_position(client, app, index):
    index([card(slug="2026-08-01-a", title="a", position=0),
           card(slug="2026-08-02-b", title="b", position=1)])
    await make_user("admin", admin=True)
    await login(client, "admin")
    await client.post("/planroom/cards/2026-08-02-b/order", json={"sort_order": 0})
    slugs = [c["slug"] for c in (await client.get("/planroom/board")).json()["cards"]]
    assert slugs == ["2026-08-02-b", "2026-08-01-a"]


async def test_clearing_order_returns_the_card_to_derived_order(client, app,
                                                                index):
    index([card(slug="2026-08-01-a", title="a", position=0),
           card(slug="2026-08-02-b", title="b", position=1)])
    await make_user("admin", admin=True)
    await login(client, "admin")
    await client.post("/planroom/cards/2026-08-02-b/order", json={"sort_order": 0})
    await client.post("/planroom/cards/2026-08-02-b/order", json={"sort_order": None})
    slugs = [c["slug"] for c in (await client.get("/planroom/board")).json()["cards"]]
    assert slugs == ["2026-08-01-a", "2026-08-02-b"]


# ── the wall ────────────────────────────────────────────────────────────────

async def test_a_non_admin_cannot_comment(client, app, index):
    await make_user("plain")
    await login(client, "plain")
    r = await client.post("/planroom/cards/2026-08-20-plan-room/comment",
                          json={"text": "hello"})
    assert r.status_code == 403


async def test_a_non_admin_cannot_flag(client, app, index):
    await make_user("plain")
    await login(client, "plain")
    r = await client.post("/planroom/cards/2026-08-20-plan-room/flag",
                          json={"blocked": True, "reason": "x"})
    assert r.status_code == 403


async def test_a_bot_may_comment_and_flag(client, app, index):
    """The residents' two write verbs land here, through the broker's bot key."""
    await make_bot()
    r = await client.post("/planroom/cards/2026-08-20-plan-room/comment",
                          json={"text": "reading it now", "author": "res-gable"},
                          headers=key())
    assert r.status_code == 200, r.text
    assert r.json()["comment"]["author_label"] == "res-gable (via broker)"
    r = await client.post("/planroom/cards/2026-08-20-plan-room/flag",
                          json={"blocked": True, "reason": "needs the chown",
                                "author": "res-gable"}, headers=key())
    assert r.status_code == 200, r.text


async def test_a_bot_may_not_archive_or_reorder(client, app, index):
    """Order and archive stay admin-only in Phase I (seq 1428 P1). Admin
    surfaces are cookie-only by construction, so a leaked bot key cannot file
    the board away."""
    index([card(column="Merged")])
    await make_bot()
    assert (await client.post("/planroom/cards/2026-08-20-plan-room/archive",
                              json={"archived": True},
                              headers=key())).status_code == 401
    assert (await client.post("/planroom/cards/2026-08-20-plan-room/order",
                              json={"sort_order": 0},
                              headers=key())).status_code == 401


async def test_a_human_cannot_label_a_comment_with_someone_elses_name(
        client, app, index):
    """`author` is a bot's attestation of who asked it. For a signed-in person
    it is a forgery affordance with no use, so it is ignored outright."""
    await make_user("admin", admin=True)
    await login(client, "admin")
    r = await client.post("/planroom/cards/2026-08-20-plan-room/comment",
                          json={"text": "hi", "author": "plink"})
    assert r.json()["comment"]["author_label"] == "Admin"


async def test_there_is_no_write_path_to_derived_state(client, app, index):
    """Not a check — an absence. Every route this router exposes writes
    card_meta, card_comments, or (slice A) a BACKLOG ROW, which is the artifact
    a backlog card renders rather than anything derived. There is still no
    endpoint that could move a card's column, change a SPECS/ Status line, or
    set a deploy badge, because no such endpoint exists to be authorized.

    This list is a canary. A route added here is a route that has to justify
    which of those three things it writes."""
    from app.routers import planroom
    writes = {r.path for r in planroom.router.routes
              if getattr(r, "methods", set()) & {"POST", "PUT", "PATCH",
                                                 "DELETE"}}
    assert writes == {
        "/planroom/cards/{slug}/comment",
        "/planroom/cards/{slug}/flag",
        "/planroom/cards/{slug}/archive",
        "/planroom/cards/{slug}/order",
        "/planroom/cards/{slug}/status",
    }


async def test_a_write_to_a_card_that_is_not_on_the_board_is_refused(
        client, app, index):
    await make_user("admin", admin=True)
    await login(client, "admin")
    r = await client.post("/planroom/cards/2026-01-01-nope/comment",
                          json={"text": "x"})
    assert r.status_code == 404


async def test_a_write_with_the_index_down_is_refused_not_guessed(
        client, app, no_index):
    """You cannot flag a card on a board you cannot read."""
    await make_user("admin", admin=True)
    await login(client, "admin")
    r = await client.post("/planroom/cards/2026-08-20-plan-room/flag",
                          json={"blocked": True, "reason": "x"})
    assert r.status_code == 503


@pytest.mark.parametrize("slug", [
    "../../etc/passwd", "not a slug", "SPECS/2026-08-20-plan-room.md",
    "2026-08-20-" + "x" * 80, "",
])
async def test_a_slug_that_is_not_a_slug_is_refused(client, app, index, slug):
    """card_meta is keyed on this string. A key that can contain a slash is a
    key somebody will eventually try to resolve as a path."""
    await make_user("admin", admin=True)
    await login(client, "admin")
    r = await client.post(f"/planroom/cards/{slug}/comment", json={"text": "x"})
    assert r.status_code in (400, 404, 405), r.text


async def test_backlog_and_keyboard_slugs_are_accepted(client, app, index):
    index([card(slug="backlog-12", kind="backlog", column="Backlog"),
           card(slug="keyboard-abc1234def0", kind="keyboard", column="Review")])
    await make_user("admin", admin=True)
    await login(client, "admin")
    for slug in ("backlog-12", "keyboard-abc1234def0"):
        r = await client.post(f"/planroom/cards/{slug}/comment",
                              json={"text": "noted"})
        assert r.status_code == 200, (slug, r.text)


# ── card detail and search ──────────────────────────────────────────────────

async def test_card_detail_carries_everything_comments_included(client, app,
                                                                 index):
    await make_user("admin", admin=True)
    await login(client, "admin")
    await client.post("/planroom/cards/2026-08-20-plan-room/comment",
                      json={"text": "one"})
    body = (await client.get("/planroom/cards/2026-08-20-plan-room")).json()
    assert body["card"]["confirm_seq"] == 1434
    assert body["card"]["review_owner"] == "Claudette"
    assert len(body["comments"]) == 1


async def test_comments_outlive_the_card_leaving_the_board(client, app, index):
    """A superseded spec, a rewritten commit. The comment is a record of who
    said what; it does not evaporate because a derivation blinked."""
    await make_user("admin", admin=True)
    await login(client, "admin")
    await client.post("/planroom/cards/2026-08-20-plan-room/comment",
                      json={"text": "kept"})
    index([])
    body = (await client.get("/planroom/cards/2026-08-20-plan-room")).json()
    assert body["card"] is None
    assert [c["text"] for c in body["comments"]] == ["kept"]


async def test_an_unknown_card_with_no_comments_is_a_404(client, app, index):
    await make_user("reader")
    await login(client, "reader")
    assert (await client.get("/planroom/cards/2026-01-01-nope")).status_code == 404


async def test_search_matches_card_text(client, app, index):
    await make_user("reader")
    await login(client, "reader")
    body = (await client.get("/planroom/search?q=Claudette")).json()
    assert [c["slug"] for c in body["cards"]] == ["2026-08-20-plan-room"]
    assert (await client.get("/planroom/search?q=zzzznope")).json()["cards"] == []


async def test_search_matches_comment_text(client, app, index):
    await make_user("admin", admin=True)
    await login(client, "admin")
    await client.post("/planroom/cards/2026-08-20-plan-room/comment",
                      json={"text": "the gatehouse chown"})
    body = (await client.get("/planroom/search?q=gatehouse chown")).json()
    assert [c["slug"] for c in body["cards"]] == ["2026-08-20-plan-room"]


async def test_the_board_filters(client, app, index):
    index([card(slug="2026-08-01-a", title="a", column="Ready",
                review_owner="Claudette", lane="server"),
           card(slug="2026-08-02-b", title="b", column="Merged",
                review_owner="Gable", lane="harness")])
    await make_user("reader")
    await login(client, "reader")

    async def slugs(qs):
        return [c["slug"] for c in
                (await client.get(f"/planroom/board?{qs}")).json()["cards"]]

    assert await slugs("column=Ready") == ["2026-08-01-a"]
    assert await slugs("owner=gable") == ["2026-08-02-b"]
    assert await slugs("lane=harness") == ["2026-08-02-b"]


async def test_the_counts_cover_every_ruled_column(client, app, index):
    await make_user("reader")
    await login(client, "reader")
    counts = (await client.get("/planroom/board")).json()["counts"]
    assert list(counts) == ["Backlog", "Proposed", "Ready", "Building",
                            "Review", "Merged", "Archived"]


async def test_filters_do_not_change_the_counts(client, app, index):
    """The counts describe the board, not the filter. A column that empties when
    you type in a search box is a board that has just lied about the house."""
    await make_user("reader")
    await login(client, "reader")
    body = (await client.get("/planroom/board?column=Merged")).json()
    assert body["cards"] == []
    assert body["counts"]["Ready"] == 1


# ── the migration ───────────────────────────────────────────────────────────

async def test_the_migration_ships_both_tables(app):
    rows = await db.fetch_all(
        "SELECT name FROM sqlite_master WHERE type='table'")
    names = {r["name"] for r in rows}
    assert {"card_meta", "card_comments"} <= names


async def test_the_schema_refuses_a_blocked_card_with_no_reason(app):
    """Enforced in the table, not only at the router: the broker writes through
    the router, but the next writer might not."""
    with pytest.raises(Exception):
        await db.execute(
            "INSERT INTO card_meta (slug, blocked) VALUES ('x', 1)")


async def test_card_meta_is_keyed_on_slug_alone(app):
    """slug == branch == unit == sidecar key (P5). Comments keyed on a movable
    path are orphans waiting to happen, so the key is not a path."""
    rows = await db.fetch_all("PRAGMA table_info(card_meta)")
    pk = [r["name"] for r in rows if r["pk"]]
    assert pk == ["slug"]


# ── the reject button (Phase II slice A, seq 1625) ──────────────────────────
#
# The button and `/backlog reject <id>` are ONE write reached two ways. These
# tests are about the second way: that the slug resolves to the right row, that
# the gate is the same gate, and that the board does not pretend the card moved.

BACKLOG_CARD = dict(slug="backlog-1", kind="backlog", column="Backlog",
                    title="add a gif picker", spec_path=None, status=None,
                    where="backlog row 1, filed by alice")


async def file_backlog_row(text: str = "add a gif picker") -> int:
    cur = await db.execute(
        "INSERT INTO backlog (text, author, created_at) VALUES (?, ?, ?)",
        (text, "alice", db.utc_now()))
    return cur.lastrowid


async def backlog_row(item_id: int = 1) -> dict:
    got = await db.fetch_one("SELECT * FROM backlog WHERE id = ?", (item_id,))
    assert got is not None
    return got


async def test_the_button_writes_the_row_through(client, app, index):
    index([card(**BACKLOG_CARD)])
    await file_backlog_row()
    alice = await make_user("alice")
    await login(client, "alice")

    r = await client.post("/planroom/cards/backlog-1/status",
                          json={"status": "rejected"})
    assert r.status_code == 200, r.text

    row = await backlog_row()
    assert row["status"] == "rejected"
    assert row["status_by_type"] == "user" and row["status_by_id"] == alice
    assert row["status_at"] is not None
    assert r.json()["item"]["status"] == "rejected"


async def test_the_button_does_not_move_the_card(client, app, index):
    """The Backlog column is DERIVED. The response returns the card as it still
    stands and says the tick will move it — a UI that asserted the move would be
    the forked truth this whole feature exists to prevent."""
    index([card(**BACKLOG_CARD)])
    await file_backlog_row()
    await make_user("alice")
    await login(client, "alice")

    r = await client.post("/planroom/cards/backlog-1/status",
                          json={"status": "rejected"})
    assert r.json()["card"]["column"] == "Backlog"
    assert "derivation tick" in r.json()["note"]

    # And the board still shows it, because nothing has re-derived yet.
    board = (await client.get("/planroom/board")).json()
    assert [c["slug"] for c in board["cards"]] == ["backlog-1"]


async def test_the_button_and_the_slash_command_are_one_write_path(
        client, app, index):
    """Same function, so the same refusal — checked by making the service raise
    and watching the endpoint carry it, rather than by reimplementing it."""
    index([card(**BACKLOG_CARD)])
    await file_backlog_row()
    await make_user("alice")
    await login(client, "alice")

    r = await client.post("/planroom/cards/backlog-1/status",
                          json={"status": "spec'd"})
    assert r.status_code == 400
    assert "needs the spec slug" in r.json()["detail"]
    assert (await backlog_row())["status"] == "open"

    r = await client.post("/planroom/cards/backlog-1/status",
                          json={"status": "spec'd",
                                "spec_ref": "2026-08-23-db-write-lock"})
    assert r.status_code == 200, r.text
    assert (await backlog_row())["spec_ref"] == "2026-08-23-db-write-lock"


async def test_a_bot_is_refused_by_the_button_too(client, app, index):
    """The gate is server-side and lives in the write path, so it cannot be
    true for chat and stale for the UI. Note this is the opposite of the other
    write endpoints here, where a bot IS a legitimate writer — because triage is
    a human act and board-native state is not."""
    index([card(**BACKLOG_CARD)])
    await file_backlog_row()
    await make_bot()

    r = await client.post("/planroom/cards/backlog-1/status",
                          json={"status": "rejected"},
                          headers={"X-Api-Key": BOT_KEY})
    assert r.status_code == 403, r.text
    assert (await backlog_row())["status"] == "open"


async def test_a_non_admin_human_may_reject(client, app, index):
    """The gate is signed-in-human, not admin — the client greys nothing the
    server would have allowed."""
    index([card(**BACKLOG_CARD)])
    await file_backlog_row()
    await make_user("bob")  # not an admin
    await login(client, "bob")

    r = await client.post("/planroom/cards/backlog-1/status",
                          json={"status": "rejected"})
    assert r.status_code == 200, r.text


async def test_the_button_refuses_a_spec_card(client, app, index):
    """A spec's Status line lives in git and has no write path anywhere. The
    endpoint refuses rather than inventing one."""
    await make_user("admin", admin=True)
    await login(client, "admin")
    r = await client.post("/planroom/cards/2026-08-20-plan-room/status",
                          json={"status": "rejected"})
    assert r.status_code == 400
    assert "SPECS/" in r.json()["detail"]


async def test_the_button_refuses_a_keyboard_card(client, app, index):
    index([card(slug="keyboard-abc1234def0", kind="keyboard", column="Review")])
    await make_user("admin", admin=True)
    await login(client, "admin")
    r = await client.post("/planroom/cards/keyboard-abc1234def0/status",
                          json={"status": "rejected"})
    assert r.status_code == 400


async def test_the_button_refuses_a_card_not_on_the_board(client, app, index):
    """A slug whose row exists but whose card does not: you cannot press a
    button on a card that is not there."""
    await file_backlog_row()
    await make_user("alice")
    await login(client, "alice")
    r = await client.post("/planroom/cards/backlog-1/status",
                          json={"status": "rejected"})
    assert r.status_code == 404
    assert (await backlog_row())["status"] == "open"


async def test_the_button_refuses_a_missing_row(client, app, index):
    """The card is on the board but the row is gone — the index is a cache and
    may be a tick out of date. The write path is the one that decides."""
    index([card(**BACKLOG_CARD)])
    await make_user("alice")
    await login(client, "alice")
    r = await client.post("/planroom/cards/backlog-1/status",
                          json={"status": "rejected"})
    assert r.status_code == 404
    assert "no backlog #1" in r.json()["detail"]


async def test_the_button_refuses_an_invented_status(client, app, index):
    index([card(**BACKLOG_CARD)])
    await file_backlog_row()
    await make_user("alice")
    await login(client, "alice")
    for bad in ("open", "merged", ""):
        r = await client.post("/planroom/cards/backlog-1/status",
                              json={"status": bad})
        assert r.status_code == 400, (bad, r.text)
    assert (await backlog_row())["status"] == "open"
