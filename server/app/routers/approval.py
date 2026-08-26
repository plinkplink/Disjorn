"""Approval object — one record per proposal, answered from either seat.

SPECS/2026-08-26-approval-object-and-resident-write-verbs.md (confirmed by
plink, #custodian seq 2022). Slice A: the table and the endpoints. No client.

| method | path                          | who                  |
|--------|-------------------------------|----------------------|
| GET    | /approval/proposals           | anyone authenticated |
| GET    | /approval/proposals/{id}      | anyone authenticated |
| POST   | /approval/proposals           | admin, bot           |
| POST   | /approval/proposals/{id}/act  | admin, bot           |

ONE STATE OF RECORD. plink answers from a client modal (slice B); residents
answer through the broker's `approval-list` / `approval-show` / `approval-act`
verbs, which arrive here as the broker's own bot identity. Both write the same
`approval_state` row. A second store for "what the residents said" would be the
forked truth the object exists to prevent.

THE SURFACE SHIPS OFF. Every endpoint below, read and write alike, refuses with
503 while `APPROVAL_ENABLED` is false, and the refusal names the setting: a
disarmed surface and an empty one must not read alike, and that text is what the
broker repeats verbatim to a resident who asks. Arming it is a witnessed plink
config change, not something this build does.

THE DECISION IS DERIVED, NEVER STORED (`_decision`). There is no column for it,
so there is nothing for a client, a verb or a stray UPDATE to set out of step
with the principals' own answers. `closed_at` is the single written consequence,
computed from the same function inside the acting transaction; there is no close
endpoint.
"""

from typing import Annotated, Literal, Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from .. import db
from ..config import get_settings
from .auth import Actor, get_actor

router = APIRouter()

CurrentActor = Annotated[Actor, Depends(get_actor)]

MAX_REMARKS_CHARS = 4000
MAX_SLUG_CHARS = 80
MAX_TITLE_CHARS = 200
MAX_TEXT_CHARS = 20000

# A handle, not a path: a spec slug or any short kebab word. Anchored and
# separator-free, because `slug` is what a resident types at the broker to name
# a proposal and what the client will eventually put in a URL.
SLUG_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._-]*$"

ACTIONS = ("approve", "deny", "rework")


# ── the arming gate ─────────────────────────────────────────────────────────

def _require_enabled() -> None:
    """503 while the surface is disarmed. Reads included, deliberately.

    Gating reads too is what keeps "off" from looking like "nobody has filed
    anything". The detail is carried back to a resident verbatim by the broker,
    so it has to say what is wrong and what would fix it."""
    if not get_settings().APPROVAL_ENABLED:
        raise HTTPException(
            status_code=503,
            detail="The approval surface is not enabled on this server: "
                   "APPROVAL_ENABLED is false. Arming it is a witnessed plink "
                   "config change — set APPROVAL_ENABLED=true in the server's "
                   "environment and restart.")


def _require_writer(actor: Actor) -> None:
    """Admin or bot may file and answer; anyone authenticated may read.

    The bot branch is the broker, which is how every resident reaches this
    object. The client hiding a button is never the wall — this is."""
    if actor.type == "bot":
        return
    if actor.user is not None and actor.user.is_admin:
        return
    raise HTTPException(status_code=403,
                        detail="Only admins and bots can file or answer "
                               "approval proposals")


def _actor_label(actor: Actor, supplied: Optional[str]) -> str:
    """Who a write is attributed to — same rule as planroom.py:_actor_label.

    A human is themselves; `author` is ignored outright, because letting a
    signed-in person label their own act with someone else's name is a forgery
    affordance. A bot may supply a label — the broker stamps the calling
    resident there from SO_PEERCRED, never from the resident's arguments — and
    the bot's own identity stays on the row underneath it."""
    if actor.type == "user":
        return (actor.user.display_name or actor.user.username) if actor.user else "user"
    name = (actor.bot.name if actor.bot else "bot")
    return f"{supplied} (via {name})" if supplied else name


# ── the object ──────────────────────────────────────────────────────────────

