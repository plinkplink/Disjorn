"""Plan Room — the board tab, rendered from the repo (Phase I: tracking).

SPECS/2026-08-20-plan-room.md (confirmed by plink, #custodian seq 1434).

| method | path                              | who        |
|--------|-----------------------------------|------------|
| GET    | /planroom/board                   | anyone     |
| GET    | /planroom/cards/{slug}            | anyone     |
| GET    | /planroom/search?q=               | anyone     |
| POST   | /planroom/cards/{slug}/comment    | admin, bot |
| POST   | /planroom/cards/{slug}/flag       | admin, bot |
| POST   | /planroom/cards/{slug}/archive    | admin      |
| POST   | /planroom/cards/{slug}/order      | admin      |
| POST   | /planroom/cards/{slug}/status     | human      | backlog cards only

THIS ROUTER DOES NOT DERIVE ANYTHING, AND MUST NEVER LEARN HOW. Every card is
a rendering of an artifact that already exists — a `SPECS/` file's Status line,
a confirm seq, a gatehouse branch, a backlog row, deploy provenance — and that
derivation runs BROKER-SIDE (seq 1428 P2), because it needs gatehouse access
(`sudo git --git-dir`) and `brokerd` imports, and this process has neither and
must not grow them. What this router reads is the derived index, and nothing
else. **If this file ever grows its own reader of SPECS/ or the gatehouse, that
is the forked truth the spec exists to prevent — a defect, not a shortcut.**

The index is a CACHE, NEVER A SOURCE: git wins every disagreement and the index
rebuilds from zero. It is opened read-only, per request, so an atomic rebuild
underneath us is invisible. An absent or unreadable index is NOT an error and
NOT silence — the face says `available: false` and says why, because an absent
index and an empty board must not read alike.

WHAT THIS ROUTER DOES OWN — comments, card order, the blocked flag + its
reason, archived. That is the complete list. It is authoritative, it lives in
`card_meta` / `card_comments` (migration 009), it is keyed on the spec slug
(P5), and it survives every rebuild. **Derived state has no write path anywhere
in this house**, which is what makes "residents can only edit board-native
state" a structural fact rather than a promise.

THE ONE ENDPOINT THAT IS NEITHER (Phase II slice A, seq 1625) —
`POST /cards/{slug}/status` writes a BACKLOG ROW, which is not board-native
state and is not derived state: it is the artifact itself. For a backlog card
the DB row IS what the card renders, so the button writes through to it exactly
as `/backlog reject <id>` in chat does — the same function in
services/backlog.py, which is where the human-only gate lives. It still writes
no card, no column and no index; the Backlog column is derived from
`status = 'open'` broker-side, and it notices at the next tick. Read that as the
rule holding, not bending: the write goes to the artifact and the derivation
stays the only thing that moves a card.

NO DRAG-TO-COLUMN. A card changes columns only because reality moved — a write
through to an artifact is how reality moves, and it is still not a hand on the
card. The rest of Phase II (spec lifecycle, diff view, review stamps, merge and
deploy buttons) is separate specs; nothing here should grow toward them.
"""

import logging
import re
import sqlite3
from typing import Annotated, Any, Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from .. import db
from ..config import get_settings
from ..models import User
from ..services import backlog as backlog_service
from .auth import Actor, get_actor, get_admin_user

logger = logging.getLogger(__name__)

router = APIRouter()

CurrentActor = Annotated[Actor, Depends(get_actor)]
AdminUser = Annotated[User, Depends(get_admin_user)]

# Ruled seq 1391 item 1. Left-to-right, and the order is load-bearing.
COLUMNS = ["Backlog", "Proposed", "Ready", "Building", "Review", "Merged",
           "Archived"]

# A card's identity is its spec filename stem, or one of the two prefixed forms
# the derivation mints for cards that have no spec file yet. Anchored, bounded,
# and no path separators: `card_meta` is keyed on this string, and a key that
# can contain a slash is a key somebody will eventually try to resolve as a
# path.
SLUG_RE = re.compile(r"^(?:\d{4}-\d{2}-\d{2}-[a-z0-9][a-z0-9-]{0,50}"
                     r"|backlog-\d{1,12}|keyboard-[0-9a-f]{7,40})$")

MAX_COMMENT_CHARS = 4000
MAX_REASON_CHARS = 500

