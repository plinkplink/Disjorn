"""Auth module (WP2): login/logout, /me, profile update, and auth dependencies.

Exported dependencies for other WPs:
    get_current_user — `disjorn_session` cookie -> sessions join users -> User.
                       Sliding 30-day expiry: expires_at refreshed on every use.
    get_current_bot  — `X-Api-Key` header -> SHA-256 hashed lookup in bots -> Bot.
    get_actor        — either of the above -> Actor (type: "user"|"bot", id, user|bot).

All three raise HTTP 401 on failure.

Hashing: passwords use argon2id (argon2-cffi); bot API keys use plain SHA-256
(they are high-entropy random secrets, so a slow KDF is unnecessary).

Helpers `hash_password`, `verify_password`, `hash_api_key` are shared with cli.py.
"""

import datetime
import hashlib
import secrets
from typing import Annotated, Optional

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from fastapi import APIRouter, Cookie, Depends, Header, HTTPException, Response
from pydantic import BaseModel, Field

from .. import db
from ..config import get_settings
from ..models import Bot, MemberType, User, UserStatus

router = APIRouter()

COOKIE_NAME = "disjorn_session"
SESSION_TTL = datetime.timedelta(days=30)

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
    return User(
        id=row["id"],
        username=row["username"],
        display_name=row["display_name"],
        avatar_path=row["avatar_path"],
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


async def _user_for_token(token: Optional[str]) -> Optional[User]:
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
    return _user_from_row(row)


async def _bot_for_key(api_key: Optional[str]) -> Optional[Bot]:
    if not api_key:
        return None
    row = await db.fetch_one(
        "SELECT * FROM bots WHERE api_key_hash = ?", (hash_api_key(api_key),)
    )
    return _bot_from_row(row) if row is not None else None


async def get_current_user(disjorn_session: SessionCookie = None) -> User:
    """Session cookie -> User. 401 if missing/unknown/expired. Sliding 30d refresh."""
    user = await _user_for_token(disjorn_session)
    if user is None:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return user


async def get_current_bot(x_api_key: ApiKeyHeader = None) -> Bot:
    """X-Api-Key header -> Bot (SHA-256 hashed lookup). 401 on failure."""
    bot = await _bot_for_key(x_api_key)
    if bot is None:
        raise HTTPException(status_code=401, detail="Invalid or missing API key")
    return bot


class Actor(BaseModel):
    """Either a user (cookie) or a bot (API key). Exactly one of user/bot is set."""

    type: MemberType
    id: int
    user: Optional[User] = None
    bot: Optional[Bot] = None


async def get_actor(
    disjorn_session: SessionCookie = None,
    x_api_key: ApiKeyHeader = None,
) -> Actor:
    """Authenticate as either a user (session cookie) or a bot (X-Api-Key). 401 on failure."""
    user = await _user_for_token(disjorn_session)
    if user is not None:
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
