"""Auth module (WP2): login/logout, /me, profile update, and auth dependencies.

Exported dependencies for other WPs:
    get_current_user — `disjorn_session` cookie -> sessions join users -> User.
                       Sliding 30-day expiry: expires_at refreshed on every use.
    get_current_bot  — `X-Api-Key` header -> SHA-256 hashed lookup in bots -> Bot.
    get_actor        — either of the above -> Actor (type: "user"|"bot", id, user|bot).
    get_admin_user   — get_current_user + the `is_admin` bit (403 without it).

The first three raise HTTP 401 on failure; get_admin_user raises 401 (not
logged in) or 403 (logged in, not an admin).

Hashing: passwords use argon2id (argon2-cffi); bot API keys use plain SHA-256
(they are high-entropy random secrets, so a slow KDF is unnecessary).

Helpers `hash_password`, `verify_password`, `hash_api_key` are shared with cli.py.

Passwords (migration 007):
    POST /auth/password                 change your own; ends your OTHER sessions
    POST /auth/users/{id}/password      ADMIN; lockout recovery for one account

Every user-authenticated dependency here also enforces `users.must_change_password`:
while it is set, only the routes in ROTATION_EXEMPT_ROUTES answer normally and
everything else is a 403 whose detail is exactly PASSWORD_CHANGE_REQUIRED, so a
client can recognize it and route to the change form. Bot authentication is
unaffected — API keys rotate through the admin surface, not through here.
"""

import datetime
import hashlib
import secrets
from typing import Annotated, Optional

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from fastapi import APIRouter, Cookie, Depends, Header, HTTPException, Request, Response
from pydantic import BaseModel, Field

from .. import db
from ..config import get_settings
from ..models import Bot, MemberType, User, UserStatus

router = APIRouter()

COOKIE_NAME = "disjorn_session"
SESSION_TTL = datetime.timedelta(days=30)

# Password rules, boring on purpose: a floor on length and nothing else. No
# composition rules, no expiry, no reuse history — each of those pushes users
# toward worse, more predictable passwords, and none of them is what this
# server is defending against.
PASSWORD_MIN_LENGTH = 12

# The exact `detail` of a rotation-gate 403. Clients match on this string, so
# it is API surface: change it and the change form stops being reachable.
PASSWORD_CHANGE_REQUIRED = "Password change required"

# (method, path) pairs a rotation-pending user may still reach: read your own
# account, set a password, or leave. Anything else is walled off until the
# password the admin handed over has been replaced.
ROTATION_EXEMPT_ROUTES = frozenset(
    {
        ("POST", "/auth/password"),
        ("GET", "/me"),
        ("POST", "/auth/logout"),
    }
)

_ph = PasswordHasher()  # argon2id by default
_dummy_hash: Optional[str] = None  # lazy; used to equalize timing for unknown users


# ---------------------------------------------------------------------------
# Hashing helpers (also used by cli.py)
# ---------------------------------------------------------------------------

def hash_password(password: str) -> str:
    return _ph.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    try:
        _ph.verify(password_hash, password)
        return True
    except (VerificationError, InvalidHashError):
        return False


def hash_api_key(api_key: str) -> str:
    return hashlib.sha256(api_key.encode("utf-8")).hexdigest()


def _dummy_verify(password: str) -> None:
    """Burn the same time as a real verify so unknown usernames aren't a timing oracle."""
    global _dummy_hash
    if _dummy_hash is None:
        _dummy_hash = _ph.hash("disjorn-dummy")
    verify_password(_dummy_hash, password)


# ---------------------------------------------------------------------------
# Session helpers
# ---------------------------------------------------------------------------

def session_expiry() -> str:
    """UTC ISO-8601 timestamp SESSION_TTL from now (same format as db.utc_now())."""
    return (
        (datetime.datetime.now(datetime.timezone.utc) + SESSION_TTL)
        .strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3]
        + "Z"
    )


def _set_session_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        COOKIE_NAME,
        token,
        max_age=int(SESSION_TTL.total_seconds()),
        path="/",
        httponly=True,
        samesite="lax",
        secure=get_settings().COOKIE_SECURE,
    )