# A backlog card's slug carries the row id it was derived from — the broker
# mints it as BACKLOG_PREFIX + id in harness/planroom/planroom.py. This is the
# only card kind whose artifact is a row in THIS database, and so the only one
# this router can write through to. Reading the id back out of the slug is not
# derivation: the broker put it there to be read.
BACKLOG_SLUG_RE = re.compile(r"^backlog-(\d{1,12})$")


# ── the index (read-only, per request) ──────────────────────────────────────

def _read_index() -> dict:
    """The derived board. Never raises; an unavailable index is DECLARED.

    Opened fresh per request and read-only. The broker rebuilds by writing a
    temp file and renaming it over this path, so a connection held across a
    rebuild would keep serving the deleted inode — a board that silently stops
    moving is worse than one that says it cannot be read."""
    path = get_settings().planroom_index
    blank_columns = {"columns": COLUMNS}
    if path is None:
        return {"face": {"available": False, "unavailable_reason":
                         "PLANROOM_INDEX is not configured on this server",
                         **blank_columns}, "cards": []}
    if not path.exists():
        return {"face": {"available": False, "unavailable_reason":
                         "the derived index has never been written — the "
                         "broker builds it on refresh-mirror and on its own "
                         "timer", **blank_columns}, "cards": []}
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        try:
            face = {r["key"]: _loads(r["value"])
                    for r in conn.execute("SELECT key, value FROM face")}
            cards = [_loads(r["card_json"]) for r in conn.execute(
                "SELECT card_json FROM cards ORDER BY board_column, position")]
        finally:
            conn.close()
    except (sqlite3.Error, ValueError) as exc:
        logger.warning("planroom index unreadable: %s", exc)
        return {"face": {"available": False,
                         "unavailable_reason": f"index unreadable: {exc}",
                         **blank_columns}, "cards": []}
    face.setdefault("columns", COLUMNS)
    face["available"] = True
    return {"face": face, "cards": [c for c in cards if isinstance(c, dict)]}


def _loads(raw: str) -> Any:
    import json
    return json.loads(raw)


# ── board-native state (authoritative, ours) ────────────────────────────────

async def _meta_by_slug() -> dict:
    rows = await db.fetch_all("SELECT * FROM card_meta")
    return {r["slug"]: r for r in rows}


async def _comment_counts() -> dict:
    rows = await db.fetch_all(
        "SELECT slug, COUNT(*) AS n FROM card_comments GROUP BY slug")
    return {r["slug"]: r["n"] for r in rows}


def _compose(card: dict, meta: Optional[dict], comments: int) -> dict:
    """One derived card plus the board-native state the server owns.

    The `archived` flag is applied HERE and only here. The index never says
    Archived-by-flag: derivation cannot know a board-native fact, and if it
    tried, the flag would be lost on every rebuild. (The index does emit
    Archived for a `superseded` / `abandoned` spec — that IS derived; a
    superseded spec is done, and Merged would claim a merge that never
    happened.)"""
    out = dict(card)
    out["blocked"] = bool(meta["blocked"]) if meta else False
    out["blocked_reason"] = meta["blocked_reason"] if meta else None
    out["blocked_by"] = meta["blocked_by"] if meta else None
    out["blocked_at"] = meta["blocked_at"] if meta else None
    out["archived"] = bool(meta["archived"]) if meta else False
    out["sort_order"] = meta["sort_order"] if meta else None
    out["comment_count"] = comments
    if out["archived"] and out.get("column") == "Merged":
        out["column"] = "Archived"
    # Blocked is a FLAG WITH A REASON, NEVER A COLUMN. A held card keeps its
    # place so everyone can see where it re-enters. Nothing below moves it.
    return out


def _sorted(cards: list) -> list:
    """Board-native order wins where it was set; derived order everywhere else.

    Derived order is whose-move-first (a card waiting on a human sorts above one
    the residents already have). An admin who drags a card is overriding exactly
    that, for exactly that card, so a set `sort_order` sorts ahead of every
    unset one rather than being averaged into them."""
    def key(c: dict):
        col = c.get("column")
        return (COLUMNS.index(col) if col in COLUMNS else len(COLUMNS),
                0 if c.get("sort_order") is not None else 1,
                c.get("sort_order") if c.get("sort_order") is not None else 0,
                c.get("position", 0), c.get("slug", ""))
    return sorted(cards, key=key)


async def _full_board() -> dict:
    index = _read_index()
    meta = await _meta_by_slug()
    counts = await _comment_counts()
    cards = [_compose(c, meta.get(c.get("slug")), counts.get(c.get("slug"), 0))
             for c in index["cards"]]
    return {"face": index["face"], "cards": _sorted(cards)}


