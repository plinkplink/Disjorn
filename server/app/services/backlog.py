"""The one write path for backlog status, and its human-only gate.

Writes the row only: the Plan Room derives its board from it. spec_ref holds a slug, never a path.
"""

import re
from typing import Any, Optional

from .. import db

STATUS_VERBS: dict[str, str] = {
    "reject": "rejected",
    "duplicate": "duplicate",
    "spec'd": "spec'd",
    "built": "built",
}

# Deliberately narrower than the table's CHECK: no verb sets `open`.
SETTABLE = frozenset(STATUS_VERBS.values())

# Anchored, with no path separators: a stored slug must not look like a path.
SPEC_SLUG_RE = re.compile(r"^\d{4}-\d{2}-\d{2}-[a-z0-9][a-z0-9-]{0,50}$")


class BacklogStatusError(Exception):
    """A refused status change; the message goes to the caller."""

    def __init__(self, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.status_code = status_code


def _check_human(actor_type: str) -> None:
    """Bots are refused server-side; a greyed button is only UX.

    Filing a request is open to anything in the house. Ruling on one is a human act:
    a resident that could reject its own items would make the record meaningless.
    """
    if actor_type != "user":
        raise BacklogStatusError(
            "Only a signed-in person can change a backlog item's status. "
            "Bots file and read the backlog; the triage verbs are a human act.",
            status_code=403,
        )


def _check_spec_ref(status: str, spec_ref: Optional[str]) -> Optional[str]:
    """`spec'd` requires a slug, or the status names a spec it cannot identify."""
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
    """Change one row's status and return the row. Any status may replace any other.

    The read and the write share one transaction.
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
            # COALESCE: built/rejected must not blank an existing spec_ref, the only pointer back.
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
