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

ONE SECRET, TWO THINGS SIGNED, SO EACH SAYS WHICH IT IS (Claudette #2551).
Every HMAC here is taken over a DOMAIN TAG plus the bytes: `b"grant\0"` for a
token payload, `b"opaque\0"` for the per-app handle. Without the tag the two
share a key and an attacker who could ever choose one input's bytes would be
choosing the other's — a grant payload whose bytes happened to read
`"7:egnnhtz3ppwi"` would carry that user's opaque id as its signature. The
NUL is what makes the tag unambiguous: no tag is a prefix of another and no
payload can start with one, so `domain + body` parses back one way only.

Every entry point checks the secret's length, not only `mint`: a gate that
booted with an empty or short `APPS_GATE_SECRET` must fail CLOSED rather than
verify happily under a key that is not one (Claudette #2544).
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

# Below this a secret is not one. Same floor as gate.GateSettings and the
# house's MIN_GATE_SECRET_BYTES; stated three times because all three are the
# door to the same key and none of them may be the only one that checks.
MIN_SECRET_BYTES = 32

# Domain tags (see the module docstring). NUL-terminated so no tag can be the
# prefix of another and no signed body can begin with one.
DOMAIN_GRANT = b"grant\0"
DOMAIN_OPAQUE = b"opaque\0"

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
    # sort_keys + compact separators so a payload round-trips to the same bytes
    # anywhere; the signature covers the bytes, not the dict, but a stable
    # encoding keeps tokens diffable and logs comparable.
    body = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return f"{_b64e(body)}.{_b64e(_sign(secret, DOMAIN_GRANT, body))}"


def verify(secret: bytes, token: str, now: float | None = None) -> dict[str, Any]:
    """Return the payload of a valid grant, or raise GrantError.

    Checks run signature-first: the JSON is not parsed until the bytes are
    known to be ours, so a forged token never reaches the parser.

    The secret is checked BEFORE the token, and it raises the same ValueError
    `mint` does rather than a GrantError: a gate holding a key that is not one
    has a configuration bug, not a visitor with a bad token, and answering 403
    would hide it behind traffic (Claudette #2544).
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
    """The per-app handle an app sees instead of an account id (D2).

    Stable for one (user, app) pair, unrelated across apps, and not reversible
    without the gate secret.
    """
    _require_secret(secret)
    digest = _sign(secret, DOMAIN_OPAQUE, f"{user_id}:{app_id}".encode("utf-8"))
    return base64.b32encode(digest).decode("ascii").rstrip("=").lower()[:OPAQUE_ID_CHARS]


# ---------------------------------------------------------------------------
# Wire encoding
# ---------------------------------------------------------------------------


def _require_secret(secret: bytes) -> None:
    """Every path that touches the key asks this first, and asks it the same way."""
    if not isinstance(secret, (bytes, bytearray)) or len(secret) < MIN_SECRET_BYTES:
        raise ValueError("grant secret must be at least 32 bytes")


def _sign(secret: bytes, domain: bytes, body: bytes) -> bytes:
    """HMAC over `domain + body`. The domain is never optional — a call site
    that has to pick one cannot forget that there are two."""
    return hmac.new(secret, domain + body, hashlib.sha256).digest()


def _b64e(raw: bytes) -> str:
    """base64url, no padding — a grant rides in a URL and in a cookie."""
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64d(text: str) -> bytes:
    """base64url, strictly — an out-of-alphabet character is an error, not litter.

    `urlsafe_b64decode` DISCARDS characters outside the alphabet, so a token
    with four `!` spliced into its payload decodes to the same bytes as the
    real one and verifies under the real signature (Claudette #2551). That is
    two spellings of one grant, which is one spelling too many for anything
    that gets logged, cached or compared. `validate=True` is only available on
    `b64decode`, hence the explicit `altchars` — it translates `-_` to `+/`
    before the alphabet check, so real base64url still decodes.
    """
    return base64.b64decode(
        text + "=" * (-len(text) % 4), altchars=b"-_", validate=True
    )