def _clear_session_cookie(response: Response) -> None:
    response.delete_cookie(
        COOKIE_NAME,
        path="/",
        httponly=True,
        samesite="lax",
        secure=get_settings().COOKIE_SECURE,
    )


def _user_from_row(row: dict) -> User:
    # Local import: media imports this module (store_avatar's callers need the
    # auth dependencies), so the versioned-URL helper can only be pulled in at
    # call time. See media.avatar_version for why the `?v=` exists.
    from .media import user_avatar_url

    return User(
        id=row["id"],
        username=row["username"],
        display_name=row["display_name"],
        avatar_path=row["avatar_path"],
        avatar_url=user_avatar_url(row["id"], row["avatar_path"]),
        status=row["status"],
        is_admin=bool(row["is_admin"]),
        created_at=row["created_at"],
    )


def _bot_from_row(row: dict) -> Bot:
    return Bot(
        id=row["id"],
        name=row["name"],
        avatar_path=row["avatar_path"],
        chibi_pack=row["chibi_pack"],
        created_at=row["created_at"],
    )


# ---------------------------------------------------------------------------
# Auth dependencies (exported for other WPs)
# ---------------------------------------------------------------------------

SessionCookie = Annotated[Optional[str], Cookie(alias=COOKIE_NAME)]
ApiKeyHeader = Annotated[Optional[str], Header(alias="X-Api-Key")]


async def _session_user(token: Optional[str]) -> Optional[tuple[User, bool]]:
    """(user, must_change_password) for a session token, or None if it is no good.

    Slides the session's expiry out to now + 30d on every successful use.
    """
    if not token:
        return None
    row = await db.fetch_one(
        """SELECT s.expires_at AS session_expires_at, u.*
           FROM sessions s JOIN users u ON u.id = s.user_id
           WHERE s.token = ?""",
        (token,),
    )
    if row is None:
        return None
    if row["session_expires_at"] <= db.utc_now():  # ISO strings compare lexicographically
        await db.execute("DELETE FROM sessions WHERE token = ?", (token,))
        return None
    # Sliding expiry: every authenticated use pushes expiry out to now + 30d.
    await db.execute(
        "UPDATE sessions SET expires_at = ? WHERE token = ?",
        (session_expiry(), token),
    )
    return _user_from_row(row), bool(row["must_change_password"])


async def _user_for_token(token: Optional[str]) -> Optional[User]:
    """Session token -> User, with no rotation gate applied.

    The WebSocket handshake (ws.py) authenticates through here. The gate is
    HTTP-shaped — it answers 403 with a detail string a client can branch on —
    and a socket has no way to say that, so rotation state is not enforced on
    the WS surface. See ROTATION_EXEMPT_ROUTES.
    """
    found = await _session_user(token)
    return found[0] if found is not None else None


def _route_key(request: Request) -> tuple[str, str]:
    """(METHOD, path) for the current request, with any ASGI root_path stripped."""
    path = request.url.path
    root = request.scope.get("root_path") or ""
    if root and path.startswith(root):
        path = path[len(root):] or "/"
    return request.method.upper(), path


def _check_rotation(request: Request, must_change_password: bool) -> None:
    """403 unless this route is one a rotation-pending user is still allowed."""
    if must_change_password and _route_key(request) not in ROTATION_EXEMPT_ROUTES:
        raise HTTPException(status_code=403, detail=PASSWORD_CHANGE_REQUIRED)


async def _bot_for_key(api_key: Optional[str]) -> Optional[Bot]:
    if not api_key:
        return None
    row = await db.fetch_one(
        "SELECT * FROM bots WHERE api_key_hash = ?", (hash_api_key(api_key),)
    )
    return _bot_from_row(row) if row is not None else None


async def get_current_user(request: Request, disjorn_session: SessionCookie = None) -> User:
    """Session cookie -> User. 401 if missing/unknown/expired. Sliding 30d refresh.

    403 (PASSWORD_CHANGE_REQUIRED) if the account still owes a password
    rotation and the route is not exempt.
    """
    found = await _session_user(disjorn_session)
    if found is None:
        raise HTTPException(status_code=401, detail="Not authenticated")
    user, must_change_password = found
    _check_rotation(request, must_change_password)
    return user


