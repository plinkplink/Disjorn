"""Apps grants: the one thing the house and the gate both understand.

The house mints, the gate verifies, and nothing else passes between them.
A grant is `base64url(payload).base64url(hmac_sha256(secret, payload))` —
stateless by design (spec `SPECS/2026-09-09-apps-serving-gate.md`, D2), so the
gate needs no database, no house credential and no call back into the house.
Revocation is therefore bounded by the TTL, not immediate: the house mints a
fresh grant on every Open/embed, so a share withdrawn now stops working within
12 hours and not sooner. That trade is what buys wall 5 — "the gate has no
database and no house credential".

This module is deliberately **pure stdlib and imports nothing from `app.*`**.
The gate (`app/gate.py`) is a second process that must never grow a path to
the house DB, and an import edge is how such a path starts.

`user.id` inside `ctx` is never the account id. `opaque_user_id` derives a
stable per-(user, app) handle under the same secret, so an app can recognise a
returning visitor and can correlate nothing across apps by id alone. (V1's
shared serving origin means apps can still correlate by other means — see the
parent spec's stated v1 limitation; this function is not the wall for that.)
"""

import base64
import binascii
import hashlib
import hmac
import json
import re
import time
from typing import Any, Iterable

# Grant format version. Bumping it invalidates every grant in flight, which is
# the intended blast radius: a token whose meaning changed must not verify.
GRANT_VERSION = 1

# Stage 1's app id (routers/apps.py APP_ID_RE, harness/cc/apps/disjorn-apps-launch):
# 12 characters of lowercase RFC 4648 base32. It is a directory name under
# /srv/apps-www and the first path segment at the gate, so it is shape-checked
# on both sides of the token rather than trusted from either.
APP_ID_RE = re.compile(r"^[a-z2-7]{12}$")

# The only two serving roots (D4). `preview` is granted to the owner only —
# that decision belongs to the house's minting call, not here; this module
# only refuses roots it has never heard of.
ROOTS = ("live", "preview")

# 12 hours (D2). The house re-mints on every Open, so this is the revocation
# bound, not a session length.
DEFAULT_TTL_SEC = 12 * 60 * 60

# Characters of lowercased base32 kept from the per-app HMAC. 16 base32 chars
# is 80 bits — far past collision range for a house, short enough to read.
OPAQUE_ID_CHARS = 16


class GrantError(Exception):
    """A grant that did not verify. `reason` is safe to log, never to serve.

    The gate answers every failure with the same 403 and the same sentence:
    telling a caller *why* their token failed is telling them how to shape the
    next one.
    """

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
    """Sign a grant for one app id and one set of roots.

    Raises ValueError on inputs the house got wrong (bad app id, unknown root,
    non-dict ctx). That is a house bug, not a caller's, so it is a ValueError
    and not the 403-shaped GrantError.
    """
    if not isinstance(secret, (bytes, bytearray)) or len(secret) < 32:
        raise ValueError("grant secret must be at least 32 bytes")
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
    # sort_keys + compact separators so a payload round-trips to the same bytes
    # anywhere; the signature covers the bytes, not the dict, but a stable
    # encoding keeps tokens diffable and logs comparable.
    body = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return f"{_b64e(body)}.{_b64e(_sign(secret, body))}"


def verify(secret: bytes, token: str, now: float | None = None) -> dict[str, Any]:
    """Return the payload of a valid grant, or raise GrantError.

    Checks run signature-first: the JSON is not parsed until the bytes are
    known to be ours, so a forged token never reaches the parser.
    """
    if not isinstance(token, str) or token.count(".") != 1:
        raise GrantError("malformed token")
    body_b64, sig_b64 = token.split(".")
    try:
        body = _b64d(body_b64)
        sig = _b64d(sig_b64)
    except (binascii.Error, ValueError):
        raise GrantError("malformed token") from None

    if not hmac.compare_digest(sig, _sign(secret, body)):
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
    """The per-app handle an app sees instead of an account id (D2).

    Stable for one (user, app) pair, unrelated across apps, and not reversible
    without the gate secret.
    """
    digest = _sign(secret, f"{user_id}:{app_id}".encode("utf-8"))
    return base64.b32encode(digest).decode("ascii").rstrip("=").lower()[:OPAQUE_ID_CHARS]


# ---------------------------------------------------------------------------
# Wire encoding
# ---------------------------------------------------------------------------


def _sign(secret: bytes, body: bytes) -> bytes:
    return hmac.new(secret, body, hashlib.sha256).digest()


def _b64e(raw: bytes) -> str:
    """base64url, no padding — a grant rides in a URL and in a cookie."""
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64d(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