def _decision(states: list[dict]) -> str:
    """Approved / denied / rework / pending, computed on every read.

    DERIVED, NEVER STORED: there is no column for this, so nothing can set it
    directly and no row can disagree with the principals it is made of. Denial
    outranks rework outranks pending — a single deny is an answer even while
    others are still thinking, and rework is a distinct answer rather than a
    soft denial, so it never closes anything."""
    values = [s["state"] for s in states]
    if any(v == "deny" for v in values):
        return "denied"
    if any(v == "rework" for v in values):
        return "rework"
    if values and all(v == "approve" for v in values):
        return "approved"
    return "pending"


def _attribution(row: dict, prefix: str) -> Optional[dict]:
    kind = row[f"{prefix}_type"]
    if kind is None:
        return None
    return {"type": kind, "id": row[f"{prefix}_id"],
            "label": row[f"{prefix}_label"]}


def _order_states(rows: list[dict]) -> list[dict]:
    """Configured principal order first, then anything else the row set holds.

    The extras are not noise: the configured list can change, and a proposal
    keeps the principals it was filed with (config.py). Dropping them here
    would hide an answer somebody actually gave."""
    configured = get_settings().approval_principals
    by_principal = {r["principal"]: r for r in rows}
    ordered = [by_principal[p] for p in configured if p in by_principal]
    known = set(configured)
    ordered += [r for r in rows if r["principal"] not in known]
    return ordered


async def _compose(proposal: dict) -> dict:
    """The one JSON shape all three endpoints answer with."""
    rows = await db.fetch_all(
        "SELECT principal, state, remarks, acted_by_type, acted_by_id, "
        "acted_by_label, acted_at FROM approval_state WHERE proposal_id = ? "
        "ORDER BY principal", (proposal["id"],))
    states = _order_states(rows)
    return {
        "id": proposal["id"],
        "slug": proposal["slug"],
        "title": proposal["title"],
        "text": proposal["text"],
        "created_by": {"type": proposal["created_by_type"],
                       "id": proposal["created_by_id"],
                       "label": proposal["created_by_label"]},
        "created_at": proposal["created_at"],
        "closed_at": proposal["closed_at"],
        "decision": _decision(states),
        "states": [{"principal": s["principal"], "state": s["state"],
                    "remarks": s["remarks"],
                    "acted_by": _attribution(s, "acted_by"),
                    "acted_at": s["acted_at"]} for s in states],
    }


async def _require_proposal(proposal_id: int) -> dict:
    row = await db.fetch_one("SELECT * FROM approval_proposal WHERE id = ?",
                             (proposal_id,))
    if row is None:
        raise HTTPException(status_code=404, detail="No such approval proposal")
    return row


# ── reads ───────────────────────────────────────────────────────────────────

@router.get("/approval/proposals")
async def list_proposals(
    actor: CurrentActor,
    state: Optional[Literal["open", "closed"]] = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
) -> dict:
    """Most recent first. `state` filters on whether the proposal is closed."""
    _require_enabled()
    where = ""
    if state == "open":
        where = "WHERE closed_at IS NULL"
    elif state == "closed":
        where = "WHERE closed_at IS NOT NULL"
    rows = await db.fetch_all(
        f"SELECT * FROM approval_proposal {where} ORDER BY id DESC LIMIT ?",
        (limit + 1,))
    truncated = len(rows) > limit
    rows = rows[:limit]
    proposals = [await _compose(r) for r in rows]
    return {"proposals": proposals, "count": len(proposals),
            "truncated": truncated}


@router.get("/approval/proposals/{proposal_id}")
async def get_proposal(proposal_id: int, actor: CurrentActor) -> dict:
    _require_enabled()
    return {"proposal": await _compose(await _require_proposal(proposal_id))}


# ── writes ──────────────────────────────────────────────────────────────────

class ProposalIn(BaseModel):
    slug: str = Field(min_length=1, max_length=MAX_SLUG_CHARS,
                      pattern=SLUG_PATTERN)
    title: str = Field(min_length=1, max_length=MAX_TITLE_CHARS)
    text: str = Field(min_length=1, max_length=MAX_TEXT_CHARS)
    # Only honoured for a BOT caller, and only as the bot's attestation of who
    # asked it. A human's proposal is their own, always.
    author: Optional[str] = Field(default=None, max_length=100)