async def get_current_bot(x_api_key: ApiKeyHeader = None) -> Bot:
    """X-Api-Key header -> Bot (SHA-256 hashed lookup). 401 on failure."""
    bot = await _bot_for_key(x_api_key)
    if bot is None:
        raise HTTPException(status_code=401, detail="Invalid or missing API key")
    return bot


async def get_admin_user(request: Request, disjorn_session: SessionCookie = None) -> User:
    """Session cookie -> User, gated on the `is_admin` bit.

    The single admin gate for the whole app (Architecture §3: flat access, one
    `is_admin` bit used only for account/bot management). Bots can never pass
    it — admin surfaces are cookie-only by construction, so a leaked bot API
    key cannot reconfigure other bots.

    An admin who owes a password rotation is gated like anybody else: the
    rotation check in get_current_user runs first, so the admin verbs stay shut
    until their own password is their own.
    """
    user = await get_current_user(request, disjorn_session)
    if not user.is_admin:
        raise HTTPException(status_code=403, detail="Admin only")
    return user


class Actor(BaseModel):
    """Either a user (cookie) or a bot (API key). Exactly one of user/bot is set."""

    type: MemberType
    id: int
    user: Optional[User] = None
    bot: Optional[Bot] = None


async def get_actor(
    request: Request,
    disjorn_session: SessionCookie = None,
    x_api_key: ApiKeyHeader = None,
) -> Actor:
    """Authenticate as either a user (session cookie) or a bot (X-Api-Key). 401 on failure.

    The rotation gate applies to the user branch only. A route reached with an
    actor is still an authenticated route, so a user who owes a rotation gets
    the same 403 here as they would through get_current_user; bots have no
    password to rotate and pass through untouched.
    """
    found = await _session_user(disjorn_session)
    if found is not None:
        user, must_change_password = found
        _check_rotation(request, must_change_password)
        return Actor(type="user", id=user.id, user=user)
    bot = await _bot_for_key(x_api_key)
    if bot is not None:
        return Actor(type="bot", id=bot.id, bot=bot)
    raise HTTPException(status_code=401, detail="Not authenticated")


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

class LoginRequest(BaseModel):
    username: str
    password: str


@router.post("/auth/login")
async def login(body: LoginRequest, response: Response) -> User:
    row = await db.fetch_one("SELECT * FROM users WHERE username = ?", (body.username,))
    if row is None:
        _dummy_verify(body.password)
        raise HTTPException(status_code=401, detail="Invalid username or password")
    if not verify_password(row["password_hash"], body.password):
        raise HTTPException(status_code=401, detail="Invalid username or password")
    if _ph.check_needs_rehash(row["password_hash"]):
        await db.execute(
            "UPDATE users SET password_hash = ? WHERE id = ?",
            (hash_password(body.password), row["id"]),
        )
    token = secrets.token_urlsafe(32)
    await db.execute(
        "INSERT INTO sessions (token, user_id, created_at, expires_at) VALUES (?, ?, ?, ?)",
        (token, row["id"], db.utc_now(), session_expiry()),
    )
    _set_session_cookie(response, token)
    return _user_from_row(row)


@router.post("/auth/logout")
async def logout(response: Response, disjorn_session: SessionCookie = None) -> dict[str, bool]:
    if disjorn_session:
        await db.execute("DELETE FROM sessions WHERE token = ?", (disjorn_session,))
    _clear_session_cookie(response)
    return {"ok": True}


@router.get("/me")
async def me(user: Annotated[User, Depends(get_current_user)]) -> User:
    return user


# ---------------------------------------------------------------------------
# Passwords
# ---------------------------------------------------------------------------
#
# Deliberately NOT a field on PATCH /me: a password is not profile data, and
# sharing a request body with display_name would mean every profile save is
# also a credential write. Its own verb, its own route, its own gate.


