"""Approval object: one record per proposal, answered from either seat.

SPECS/2026-08-26-approval-object-and-resident-write-verbs.md, slice A.

| method | path                          | who                  |
|--------|-------------------------------|----------------------|
| GET    | /approval/proposals           | anyone authenticated |
| GET    | /approval/proposals/{id}      | anyone authenticated |
| POST   | /approval/proposals           | admin, bot           |
| POST   | /approval/proposals/{id}/act  | admin as self, relay |

plink's modal and the residents' broker verbs write the same row: a second
store would be forked truth. The decision is derived on every read, never
stored; `closed_at` is its one written consequence.
"""

import re
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

# Typed at the broker and carried in a URL, so anchored and separator-free.
SLUG_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._-]*$"

ACTIONS = ("approve", "deny", "rework")

# A resident seat's principal name. Anything else configured is a person.
RESIDENT_PRINCIPAL_RE = re.compile(r"^res-[a-z][a-z0-9-]*$")


# ── the arming gate ─────────────────────────────────────────────────────────

def _require_enabled() -> None:
    """503 while disarmed, reads included, so off never reads as empty. The
    broker relays the detail verbatim, so it names the fix."""
    if not get_settings().APPROVAL_ENABLED:
        raise HTTPException(
            status_code=503,
            detail="The approval surface is not enabled on this server: "
                   "APPROVAL_ENABLED is false. Arming it is a witnessed plink "
                   "config change — set APPROVAL_ENABLED=true in the server's "
                   "environment and restart.")


def _require_writer(actor: Actor) -> None:
    """Admin or bot may write; anyone authenticated may read."""
    if actor.type == "bot":
        return
    if actor.user is not None and actor.user.is_admin:
        return
    raise HTTPException(status_code=403,
                        detail="Only admins and bots can file or answer "
                               "approval proposals")


def _actor_label(actor: Actor, supplied: Optional[str]) -> str:
    """planroom.py's rule: a person is always themselves (a supplied name
    would be a forgery affordance); a bot's label is an attestation, kept
    beside its own name."""
    if actor.type == "user":
        return (actor.user.display_name or actor.user.username) if actor.user else "user"
    name = (actor.bot.name if actor.bot else "bot")
    return f"{supplied} (via {name})" if supplied else name


# ── the object ──────────────────────────────────────────────────────────────

def _decision(states: list[dict]) -> str:
    """Deny outranks rework outranks pending. Rework is its own answer, not a
    soft deny, so it never closes a proposal."""
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
    """Configured order first, then principals the config no longer names: a
    proposal keeps the principals it was filed with, and so do their answers."""
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
    # Honoured for a bot only; a person's proposal is always their own.
    author: Optional[str] = Field(default=None, max_length=100)


class ActIn(BaseModel):
    principal: Optional[str] = Field(default=None, min_length=1, max_length=100)
    action: Literal["approve", "deny", "rework"]
    remarks: Optional[str] = Field(default=None, max_length=MAX_REMARKS_CHARS)


@router.post("/approval/proposals")
async def create_proposal(actor: CurrentActor,
                          body: ProposalIn = Body(...)) -> dict:
    """File a proposal with a pending row per configured principal, so an
    unanswered principal is `pending`, never absent."""
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


def _acting_principal(actor: Actor, named: Optional[str]) -> str:
    """Who an act answers as. A person is only themselves. Only a relay bot
    may name a principal (the broker stamps it from SO_PEERCRED), and only a
    resident's: a relayed act is never a person's."""
    settings = get_settings()
    principals = settings.approval_principals
    if actor.type == "user":
        me = actor.user.username if actor.user else ""
        if named is not None and named != me:
            raise HTTPException(
                status_code=403,
                detail=f"You can only answer as yourself ({me}), not as "
                       f"{named}.")
        if me not in principals:
            raise HTTPException(
                status_code=403,
                detail=f"{me} is not a principal on this server.")
        return me
    bot_name = actor.bot.name if actor.bot else ""
    if bot_name not in settings.APPROVAL_RELAY_BOT_NAMES:
        raise HTTPException(
            status_code=403,
            detail=f"Bot {bot_name!r} is not an approval relay "
                   f"(APPROVAL_RELAY_BOT_NAMES); only the relay answers for a "
                   f"resident.")
    if not named:
        raise HTTPException(status_code=400,
                            detail="The relay must name the resident it "
                                   "answers for.")
    if named not in principals:
        raise HTTPException(
            status_code=400,
            detail=f"'{named}' is not a principal on this server. "
                   f"Valid principals: {', '.join(principals)}.")
    if not RESIDENT_PRINCIPAL_RE.match(named):
        raise HTTPException(
            status_code=403,
            detail=f"A relayed answer cannot be {named}'s: a person answers "
                   f"from their own session.")
    return named


@router.post("/approval/proposals/{proposal_id}/act")
async def act_on_proposal(proposal_id: int, actor: CurrentActor,
                          body: ActIn = Body(...)) -> dict:
    """One principal's answer, as `_acting_principal` derives it. Re-acting
    replaces that principal's row: a mind may change while the proposal is
    open, and `acted_at` says when."""
    _require_enabled()
    _require_writer(actor)
    proposal = await _require_proposal(proposal_id)
    principal = _acting_principal(actor, body.principal)
    if proposal["closed_at"] is not None:
        raise HTTPException(
            status_code=409,
            detail="That approval proposal is closed; its decision is already "
                   "on the record.")

    label = _actor_label(actor, None)
    now = db.utc_now()
    # One transaction, so no reader sees an answer without its closure.
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
            (proposal_id, principal, body.action, body.remarks,
             actor.type, actor.id, label, now))
        rows = await db.fetch_all(
            "SELECT state FROM approval_state WHERE proposal_id = ?",
            (proposal_id,))
        if _decision(rows) in ("approved", "denied"):
            await conn.execute(
                "UPDATE approval_proposal SET closed_at = ? WHERE id = ?",
                (now, proposal_id))
    return {"proposal": await _compose(await _require_proposal(proposal_id))}