class ActIn(BaseModel):
    principal: str = Field(min_length=1, max_length=100)
    action: Literal["approve", "deny", "rework"]
    remarks: Optional[str] = Field(default=None, max_length=MAX_REMARKS_CHARS)


@router.post("/approval/proposals")
async def create_proposal(actor: CurrentActor,
                          body: ProposalIn = Body(...)) -> dict:
    """File a proposal, with a pending row for every configured principal.

    The state rows are written HERE, not on first answer, and that is the whole
    reason the object can be read at a glance: an unanswered principal is a
    `pending` row rather than an absent one, so nobody has to work out whether
    silence means "has not looked" or "is not being asked"."""
    _require_enabled()
    _require_writer(actor)
    existing = await db.fetch_one(
        "SELECT id FROM approval_proposal WHERE slug = ?", (body.slug,))
    if existing is not None:
        raise HTTPException(
            status_code=409,
            detail=f"An approval proposal with that slug already exists: "
                   f"id {existing['id']}")
    label = _actor_label(actor, body.author)
    principals = get_settings().approval_principals
    async with db.transaction() as conn:
        cur = await conn.execute(
            "INSERT INTO approval_proposal (slug, title, text, "
            "created_by_type, created_by_id, created_by_label) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (body.slug, body.title.strip(), body.text, actor.type, actor.id,
             label))
        proposal_id = cur.lastrowid
        for principal in principals:
            await conn.execute(
                "INSERT INTO approval_state (proposal_id, principal) "
                "VALUES (?, ?)", (proposal_id, principal))
    return {"proposal": await _compose(await _require_proposal(proposal_id))}


@router.post("/approval/proposals/{proposal_id}/act")
async def act_on_proposal(proposal_id: int, actor: CurrentActor,
                          body: ActIn = Body(...)) -> dict:
    """One principal's answer: approve, deny or rework, with remarks.

    WHO MAY ANSWER AS WHOM is the load-bearing rule. A signed-in human answers
    as themselves and nobody else — the client modal is plink's seat, not a way
    to answer for a resident. A bot may name any configured principal, because
    the broker stamps it from the connecting seat's SO_PEERCRED exactly as
    `board-flag` stamps its author, and this process cannot re-derive that.

    Re-acting replaces that principal's row (primary key, not an append log): a
    principal may change its mind while the proposal is open, and the new
    `acted_at` is when they did."""
    _require_enabled()
    _require_writer(actor)
    proposal = await _require_proposal(proposal_id)
    principals = get_settings().approval_principals
    if body.principal not in principals:
        raise HTTPException(
            status_code=400,
            detail=f"'{body.principal}' is not a principal on this server. "
                   f"Valid principals: {', '.join(principals)}.")
    if actor.type == "user":
        username = actor.user.username if actor.user else None
        if body.principal != username:
            raise HTTPException(
                status_code=403,
                detail=f"You can only answer as yourself ({username}), not as "
                       f"{body.principal}.")
    if proposal["closed_at"] is not None:
        raise HTTPException(
            status_code=409,
            detail="That approval proposal is closed; its decision is already "
                   "on the record.")

    label = _actor_label(actor, None)
    now = db.utc_now()
    # Answer and consequence in ONE transaction: closed_at is derived from the
    # rows this statement writes, so a reader must never see the new answer
    # without the closure it implies.
    async with db.transaction() as conn:
        await conn.execute(
            "INSERT INTO approval_state (proposal_id, principal, state, "
            "remarks, acted_by_type, acted_by_id, acted_by_label, acted_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(proposal_id, principal) DO UPDATE SET "
            "state = excluded.state, remarks = excluded.remarks, "
            "acted_by_type = excluded.acted_by_type, "
            "acted_by_id = excluded.acted_by_id, "
            "acted_by_label = excluded.acted_by_label, "
            "acted_at = excluded.acted_at",
            (proposal_id, body.principal, body.action, body.remarks,
             actor.type, actor.id, label, now))
        rows = await db.fetch_all(
            "SELECT state FROM approval_state WHERE proposal_id = ?",
            (proposal_id,))
        if _decision(rows) in ("approved", "denied"):
            await conn.execute(
                "UPDATE approval_proposal SET closed_at = ? WHERE id = ?",
                (now, proposal_id))
    return {"proposal": await _compose(await _require_proposal(proposal_id))}
