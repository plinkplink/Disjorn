"""Asking an admin for a password reset, for a house with no outgoing email.

    POST /auth/reset-requests               (open)  202, same answer for any username
    GET  /auth/reset-requests               (ADMIN) open requests, oldest first
    POST /auth/reset-requests/{id}/dismiss  (ADMIN) close one without a reset

A reset through POST /auth/users/{id}/password closes the account's open
request (services/accounts.py).
"""

import asyncio
import logging
from typing import Annotated, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from .. import db
from ..models import User
from ..services import push, throttle
from .auth import get_admin_user

logger = logging.getLogger(__name__)

router = APIRouter()

GENERIC_ANSWER = "If that account exists, an admin has been asked."
NOTE_MAX_LENGTH = 200

_per_ip = throttle.Limiter(3, 3600)
_per_username = throttle.Limiter(3, 3600)

_pending_tasks: set[asyncio.Task] = set()


async def wait_pending() -> None:
    while _pending_tasks:
        await asyncio.gather(*list(_pending_tasks), return_exceptions=True)


class ResetRequestIn(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    note: Optional[str] = Field(default=None, max_length=NOTE_MAX_LENGTH)


class ResetRequestOut(BaseModel):
    id: int
    user_id: int
    username: str
    display_name: str
    note: Optional[str]
    requested_at: str


async def _file_request(username: str, note: Optional[str]) -> None:
    try:
        user = await db.fetch_one(
            "SELECT id, username FROM users WHERE username = ?", (username,)
        )
        if user is None:
            return
        cur = await db.execute(
            "INSERT OR IGNORE INTO password_reset_requests (user_id, note) VALUES (?, ?)",
            (user["id"], note),
        )
        if not cur.rowcount:
            return
        admins = await db.fetch_all(
            "SELECT id FROM users WHERE is_admin = 1 AND id != ?", (user["id"],)
        )
        await push.notify_users(
            [a["id"] for a in admins],
            {
                "title": "Password reset requested",
                "body": f"{user['username']} asked for a password reset",
                "channel_id": None,
                "message_id": None,
                "url": "/settings",
                "kind": "message",
            },
        )
    except Exception:  # noqa: BLE001 — nobody is waiting on this
        logger.exception("filing a reset request failed")


@router.post("/auth/reset-requests", status_code=202)
async def request_reset(body: ResetRequestIn, request: Request):
    """Answers before looking the name up, so timing says nothing about it."""
    name = body.username.strip()
    wait = throttle.attempt(
        [(_per_ip, throttle.client_ip(request)), (_per_username, name.casefold())]
    )
    if wait:
        return JSONResponse(
            status_code=429,
            content={"detail": "Too many requests. Try again later."},
            headers={"Retry-After": str(wait)},
        )
    task = asyncio.create_task(_file_request(name, (body.note or "").strip() or None))
    _pending_tasks.add(task)
    task.add_done_callback(_pending_tasks.discard)
    return {"detail": GENERIC_ANSWER}


@router.get("/auth/reset-requests")
async def list_reset_requests(
    admin: Annotated[User, Depends(get_admin_user)],
) -> list[ResetRequestOut]:
    rows = await db.fetch_all(
        """SELECT r.id, r.user_id, u.username, u.display_name, r.note, r.requested_at
           FROM password_reset_requests r JOIN users u ON u.id = r.user_id
           WHERE r.status = 'open' ORDER BY r.id"""
    )
    return [ResetRequestOut(**r) for r in rows]


@router.post("/auth/reset-requests/{request_id}/dismiss")
async def dismiss_reset_request(
    request_id: int, admin: Annotated[User, Depends(get_admin_user)]
) -> dict[str, bool]:
    cur = await db.execute(
        """UPDATE password_reset_requests
           SET status = 'dismissed', resolved_at = ?, resolved_by = ?
           WHERE id = ? AND status = 'open'""",
        (db.utc_now(), admin.id, request_id),
    )
    if not cur.rowcount:
        raise HTTPException(status_code=404, detail="No open request with that id")
    return {"ok": True}
