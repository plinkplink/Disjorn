"""What every admin password reset writes besides the new hash, shared by the
HTTP route and cli.py so the two cannot drift."""

from typing import Optional

from .. import db


async def note_password_reset(user_id: int, by_user_id: Optional[int]) -> None:
    """Call inside the reset's transaction; `by_user_id` is None from the console."""
    await db.execute(
        """UPDATE password_reset_requests
           SET status = 'resolved', resolved_at = ?, resolved_by = ?
           WHERE user_id = ? AND status = 'open'""",
        (db.utc_now(), by_user_id, user_id),
        commit=False,
    )
