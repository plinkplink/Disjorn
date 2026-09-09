"""The apps grant: what the house signs and the gate verifies (D2).

SPECS/2026-09-09-apps-serving-gate.md D2. Two functions and no state, because
this module is the ONE thing the house and the gate agree on: the gate has no
database and no house credential, so a grant is the entire conversation between
them.

    base64url(payload_json) + "." + base64url(hmac_sha256(secret, payload_json))

with payload `{v, app, roots, exp, ctx}`. Stateless and signed: minting is a
pure function of the secret and the facts, verification is a pure function of
the secret and the token, and revocation is bounded by `exp` (TTL 12 h, and the
house mints a fresh grant on every open) rather than by a table somebody has to
remember to delete from.

The opaque per-app user id is here for the same reason: it is derived from the
same secret, and the two sides must derive it identically or `shared_with`
names a person the app cannot recognise. It is `hmac(secret, "<uid>:<app>")`
in lowercase base32, truncated — stable per (user, app), and never the account
id, so an app learns that two visits are the same person without learning who.
"""

import hmac
import json
import time
from base64 import b32encode, urlsafe_b64encode
from hashlib import sha256
from typing import Any, Optional, Sequence

# Grant lifetime (D2): 12 hours.
DEFAULT_TTL_SEC = 43200

# Characters of base32 kept for the opaque id. 16 × 5 bits = 80 bits of the
# HMAC — far past collision range for a house, and short enough to read.
OPAQUE_ID_CHARS = 16


def _b64u(raw: bytes) -> str:
    """base64url, no padding — the token rides in a query string and a cookie."""
    return urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def mint(
    secret: bytes,
    *,
    app_id: str,
    roots: Sequence[str],
    ctx: dict[str, Any],
    ttl_sec: int = DEFAULT_TTL_SEC,
    now: Optional[float] = None,
) -> str:
    """A signed grant for one app, one root set, one viewer's context blob.

    `roots` is what the gate will let this cookie reach: `["live"]` for a
    viewer, `["live", "preview"]` for the owner. The app id is IN the payload
    and the gate checks it against the path's first segment, which is what
    stops a grant for one app from opening another on the shared origin.
    """
    payload = {
        "v": 1,
        "app": app_id,
        "roots": list(roots),
        "exp": int((time.time() if now is None else now) + ttl_sec),
        "ctx": ctx,
    }
    body = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    signature = hmac.new(secret, body, sha256).digest()
    return f"{_b64u(body)}.{_b64u(signature)}"


def opaque_user_id(secret: bytes, user_id: int, app_id: str) -> str:
    """This user, as this app is allowed to know them (D2).

    Stable for the pair and derived from the gate secret, so no table maps it
    back and the mapping cannot leak by being read. Different apps get
    different ids for the same person by construction.
    """
    digest = hmac.new(secret, f"{user_id}:{app_id}".encode("utf-8"), sha256).digest()
    return b32encode(digest).decode("ascii").rstrip("=").lower()[:OPAQUE_ID_CHARS]