def _check_slug(slug: str) -> str:
    if not SLUG_RE.match(slug):
        raise HTTPException(status_code=400, detail="Not a card slug")
    return slug


# ── reads: everyone may read the tab ────────────────────────────────────────

@router.get("/planroom/board")
async def get_board(
    actor: CurrentActor,
    column: Optional[str] = Query(default=None),
    lane: Optional[str] = Query(default=None, max_length=200),
    owner: Optional[str] = Query(default=None, max_length=200),
    blocked: Optional[bool] = Query(default=None),
) -> dict:
    """The whole board: the face, then every card, in column order.

    The face carries the derivation time and the mirror head it derived from.
    Both are always rendered by the client, because the board cannot go stale
    relative to the mirror — it is not a copy of it — but the mirror itself can
    lag, and staleness in this house is declared, never denied."""
    board = await _full_board()
    cards = board["cards"]
    if column:
        cards = [c for c in cards if c.get("column") == column]
    if lane:
        low = lane.lower()
        cards = [c for c in cards if low in (c.get("lane") or "").lower()]
    if owner:
        low = owner.lower()
        cards = [c for c in cards
                 if low in (c.get("review_owner") or "").lower()]
    if blocked is not None:
        cards = [c for c in cards if c["blocked"] is blocked]
    return {"face": board["face"], "cards": cards,
            "counts": {col: sum(1 for c in board["cards"]
                                if c.get("column") == col) for col in COLUMNS}}


@router.get("/planroom/cards/{slug}")
async def get_card(slug: str, actor: CurrentActor) -> dict:
    """One card, everything on it, comments included.

    Comments are returned even when the card itself is not on the board — a
    spec that was superseded, a keyboard card whose commit was rewritten. They
    are a record of who said what, and they outlive the card's appearance."""
    _check_slug(slug)
    board = await _full_board()
    card = next((c for c in board["cards"] if c.get("slug") == slug), None)
    comments = await db.fetch_all(
        "SELECT id, slug, author_type, author_id, author_label, text, "
        "created_at FROM card_comments WHERE slug = ? ORDER BY id", (slug,))
    if card is None:
        if not comments:
            raise HTTPException(status_code=404, detail="No such card")
        return {"card": None, "comments": comments, "face": board["face"],
                "note": "This card is not currently on the board; its comments "
                        "are kept anyway."}
    return {"card": card, "comments": comments, "face": board["face"]}


@router.get("/planroom/search")
async def search_cards(
    actor: CurrentActor,
    q: str = Query(min_length=1, max_length=200),
    limit: int = Query(default=50, ge=1, le=200),
) -> dict:
    """Substring search across a card's visible text and its comments.

    Skim is the default and detail is opt-in — that is the context-budget answer
    to the "tricky part" of the request: a resident should be able to find the
    one card they need without reading the whole board into their window."""
    needle = q.lower()
    board = await _full_board()
    hits = [c for c in board["cards"] if needle in _haystack(c).lower()]
    if len(hits) < limit:
        seen = {c["slug"] for c in hits}
        rows = await db.fetch_all(
            "SELECT DISTINCT slug FROM card_comments WHERE lower(text) LIKE ?",
            (f"%{needle}%",))
        by_slug = {c["slug"]: c for c in board["cards"]}
        for r in rows:
            if r["slug"] not in seen and r["slug"] in by_slug:
                hits.append(by_slug[r["slug"]])
                seen.add(r["slug"])
    return {"face": board["face"], "query": q, "cards": hits[:limit],
            "truncated": len(hits) > limit}


def _haystack(card: dict) -> str:
    bits = [card.get(k) for k in ("slug", "title", "note", "where", "status",
                                  "lane", "review_owner", "builder", "tier",
                                  "requester", "spec_path", "blocked_reason",
                                  "body")]
    bits.append(" ".join(card.get("flags") or []))
    bits.append(" ".join(card.get("shas") or []))
    return "\n".join(str(b) for b in bits if b)


# ── writes: board-native state ONLY ─────────────────────────────────────────
#
# Every endpoint below writes card_meta or card_comments and nothing else.
# There is no endpoint that writes a column, a Status line, a confirm seq or a
# deploy badge, because there is no such thing anywhere in the house: derived
# state has no write path. That is what makes the resident write verbs
# structurally unable to touch it (seq 1428 P1) — not a check, an absence.


class CommentIn(BaseModel):
    text: str = Field(min_length=1, max_length=MAX_COMMENT_CHARS)
    # Only honoured for a BOT caller, and only as the bot's attestation of who
    # asked it. A human's comment is their own, always.
    author: Optional[str] = Field(default=None, max_length=100)


