"""Apps grants: the one thing the house and the gate share.

Grants are stateless, so revocation is bounded by the TTL. This module imports
nothing from `app.*`: the gate must never gain a path to the house DB.
Apps can still correlate visitors on the shared origin.
"""

import base64
import binascii
import hashlib
import hmac
import json
import re
import time
from typing import Any, Iterable

# Bumping this invalidates every grant in flight.
GRANT_VERSION = 1

# Checked on mint and verify: the gate uses it as a path segment.
APP_ID_RE = re.compile(r"^[a-z2-7]{12}$")

# Unknown roots are refused here; who gets `preview` is the house's call.
ROOTS = ("live", "preview")

DEFAULT_TTL_SEC = 12 * 60 * 60

# Same floor as gate.GateSettings and the house; each checks it.
MIN_SECRET_BYTES = 32

# One key signs two kinds of thing. Without the tag, a payload's signature
# could equal some user's opaque id. NUL-terminated so no tag prefixes another.
DOMAIN_GRANT = b"grant\0"
DOMAIN_OPAQUE = b"opaque\0"

OPAQUE_ID_CHARS = 16


class GrantError(Exception):
    """Failed verification. `reason` is for logs only: every failure gets the same 403,
    since telling a caller why is telling them how to shape the next one."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def mint(
    secret: bytes,
    *,
    app_id: str,
    roots: Iterable[str],
    ctx: dict[str, Any],
    ttl_sec: int = DEFAULT_TTL_SEC,
    now: float | None = None,
) -> str:
    """Sign a grant. Bad inputs are house bugs and raise ValueError, not GrantError."""
    _require_secret(secret)
    if not APP_ID_RE.fullmatch(app_id or ""):
        raise ValueError(f"not an app id: {app_id!r}")
    root_list = list(roots)
    if not root_list or any(r not in ROOTS for r in root_list):
        raise ValueError(f"roots must be a non-empty subset of {list(ROOTS)}: {root_list!r}")
    if not isinstance(ctx, dict):
        raise ValueError("ctx must be a dict (the context blob)")
    if ttl_sec <= 0:
        raise ValueError("ttl_sec must be positive")

    issued = time.time() if now is None else now
    payload = {
        "v": GRANT_VERSION,
        "app": app_id,
        "roots": root_list,
        "exp": int(issued) + int(ttl_sec),
        "ctx": ctx,
    }
    body = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return f"{_b64e(body)}.{_b64e(_sign(secret, DOMAIN_GRANT, body))}"


def verify(secret: bytes, token: str, now: float | None = None) -> dict[str, Any]:
    """Return the payload of a valid grant, or raise GrantError.

    Signature is checked before parsing. A short secret raises ValueError
    so a bad key cannot hide behind 403s.
    """
    _require_secret(secret)
    if not isinstance(token, str) or token.count(".") != 1:
        raise GrantError("malformed token")
    body_b64, sig_b64 = token.split(".")
    try:
        body = _b64d(body_b64)
        sig = _b64d(sig_b64)
    except (binascii.Error, ValueError):
        raise GrantError("malformed token") from None

    if not hmac.compare_digest(sig, _sign(secret, DOMAIN_GRANT, body)):
        raise GrantError("bad signature")

    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise GrantError("malformed payload") from None
    if not isinstance(payload, dict):
        raise GrantError("malformed payload")

    if payload.get("v") != GRANT_VERSION:
        raise GrantError(f"unsupported grant version: {payload.get('v')!r}")
    if not APP_ID_RE.fullmatch(str(payload.get("app") or "")):
        raise GrantError("bad app id")

    roots = payload.get("roots")
    if (
        not isinstance(roots, list)
        or not roots
        or any(not isinstance(r, str) or r not in ROOTS for r in roots)
    ):
        raise GrantError("bad roots")

    exp = payload.get("exp")
    if not isinstance(exp, int) or isinstance(exp, bool):
        raise GrantError("bad expiry")
    if (time.time() if now is None else now) >= exp:
        raise GrantError("expired")

    ctx = payload.get("ctx")
    if not isinstance(ctx, dict):
        raise GrantError("bad context blob")

    return payload


def opaque_user_id(secret: bytes, user_id: int, app_id: str) -> str:
    """The per-app handle an app sees instead of an account id."""
    _require_secret(secret)
    digest = _sign(secret, DOMAIN_OPAQUE, f"{user_id}:{app_id}".encode("utf-8"))
    return base64.b32encode(digest).decode("ascii").rstrip("=").lower()[:OPAQUE_ID_CHARS]


# Wire encoding


def _require_secret(secret: bytes) -> None:
    """Every path that touches the key calls this first."""
    if not isinstance(secret, (bytes, bytearray)) or len(secret) < MIN_SECRET_BYTES:
        raise ValueError("grant secret must be at least 32 bytes")


def _sign(secret: bytes, domain: bytes, body: bytes) -> bytes:
    """HMAC over `domain + body`; never sign without a domain."""
    return hmac.new(secret, domain + body, hashlib.sha256).digest()


def _b64e(raw: bytes) -> str:
    """base64url, unpadded, for URLs and cookies."""
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64d(text: str) -> bytes:
    """Strict. Never use `urlsafe_b64decode`: it drops bad characters, giving one grant two spellings."""
    return base64.b64decode(
        text + "=" * (-len(text) % 4), altchars=b"-_", validate=True
    )
