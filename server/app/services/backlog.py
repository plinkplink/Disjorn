"""The backlog row's status, and the ONE function that changes it.

SPECS/2026-08-23-plan-room-phase2-slice-a.md (confirmed by plink, #custodian
seq 1625).

Two callers, one write path:

  * ``/backlog reject|duplicate|spec'd|built <id>`` in chat (routers/slash.py)
  * the reject button on a Backlog-column card (routers/planroom.py)

They are the same write, reached two ways, and this module is the only place
that write exists. That is not tidiness — it is where the gate lives. If the
human check sat in each router instead, there would be two of it, and two of a
rule is one of them drifting. A caller cannot reach the UPDATE without passing
the guard, because the guard is inside the function that does the UPDATE.

WHAT THIS IS NOT: a write to derived state. A backlog row IS the artifact — the
Plan Room's Backlog column is *derived from* `status = 'open'`, broker-side, and
nothing here touches a card, a column or an index. The row moves; the board
notices at the next derivation tick and announces it like every other
transition. That ordering is the feature: the command edits the artifact and the
machinery says so, rather than the UI asserting a state and hoping the artifact
catches up.

`spec_ref` stores the spec SLUG, never a path (Claudette #1619). A card's
identity is already its slug, the path is derivable from it, and a stored path
is wrong the day `SPECS/` is reorganised. Resolution to a path happens at read
time, where a stale answer costs a broken link instead of a broken row.
"""

import re
from typing import Any, Optional

from .. import db

# verb typed in chat -> status written to the row. Only `reject` differs from
# its status, because "reject" is what a person does and "rejected" is what the
# row then is; the other three read the same in both moods.
STATUS_VERBS: dict[str, str] = {
    "reject": "rejected",
    "duplicate": "duplicate",
    "spec'd": "spec'd",
    "built": "built",
}

# The statuses this write path can set. Deliberately NOT the full CHECK set:
# there is no verb that returns a row to `open`, because plink named four verbs
# and a fifth with no request behind it is vocabulary nobody asked for.
SETTABLE = frozenset(STATUS_VERBS.values())

# A spec slug — the date-prefixed form, the same shape planroom.SLUG_RE accepts
# for a spec card and the same shape the branch name and the systemd unit use.
# Anchored and free of path separators on purpose: the whole point of storing a
# slug is that it is not a path, so a value that could be mistaken for one must
# not get in.
SPEC_SLUG_RE = re.compile(r"^\d{4}-\d{2}-\d{2}-[a-z0-9][a-z0-9-]{0,50}$")


class BacklogStatusError(Exception):
    """A status change that was refused, with the sentence to show the caller.

    ``status_code`` is the HTTP code the Plan Room endpoint answers with; the
    chat path ignores it and posts ``str(exc)`` as the reply. One refusal, two
    renderings — the same reason either way.
    """

    def __init__(self, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.status_code = status_code


def _check_human(actor_type: str) -> None:
    """Bots are refused here, server-side, and that is the whole gate.

    Same shape as the authenticated-human guard on a build: filing a request is
    something anything in the house may do, but *ruling on* one is a human act.
    A resident that could reject its own backlog items would be marking its own
    homework, and the record would stop meaning anything.

    A greyed button is UX. This is the enforcement.
    """
    if actor_type != "user":
        raise BacklogStatusError(
            "Only a signed-in person can change a backlog item's status. "
            "Bots file and read the backlog; the triage verbs are a human act.",
            status_code=403,
        )


def _check_spec_ref(status: str, spec_ref: Optional[str]) -> Optional[str]:
    """`spec'd` implies a pointer, and the pointer is a slug.

    Shipping the verb without the field would leave a status with no referent —
    a row that claims a spec exists and cannot say which one (Claudette #1615).
    So the two move together or neither does.
    """
    ref = (spec_ref or "").strip()
    if status != "spec'd":
        if ref:
            raise BacklogStatusError(
                f"A spec slug only belongs on a `spec'd` write, not on `{status}`.")

        return None
    if not ref:
        raise BacklogStatusError(
            "`spec'd` needs the spec slug it refers to — a status that says a "
            "spec exists without saying which one is not a record of anything. "
            "Try: /backlog spec'd <id> 2026-08-23-some-spec")
    if "/" in ref or ref.endswith(".md"):
        raise BacklogStatusError(
            f"`{ref}` is a path, not a slug. The slug is the filename stem — "
            "the path is derivable from it, and a stored path breaks the day "
            "SPECS/ is reorganised. Try: /backlog spec'd <id> "
            "2026-08-23-some-spec")
    if not SPEC_SLUG_RE.match(ref):
        raise BacklogStatusError(
            f"`{ref}` is not a spec slug. A slug looks like "
            "`2026-08-23-some-spec`: an ISO date, then lowercase words joined "
            "by hyphens.")
    return ref


async def set_status(
    item_id: int,
    status: str,
    *,
    actor_type: str,
    actor_id: int,
    spec_ref: Optional[str] = None,
) -> dict[str, Any]:
    """Change one backlog row's status. Returns the row as it now stands.

    Raises :class:`BacklogStatusError` for every refusal — the caller renders it
    (a chat reply, or an HTTP error with ``exc.status_code``).

    Any of the four statuses may replace any other, including one already set.
    That is deliberate and it is the case that motivated `duplicate` existing at
    all: row 3 was marked `rejected` for want of a better word, and a triage
    table you cannot correct is one that accumulates wrong answers. There is no
    transition matrix here because there is no transition that would be a lie —
    the row records what somebody last decided, with their name on it.

    The read and the write share one transaction, so the "no such item" answer
    cannot be raced by a concurrent write, and the status, the spec_ref and the
    attribution land in a single statement — a row is never briefly `spec'd`
    with nobody's name on it or no spec to point at.
    """
    _check_human(actor_type)
    if status not in SETTABLE:
        raise BacklogStatusError(
            f"`{status}` is not a status this can set "
            f"({', '.join(sorted(SETTABLE))}).")
    ref = _check_spec_ref(status, spec_ref)

    async with db.transaction():
        row = await db.fetch_one("SELECT id FROM backlog WHERE id = ?", (item_id,))
        if row is None:
            raise BacklogStatusError(
                f"There is no backlog #{item_id}. `/backlog` lists what there is.",
                status_code=404,
            )
        await db.execute(
            # COALESCE, not a plain assignment: a `built` or `rejected` write
            # leaves an existing spec_ref alone. The spec a row was drafted into
            # is still the spec it was drafted into after somebody rejects it,
            # and blanking that would destroy the only pointer back.
            "UPDATE backlog SET status = ?, spec_ref = COALESCE(?, spec_ref), "
            "status_by_type = ?, status_by_id = ?, status_at = ? WHERE id = ?",
            (status, ref, actor_type, actor_id, db.utc_now(), item_id),
            commit=False,
        )
        updated = await db.fetch_one(
            "SELECT id, text, author, created_at, status, spec_ref, "
            "status_by_type, status_by_id, status_at FROM backlog WHERE id = ?",
            (item_id,),
        )
    assert updated is not None  # written inside the transaction we just committed
    return updated