class FlagIn(BaseModel):
    blocked: bool
    reason: Optional[str] = Field(default=None, max_length=MAX_REASON_CHARS)
    author: Optional[str] = Field(default=None, max_length=100)


class ArchiveIn(BaseModel):
    archived: bool


class OrderIn(BaseModel):
    sort_order: Optional[int] = Field(default=None, ge=0, le=100000)


class StatusIn(BaseModel):
    """A backlog row's new status, and the spec slug a `spec'd` write implies.

    The field is the status, not a fixed verb, even though the only button
    shipping today is Reject: this endpoint IS the slash command's write path
    reached from the UI, and narrowing it to one value here would make the two
    callers different write paths that merely look alike. Which values are
    settable, and what `spec'd` requires, is decided once in
    services/backlog.py — this model does not get its own opinion.
    """

    status: str = Field(max_length=40)
    spec_ref: Optional[str] = Field(default=None, max_length=100)


async def _require_card(slug: str) -> dict:
    """A write must name a card that is actually on the board.

    Not decoration: `card_meta` is keyed on a free-form string, and a write path
    that accepts any string is a table that fills with typos nobody can find
    again. If the index is unavailable this refuses too, which is correct — you
    cannot flag a card on a board you cannot read."""
    _check_slug(slug)
    board = await _full_board()
    if board["face"].get("available") is False:
        raise HTTPException(
            status_code=503,
            detail="The Plan Room index is unavailable, so there is no board to "
                   "write to: " + str(board["face"].get("unavailable_reason")))
    card = next((c for c in board["cards"] if c.get("slug") == slug), None)
    if card is None:
        raise HTTPException(status_code=404, detail="No such card")
    return card


def _actor_label(actor: Actor, supplied: Optional[str]) -> str:
    """Who a write is attributed to.

    A human is themselves; `author` is ignored outright, because letting a
    signed-in person label their own comment with someone else's name is a
    forgery affordance and there is no use for it. A bot may supply a label —
    the broker stamps the CALLING RESIDENT there from SO_PEERCRED, never from
    the resident's own arguments — and the bot's identity remains the wall."""
    if actor.type == "user":
        return (actor.user.display_name or actor.user.username) if actor.user else "user"
    name = (actor.bot.name if actor.bot else "bot")
    return f"{supplied} (via {name})" if supplied else name


def _require_writer(actor: Actor) -> None:
    """Admin or bot. Ruled: only admin and bots can edit and move cards.

    Everyone may READ the tab; this is the wall on the other half. The client
    hiding a button is never the wall — the refusal is."""
    if actor.type == "bot":
        return
    if actor.user is not None and actor.user.is_admin:
        return
    raise HTTPException(status_code=403,
                        detail="Only admins and bots can edit cards")


async def _upsert_meta(slug: str, **fields) -> None:
    cols = ", ".join(f"{k} = ?" for k in fields)
    await db.execute(
        f"INSERT INTO card_meta (slug, {', '.join(fields)}, updated_at) "
        f"VALUES (?, {', '.join('?' for _ in fields)}, ?) "
        f"ON CONFLICT(slug) DO UPDATE SET {cols}, updated_at = excluded.updated_at",
        (slug, *fields.values(), db.utc_now(), *fields.values()))


@router.post("/planroom/cards/{slug}/comment")
async def add_comment(slug: str, actor: CurrentActor,
                      body: CommentIn = Body(...)) -> dict:
    _require_writer(actor)
    await _require_card(slug)
    label = _actor_label(actor, body.author)
    cur = await db.execute(
        "INSERT INTO card_comments (slug, author_type, author_id, "
        "author_label, text) VALUES (?, ?, ?, ?, ?)",
        (slug, actor.type, actor.id, label, body.text.strip()))
    row = await db.fetch_one(
        "SELECT id, slug, author_type, author_id, author_label, text, "
        "created_at FROM card_comments WHERE id = ?", (cur.lastrowid,))
    return {"comment": row}


