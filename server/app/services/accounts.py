"""What every admin password reset writes besides the new hash, shared by the
HTTP route and cli.py so the two cannot drift."""

from typing import Optional

from .. import db

CONSOLE_ACTOR = "an admin at the server console"


async def note_password_reset(
    user_id: int, by_user_id: Optional[int], by_name: str
) -> None:
    """Call inside the reset's transaction; `by_user_id` is None from the console."""
    now = db.utc_now()
    await db.execute(
        """UPDATE password_reset_requests
           SET status = 'resolved', resolved_at = ?, resolved_by = ?
           WHERE user_id = ? AND status = 'open'""",
        (now, by_user_id, user_id),
        commit=False,
    )
    await db.execute(
        """INSERT INTO account_events (user_id, kind, actor_user_id, actor_name, created_at)
           VALUES (?, 'password_reset', ?, ?, ?)""",
        (user_id, by_user_id, by_name, now),
        commit=False,
    )


async def take_reset_notice(user_id: int) -> Optional[dict[str, str]]:
    """The newest reset this account has not been shown, marked shown."""
    row = await db.fetch_one(
        """SELECT actor_name, created_at FROM account_events
           WHERE user_id = ? AND kind = 'password_reset' AND seen_at IS NULL
           ORDER BY id DESC LIMIT 1""",
        (user_id,),
    )
    if row is None:
        return None
    await db.execute(
        """UPDATE account_events SET seen_at = ?
           WHERE user_id = ? AND kind = 'password_reset' AND seen_at IS NULL""",
        (db.utc_now(), user_id),
    )
    return {"by": row["actor_name"], "at": row["created_at"]}