class PasswordChange(BaseModel):
    current_password: str
    new_password: str = Field(min_length=PASSWORD_MIN_LENGTH)


@router.post("/auth/password")
async def change_password(
    body: PasswordChange,
    user: Annotated[User, Depends(get_current_user)],
    disjorn_session: SessionCookie = None,
) -> dict[str, bool]:
    """Change your own password: current one required, other sessions ended.

    Ending the other sessions is the half that matters. A password change is
    normally a response to somebody else having had the password — the admin
    who created the account, or worse — and whoever that was may be holding a
    live session cookie right now. Without the DELETE below, changing the
    password evicts nobody and the whole feature is theater. The calling
    session is kept so the user is not signed out of the tab they just used.
    """
    row = await db.fetch_one("SELECT password_hash FROM users WHERE id = ?", (user.id,))
    if row is None:  # account deleted mid-session
        raise HTTPException(status_code=401, detail="Not authenticated")
    if not verify_password(row["password_hash"], body.current_password):
        # 403, not 401: the session is fine, the claim about the old password
        # is not. Nothing is written on this path.
        raise HTTPException(status_code=403, detail="Current password is incorrect")
    if body.new_password == body.current_password:
        raise HTTPException(
            status_code=400, detail="New password must differ from the current one"
        )

    new_hash = hash_password(body.new_password)  # argon2 is slow; hash before BEGIN
    async with db.transaction():
        # One transaction: the new hash and the cleared flag land together, so
        # there is no window where the user has rotated but is still walled off
        # (or vice versa).
        await db.execute(
            "UPDATE users SET password_hash = ?, must_change_password = 0 WHERE id = ?",
            (new_hash, user.id),
            commit=False,
        )
        await db.execute(
            "DELETE FROM sessions WHERE user_id = ? AND token != ?",
            (user.id, disjorn_session),
            commit=False,
        )
    return {"ok": True}


class AdminPasswordReset(BaseModel):
    new_password: str = Field(min_length=PASSWORD_MIN_LENGTH)


@router.post("/auth/users/{user_id}/password")
async def admin_reset_password(
    user_id: int,
    body: AdminPasswordReset,
    admin: Annotated[User, Depends(get_admin_user)],
) -> dict[str, bool]:
    """ADMIN: set another user's password (lockout recovery). Narrow on purpose.

    The account is marked as owing a rotation and ALL of its sessions die, so
    the admin's knowledge of the password is good for exactly one login and the
    user must immediately replace it. This is the only admin verb over another
    account's credentials — there is no read side, because there is nothing to
    read: only the argon2 hash is stored.
    """
    if user_id == admin.id:
        # Self-service is the other route, and it does not lock you out of your
        # own sessions or make you rotate twice.
        raise HTTPException(
            status_code=400, detail="Use POST /auth/password to change your own password"
        )
    row = await db.fetch_one("SELECT id FROM users WHERE id = ?", (user_id,))
    if row is None:
        raise HTTPException(status_code=404, detail="User not found")

    new_hash = hash_password(body.new_password)
    async with db.transaction():
        await db.execute(
            "UPDATE users SET password_hash = ?, must_change_password = 1 WHERE id = ?",
            (new_hash, user_id),
            commit=False,
        )
        await db.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,), commit=False)
    return {"ok": True}


class ProfileUpdate(BaseModel):
    display_name: Optional[str] = Field(default=None, min_length=1, max_length=80)
    status: Optional[UserStatus] = None


@router.patch("/me")
async def update_me(
    body: ProfileUpdate, user: Annotated[User, Depends(get_current_user)]
) -> User:
    sets: list[str] = []
    params: list = []
    if body.display_name is not None:
        sets.append("display_name = ?")
        params.append(body.display_name)
    if body.status is not None:
        sets.append("status = ?")
        params.append(body.status)
    if sets:
        params.append(user.id)
        await db.execute(f"UPDATE users SET {', '.join(sets)} WHERE id = ?", params)
    row = await db.fetch_one("SELECT * FROM users WHERE id = ?", (user.id,))
    assert row is not None
    return _user_from_row(row)