@router.post("/planroom/cards/{slug}/flag")
async def set_flag(slug: str, actor: CurrentActor,
                   body: FlagIn = Body(...)) -> dict:
    """Block or unblock a card, with a reason.

    A reason is REQUIRED to block: a blocked card without one is a card nobody
    can unblock, because nobody can tell what would have to change. The card
    does not move — blocked is a flag, never a column, so a held card keeps its
    place and everyone can see where it re-enters."""
    _require_writer(actor)
    await _require_card(slug)
    reason = (body.reason or "").strip()
    if body.blocked and not reason:
        raise HTTPException(
            status_code=400,
            detail="Blocking a card needs a reason — a card blocked for no "
                   "stated reason is one nobody can unblock.")
    if body.blocked:
        await _upsert_meta(slug, blocked=1, blocked_reason=reason[:MAX_REASON_CHARS],
                           blocked_by=_actor_label(actor, body.author),
                           blocked_at=db.utc_now())
    else:
        await _upsert_meta(slug, blocked=0, blocked_reason=None,
                           blocked_by=None, blocked_at=None)
    return {"card": await _card_state(slug)}


@router.post("/planroom/cards/{slug}/archive")
async def set_archived(slug: str, admin: AdminUser,
                       body: ArchiveIn = Body(...)) -> dict:
    """Archive a merged card. ADMIN ONLY in Phase I (seq 1428 P1).

    Residents write blocked + comments through their two verbs; order and
    archive stay the keyboard's. Archiving is filing, and filing is the
    keyboard's judgement about what it no longer needs to see."""
    card = await _require_card(slug)
    if body.archived and card.get("column") not in ("Merged", "Archived"):
        raise HTTPException(
            status_code=400,
            detail="Only a merged card can be archived — archiving anything "
                   "else would hide work that is still in flight.")
    await _upsert_meta(slug, archived=1 if body.archived else 0,
                       archived_at=db.utc_now() if body.archived else None)
    return {"card": await _card_state(slug)}


@router.post("/planroom/cards/{slug}/order")
async def set_order(slug: str, admin: AdminUser,
                    body: OrderIn = Body(...)) -> dict:
    """Set a card's position within its column. ADMIN ONLY in Phase I.

    Order within a column, and nothing else. This is NOT drag-to-column: a card
    changes columns only because reality moved, and it will stay that way until
    Phase II's write-through ships. `null` hands the card back to the derived
    whose-move-first order."""
    await _require_card(slug)
    await _upsert_meta(slug, sort_order=body.sort_order)
    return {"card": await _card_state(slug)}


@router.post("/planroom/cards/{slug}/status")
async def set_backlog_status(slug: str, actor: CurrentActor,
                             body: StatusIn = Body(...)) -> dict:
    """Write a backlog card's status through to its row. Signed-in humans only.

    THE SAME WRITE AS `/backlog reject <id>` IN CHAT — literally the same
    function (services/backlog.py), not a parallel implementation of it. One
    write path, two callers, so the human-only gate cannot be true on one and
    stale on the other. This handler's whole job is to turn a card slug back
    into the row id it was derived from and to render the refusal; it decides
    nothing about who may write or what may be written.

    BACKLOG CARDS ONLY, and the refusal below is the doctrine rather than a
    convenience. A spec card's status lives in a `SPECS/` file's Status line, in
    git — there is no endpoint that writes it because there is no such thing
    anywhere in this house. A backlog row is different in kind: it is the
    artifact, it lives in this database, and the Backlog column is derived from
    it. So this writes the artifact and stops.

    The card in the response is DELIBERATELY THE OLD ONE — the board still shows
    the row where it was, because the column is derived and derivation runs on
    the broker's own timer. The client says so instead of pretending the card
    moved. A UI that asserted the move would be the forked truth the Plan Room
    spec exists to prevent, arriving as an optimistic update.
    """
    _check_slug(slug)
    match = BACKLOG_SLUG_RE.match(slug)
    if match is None:
        raise HTTPException(
            status_code=400,
            detail="Only a backlog card has a status this board can write. A "
                   "spec's Status line lives in its SPECS/ file, in git, and "
                   "nothing here writes it.")
    card = await _require_card(slug)
    if card.get("kind") != "backlog":
        raise HTTPException(
            status_code=400,
            detail="That card is not a backlog row, so it has no status here "
                   "to write.")
    try:
        item = await backlog_service.set_status(
            int(match.group(1)), body.status,
            actor_type=actor.type, actor_id=actor.id, spec_ref=body.spec_ref)
    except backlog_service.BacklogStatusError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    return {"item": item, "card": card,
            "note": "The row is updated. The card leaves the Backlog column at "
                    "the next derivation tick, and the broker announces it "
                    "like every other transition."}


async def _card_state(slug: str) -> Optional[dict]:
    board = await _full_board()
    return next((c for c in board["cards"] if c.get("slug") == slug), None)
